# Google Drive setup

The desktop app can read an invoice tree straight from Google Drive and publish
results back to a new Drive folder.

**If you are just using the app: there is no setup.** Open it → **Source: ☁️
Google Drive → Connect Google Drive**, sign in with your work account, paste the
Drive **folder link or id** of the invoice tree, name the **output folder**, and
**Run**. Results (master workbook + a folder of the detected invoices) are written
to a _new_ Drive folder — your originals are never modified.

The rest of this page is for whoever builds and releases the app.

## Why it needs no per-user setup

The OAuth client is configured with user type **Internal**, meaning only accounts
in our Google Workspace org can consent to it. Google waives app verification
entirely for internal apps, which buys three things that the old External +
Testing setup didn't have:

- the _restricted_ `drive.readonly` scope works with **no security assessment**
  (external apps would need the annual third-party CASA audit to publish it),
- **no test-user list** — every account in the org can sign in, no allowlisting,
- **no 7-day sign-in expiry.** External apps stuck in Testing mode get refresh
  tokens that die after a week; internal apps don't, so long scans and repeat runs
  don't hit surprise re-consent.

Because it's one org-wide client, `client_secret.json` is **bundled inside the
app** and users never handle it.

## Maintainer: the Cloud project (one-time)

The Cloud project must be **owned by the Workspace organisation** — a project
created under a personal `@gmail.com` account will not offer the _Internal_ user
type at all.

1. [https://console.cloud.google.com/](https://console.cloud.google.com/) → create the project **inside the org**
   (check the Organization field in the project picker; it must not say "No
   organization").
2. **APIs & Services → Library →** search **Google Drive API →** _Enable_.
3. **Google Auth Platform → Audience:** set **User type = Internal**.
4. **Google Auth Platform → Clients → Create client:** application type
   **Desktop app** → _Create_ → **Download JSON**.

## Maintainer: shipping the secret

The secret is **not in git**. Google's secret scanner reports leaked OAuth clients
on GitHub and auto-revokes them, which would break every installed copy of the app.
It's injected at build time instead:

```bash
# local release build
cp ~/Downloads/client_secret_*.json packaging/client_secret.json   # gitignored
pyinstaller packaging/InvoiceGSTRLinker.spec --noconfirm

# or point at it explicitly (this is what CI does, from a repo secret)
INVOICES_CLIENT_SECRET_FILE=/path/to/client_secret.json \
  pyinstaller packaging/InvoiceGSTRLinker.spec --noconfirm
```

For the GitHub Actions release build, paste the **whole JSON file contents** into a
repo secret named **`GOOGLE_CLIENT_SECRET_JSON`** (Settings → Secrets and variables
→ Actions → New repository secret). `build-desktop-apps.yml` writes it to
`packaging/client_secret.json` before PyInstaller runs. Forks and pull requests
don't get the secret, so they build a Drive-less app — that is intended.

The spec prints whether it bundled a client. Verify the built app before shipping —
this fails the build if the secret didn't make it in (CI runs it on every build, and
makes it fatal on a `v*` tag):

```bash
dist/InvoiceGSTRLinker/InvoiceGSTRLinker --selftest
# selftest OK: drive backend imports + discovery doc load + bundled OAuth client
```

A desktop OAuth client secret is not a true secret — Google's native-app guidance
assumes it can be extracted from any distributed binary — so bundling it is the
intended pattern, not a compromise. Internal user type means an extracted client
still only lets org accounts sign in.

## Overriding the bundled client

`resolve_client_secret()` (`invoices/io/drive.py`) resolves in the order
**explicit path → app-support dir → bundled**, so dropping your own
`client_secret.json` in the app-support folder points the app at a different Cloud
project without a rebuild — useful for development, or for a second org.

| OS      | Folder                                             |
| ------- | -------------------------------------------------- |
| macOS   | `~/Library/Application Support/InvoiceGSTRLinker/` |
| Windows | `%APPDATA%\InvoiceGSTRLinker\`                     |
| Linux   | `~/.config/InvoiceGSTRLinker/`                     |

The cached sign-in (`token.json`) lives there too; delete it to force re-consent.

## Good to know

- **Scopes.** `drive.readonly` to read the tree you point it at, `drive.file` to
  create its own output folder. Note that `drive.file` alone could not do the scan:
  it grants access only per-file, and picking a folder does **not** grant access to
  the files inside it — which is why the read path needs `drive.readonly` and hence
  why the client must be Internal.
- **Interrupted runs resume.** A large scan checkpoints continuously; if the app
  closes or the network drops, start the same folder again and it picks up where it
  left off.
- **Only in-scope files download.** The app reads only PDFs under
  `.../Payments/Kotak/...`, so out-of-scope parts of the Drive never transfer.
