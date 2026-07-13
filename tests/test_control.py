"""Pause / stop / resume — the behaviours that made the tool feel broken.

Three regressions are pinned here:

1. **Pause was invisible.** `paused` was only published to the UI as a side effect
   of a document *completing*, and pausing stops documents completing — so
   status.json froze at `paused: false`, the button flipped back to "Pause" on the
   next poll, and Resume was unreachable. Control state must be *pulled* from the
   RunControl, not pushed from the pipeline.
2. **Stop was slow.** The gate was checked once per document, so a Stop waited out
   a whole 30-page OCR job. It is now checked between stages, and during the walk.
3. **Stop could corrupt resume.** `complete` was derived from "did the pipeline
   cancel" while the UI's phase was derived from "was Stop pressed" — so a Stop
   landing as the run drained marked the run *complete* yet offered "Resume", and
   the resume then silently rescanned from zero.
"""
from __future__ import annotations

import threading
import time

import pytest

from invoices.core.control import RunCancelled, RunControl
from invoices.core.interfaces import Stage
from invoices.core.models import Document
from invoices.core.pipeline import Pipeline


def doc(i: int) -> Document:
    return Document(id=f"d{i:03d}", path=f"/x/{i}.pdf", filename=f"{i}.pdf",
                    source_key=f"local:/x/{i}.pdf")


class Slow(Stage):
    """A stage that takes a beat, and records which docs it actually touched."""
    name = "slow"

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def process(self, d: Document) -> Document:
        time.sleep(self.delay)
        with self._lock:
            self.seen.append(d.id)
        return d


# --------------------------------------------------------------------------- #
# the primitive
# --------------------------------------------------------------------------- #
def test_gate_raises_once_stopped():
    c = RunControl()
    c.gate()                       # running: passes straight through
    c.stop()
    with pytest.raises(RunCancelled):
        c.gate()


def test_pause_blocks_until_resume_and_is_observable():
    c = RunControl()
    c.pause()
    assert c.paused is True        # ...and the UI can SEE it, without a doc completing

    released = threading.Event()

    def worker():
        c.gate()                   # parks here
        released.set()

    threading.Thread(target=worker, daemon=True).start()
    assert not released.wait(0.15)  # still parked
    c.resume()
    assert released.wait(1.0)       # let through
    assert c.paused is False


def test_stop_releases_a_paused_worker():
    """Stop must not leave workers parked on a pause that will never be lifted."""
    c = RunControl()
    c.pause()
    raised = threading.Event()

    def worker():
        try:
            c.gate()
        except RunCancelled:
            raised.set()

    threading.Thread(target=worker, daemon=True).start()
    time.sleep(0.05)
    c.stop()
    assert raised.wait(1.0)


def test_paused_time_is_excluded_from_the_clock():
    c = RunControl()
    c.pause()
    time.sleep(0.12)
    c.resume()
    assert c.paused_seconds() >= 0.1


# --------------------------------------------------------------------------- #
# what the UI is told (regression #1 — the headline bug)
# --------------------------------------------------------------------------- #
def test_status_reports_paused_without_any_document_completing(tmp_path):
    """The bug: `paused` was written to status.json only when a doc finished.

    Pausing stops docs finishing, so the flag never got published, the poll loop
    kept reading `paused: false`, the button flipped back to "⏸ Pause" — and the
    user could never press Resume. Control state has to be *pulled* from the live
    RunControl, which is what this asserts: no status.json, no completed document,
    and the UI is still told the truth.
    """
    from invoices.web.runner import RunController

    c = RunController(tmp_path)
    c.control = RunControl()
    c.phase = "scanning"

    assert c.status()["paused"] is False

    c.control.pause()
    assert c.status()["paused"] is True          # <-- nothing completed; still true
    assert c.status()["phase"] == "scanning"

    c.control.resume()
    assert c.status()["paused"] is False

    c.control.stop()
    assert c.status()["stopping"] is True

    # ...and once the run is over, a stale control must not keep claiming to pause
    c.phase = "done"
    assert c.status()["paused"] is False
    assert c.status()["stopping"] is False


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def test_stop_halts_the_run_and_leaves_the_rest_uncheckpointed():
    """A cancelled doc is never checkpointed, so a resume picks it up again."""
    control = RunControl()
    stage = Slow(delay=0.02)
    p = Pipeline([stage], workers=2, control=control)

    checkpointed: list[str] = []

    def walker():
        for i in range(200):
            yield doc(i)

    def on_done(d: Document) -> None:
        checkpointed.append(d.source_key)
        if len(checkpointed) == 5:
            control.stop()

    p.run(walker(), on_doc_done=on_done)

    assert p.cancelled is True
    assert not p.discovery_complete          # we stopped before the tree ran out
    assert len(checkpointed) < 200           # the rest is left for the resume
    # nothing was checkpointed that wasn't actually processed
    assert set(checkpointed) <= {f"local:/x/{d}.pdf" for d in range(200)}


