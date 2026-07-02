#!/usr/bin/env python3
"""Plant realistic 'human-maintained mess' into the sample tree so the pipeline's
edge-case handling can be developed and tested.

All dirt lives under an isolated month — `FY 22-23/Payments/Kotak/Aug-2022/` —
which does not exist in the clean set, so existing golden files are untouched.
A manifest (`_dirt_manifest.json`) records every planted path + its expected
behaviour; tests/test_dirt.py asserts against it.

Usage:
    python scripts/introduce_dirt.py --root "Testing Environment"
    python scripts/introduce_dirt.py --root "Testing Environment" --clean   # remove dirt
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import fitz  # PyMuPDF

DIRT_MONTH = "Aug-2022"
MANIFEST = "_dirt_manifest.json"


# --------------------------------------------------------------------------- #
# content templates
# --------------------------------------------------------------------------- #
def tax_invoice(vendor, no, gstin, date, taxable, buyer="Nimbus Retail Solutions Pvt Ltd",
                buyer_gstin="07AAFCN4321P1Z5") -> str:
    gst = round(taxable * 0.09, 2)
    total = round(taxable + 2 * gst, 2)
    return (
        f"TAX INVOICE\n{vendor}\n"
        f"Invoice No.: {no}\nGSTIN: {gstin}\nInvoice Date: {date}\n"
        f"State: Delhi (07)\nPlace of Supply: 07-Delhi\n"
        f"Bill To:\n{buyer}\nGSTIN: {buyer_gstin}\n"
        f"12 Some Road, New Delhi, 110001\nState: Delhi (07)\n"
        f"# Description HSN/SAC Qty Rate (Rs.) Taxable Value (Rs.)\n"
        f"1 Professional services 998311 1 {taxable:,.2f} {taxable:,.2f}\n"
        f"Taxable Value\nRs. {taxable:,.2f}\n"
        f"CGST @ 9.0%\nRs. {gst:,.2f}\nSGST @ 9.0%\nRs. {gst:,.2f}\n"
        f"Total Invoice Value\nRs. {total:,.2f}\nITC Status: Eligible\n"
    )


def approval_form(vendor, apr_no, ref_no, gstin, date, taxable) -> str:
    gst = round(taxable * 0.09, 2)
    total = round(taxable + 2 * gst, 2)
    return (
        f"Nimbus Retail Solutions Pvt Ltd\nPURCHASE / EXPENSE APPROVAL FORM\n"
        f"Approval Form No.: {apr_no}\nApproval Date: {date}\n"
        f"Reference Invoice No.: {ref_no}\nInvoice Date: {date}\n"
        f"Vendor Name {vendor}\nVendor GSTIN {gstin}\n"
        f"Invoice (Taxable) Value Rs. {taxable:,.2f}\nTotal GST Rs. {2*gst:,.2f}\n"
        f"Total Invoice Value Rs. {total:,.2f}\n"
        f"Approval Decision\nStatus: Approved\nRemarks: Verified. Approved for payment.\n"
    )


def zephyr_invoice(no, gstin, date, taxable) -> str:
    """A non-standard layout the baseline extractor cannot fully parse."""
    total = round(taxable * 1.18, 2)
    return (
        f"BILL / TAX INVOICE\nZephyr Logistics Pvt Ltd\n"
        f"Bill No: {no}\nGST Registration No: {gstin}\nDated: {date}\n"
        f"Freight & handling charges\n"
        f"Taxable: INR {taxable:,.2f}\nIGST @ 18%: INR {round(taxable*0.18,2):,.2f}\n"
        f"Amount Payable: INR {total:,.2f}\n"
    )


# --------------------------------------------------------------------------- #
# pdf writers
# --------------------------------------------------------------------------- #
def write_pdf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    rect = fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50)
    page.insert_textbox(rect, text, fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def write_raw(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --------------------------------------------------------------------------- #
# planting
# --------------------------------------------------------------------------- #
def plant(root: Path) -> list[dict]:
    base = root / "FY 22-23" / "Payments" / "Kotak" / DIRT_MONTH
    entries: list[dict] = []

    def rec(rel_pdf: Path, label: str, note: str, **expect):
        entries.append({"path": str(rel_pdf.resolve()), "label": label,
                        "note": note, "expect": expect})

    # 1) invoice with a non-standard filename (+ stray non-PDF junk beside it)
    d = base / "05-Aug-2022" / "Quantum Metal Works Pvt Ltd"
    p = d / "scan0012.pdf"
    write_pdf(p, tax_invoice("Quantum Metal Works Pvt Ltd", "QMW/22-23/001",
                             "27QMWXX1234Q1Z8", "05-Aug-2022", 88000.00))
    write_raw(d / "notes.txt", b"paid via neft ref 99812")
    write_raw(d / "whatsapp_photo.jpg", b"\xff\xd8\xff\xe0not-really-a-jpg")
    rec(p, "misnamed_invoice",
        "invoice content, filename has no Invoice_ prefix -> content classifier must catch it",
        doc_type="invoice", id_mismatch=True)

    # 2) invoice mistakenly named Approval_*
    d = base / "06-Aug-2022" / "Quantum Metal Works Pvt Ltd"
    p = d / "Approval_QMW-22-23-002.pdf"
    write_pdf(p, tax_invoice("Quantum Metal Works Pvt Ltd", "QMW/22-23/002",
                             "27QMWXX1234Q1Z8", "06-Aug-2022", 45000.00))
    rec(p, "invoice_named_approval",
        "filename says approval, content is an invoice",
        doc_type="invoice", flag="filename_content_mismatch")

    # 3) approval mistakenly named Invoice_*  (must NOT count as an invoice)
    d = base / "06-Aug-2022" / "Sundry Vendor Pvt Ltd"
    p = d / "Invoice_FAKE-001.pdf"
    write_pdf(p, approval_form("Sundry Vendor Pvt Ltd", "APR/SUND/001",
                               "SUND/22-23/001", "07AABCS1111S1Z2", "06-Aug-2022", 12000.00))
    rec(p, "approval_named_invoice",
        "filename says invoice, content is an approval form",
        doc_type="approval", flag="filename_content_mismatch")

    # 4) empty company folder (no PDF at all)
    (base / "08-Aug-2022" / "Ghost Traders Pvt Ltd").mkdir(parents=True, exist_ok=True)
    entries.append({"path": None, "label": "empty_folder",
                    "note": "company folder with no invoice; must be skipped, no crash",
                    "expect": {}})

    # 5) extra nested subfolder between company and the PDF
    d = base / "10-Aug-2022" / "Vertex Supplies Pvt Ltd" / "Rescans"
    p = d / "Invoice_VERT-22-23-001.pdf"
    write_pdf(p, tax_invoice("Vertex Supplies Pvt Ltd", "VERT/22-23/001",
                             "24VERTX7788V1Z3", "10-Aug-2022", 61000.00))
    rec(p, "nested_subfolder",
        "unexpected 'Rescans' level -> folder company mis-derives, should flag for review",
        doc_type="invoice", flag="company_mismatch")

    # 6) invoice filed under the wrong company folder
    d = base / "12-Aug-2022" / "Zenith Corp"
    p = d / "Invoice_APEX-22-23-099.pdf"
    write_pdf(p, tax_invoice("Apex Business Consultants Pvt Ltd", "APEX/22-23/099",
                             "36AAECA4456F1Z1", "12-Aug-2022", 150000.00))
    rec(p, "misfiled_company",
        "folder 'Zenith Corp' but invoice vendor is Apex -> company mismatch",
        doc_type="invoice", flag="company_mismatch")

    # 7) duplicate invoice id (same id as a clean-set invoice)
    d = base / "14-Aug-2022" / "Apex Business Consultants Pvt Ltd"
    p = d / "Invoice_APEX-22-23-001.pdf"
    write_pdf(p, tax_invoice("Apex Business Consultants Pvt Ltd", "APEX/22-23/001",
                             "36AAECA4456F1Z1", "14-Aug-2022", 99000.00))
    rec(p, "duplicate_id",
        "same invoice id as an existing clean-set file -> duplicate detection",
        doc_type="invoice", flag="duplicate_id")

    # 8) vendor with a non-standard layout -> only the per-vendor extractor fills it
    d = base / "15-Aug-2022" / "Zephyr Logistics Pvt Ltd"
    p = d / "Invoice_ZEPH-22-23-001.pdf"
    write_pdf(p, zephyr_invoice("ZEPH-22-23-001", "07ZEPHY1234K1Z5", "15/08/2022", 104489.00))
    rec(p, "vendor_extractor",
        "Bill No/Dated/Amount Payable layout; baseline misses money+date, per-vendor extractor recovers",
        doc_type="invoice", total_value=123297.02)

    # 9) non-standard (ISO) date-folder format
    d = base / "2022-08-20" / "Meridian Tools Pvt Ltd"
    p = d / "Invoice_MERI-22-23-001.pdf"
    write_pdf(p, tax_invoice("Meridian Tools Pvt Ltd", "MERI/22-23/001",
                             "19MERID5566M1Z4", "20-Aug-2022", 33000.00))
    rec(p, "iso_date_folder",
        "date folder '2022-08-20' not in DD-Mon-YYYY form -> path_incomplete flag",
        doc_type="invoice", flag="path_incomplete")

    # 10) corrupt / non-PDF bytes with a .pdf extension
    d = base / "16-Aug-2022" / "Broken Files Pvt Ltd"
    p = d / "Invoice_BROK-22-23-001.pdf"
    write_raw(p, b"%PDF but not really -- this file is intentionally corrupt")
    rec(p, "corrupt_pdf",
        "unreadable PDF -> fault isolated, doc.error set, run continues",
        error=True)

    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description="plant test dirt into the sample tree")
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--clean", action="store_true", help="remove planted dirt")
    args = ap.parse_args()

    dirt_dir = args.root / "FY 22-23" / "Payments" / "Kotak" / DIRT_MONTH
    manifest_path = args.root / MANIFEST

    if args.clean:
        if dirt_dir.exists():
            shutil.rmtree(dirt_dir)
        manifest_path.unlink(missing_ok=True)
        print(f"removed dirt under {dirt_dir} and {manifest_path}")
        return

    entries = plant(args.root)
    manifest_path.write_text(json.dumps(entries, indent=2))
    planted = [e for e in entries if e["path"]]
    print(f"planted {len(planted)} dirty PDFs (+1 empty folder) under {dirt_dir}")
    for e in entries:
        print(f"  - {e['label']:22s} {e['note']}")
    print(f"\nmanifest: {manifest_path}")
    print(f'next: python -m invoices scan --root "{args.root}" --no-serve --workers 8')


if __name__ == "__main__":
    main()