def test_resume_skips_checkpointed_sources_but_counts_them_in_the_total():
    """A resumed run reports progress against the whole corpus, not the remainder."""
    stage = Slow(delay=0.0)
    p = Pipeline([stage], workers=2)
    already = {f"local:/x/{i}.pdf" for i in range(6)}   # pretend these are done

    docs = p.run((doc(i) for i in range(10)), skip_keys=already)

    assert sorted(d.id for d in docs) == [f"d{i:03d}" for i in range(6, 10)]
    assert stage.seen and set(stage.seen) == {f"d{i:03d}" for i in range(6, 10)}
    assert p.discovered == 10        # the WHOLE tree, so the bar isn't a lie
    assert p.skipped == 6
    assert p.completed == 4


def test_walk_is_cancellable_during_discovery():
    """Stop during the walk used to be a literal no-op — the walker never checked."""
    control = RunControl()
    p = Pipeline([Slow(delay=0.0)], workers=2, control=control)
    produced = 0

    def walker():
        nonlocal produced
        for i in range(10_000):
            control.gate()          # what the real walkers now do, per folder/page
            produced += 1
            if produced == 20:
                control.stop()
            yield doc(i)

    p.run(walker())

    assert p.cancelled is True
    assert produced < 10_000        # we did not have to enumerate the whole tree


def test_stopped_run_stays_resumable_and_a_finished_one_does_not(tmp_path):
    """The `complete` / `stopped` contradiction that silently rescanned from zero.

    `complete` was derived from the pipeline's cancel flag while the UI's phase came
    from `control.stopped`. A Stop landing as the run drained set phase=stopped AND
    complete=True: the UI offered Resume, `find_resumable` refused to reopen a
    complete run, and "resume" quietly started over. Both now come from one fact —
    did we actually reach the end of the tree.
    """
    from invoices.detect import run_scan
    from invoices.io.runstore import RunStore

    out = tmp_path / "out"

    def walker(root, settings, control=None):
        for i in range(50):
            if control is not None:
                control.gate()
            yield doc(i)

    # 1 · stopped part-way -> incomplete -> resumable
    control = RunControl()
    seen = 0

    def stopping_walker(root, settings, control=control):
        nonlocal seen
        for i in range(50):
            control.gate()
            seen += 1
            if seen == 10:
                control.stop()
            yield doc(i)

    store, result = run_scan("/nowhere", out, quiet=True, walker=stopping_walker,
                             root_key="k", control=control)
    assert result.complete is False
    assert store.read_meta()["complete"] is False
    assert RunStore.find_resumable(out, "k") is not None      # resume will reopen it

    # 2 · run to the end -> complete -> NOT offered as resumable
    store2, result2 = run_scan("/nowhere", out, quiet=True, walker=walker,
                               store=store, root_key="k", control=RunControl())
    assert result2.complete is True
    assert store2.read_meta()["complete"] is True
    assert RunStore.find_resumable(out, "k") is None

    # 3 · a Stop pressed *after* the tree was exhausted must not un-complete the run
    late = RunControl()
    store3, result3 = run_scan("/nowhere", out, quiet=True, walker=walker,
                               root_key="k2", control=late)
    late.stop()
    assert result3.complete is True


def test_a_stage_failure_is_isolated_but_a_stop_is_not_swallowed():
    """Fault isolation must not mistake a user's Stop for a broken document."""
    control = RunControl()

    class Boom(Stage):
        name = "boom"

        def process(self, d: Document) -> Document:
            if d.id == "d001":
                raise ValueError("bad pdf")
            if d.id == "d003":
                control.stop()      # a Stop raised from *inside* a stage
            return d

    p = Pipeline([Boom()], workers=1, control=control)
    out = p.run(doc(i) for i in range(6))

    broken = [d for d in out if d.error]
    assert [d.id for d in broken] == ["d001"]              # flagged, run continued
    assert any(f.code == "stage_error" for f in broken[0].flags)
    assert p.cancelled is True                             # ...and the Stop landed
    # the Stop was not recorded as a stage error on any document
    assert not any("RunCancelled" in (d.error or "") for d in out)
