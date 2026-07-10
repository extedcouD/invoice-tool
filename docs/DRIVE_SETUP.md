# Google Drive setup (one-time)

The desktop app can read an invoice tree straight from Google Drive and publish
results back to a new Drive folder. To let it sign in to your account you create
a small Google Cloud OAuth client **once** and drop its `client_secret.json`
where the app looks for it. No servers, no billing — a few clicks.

## 1. Create the OAuth client

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library →** search **Google Drive API →** *Enable*.
3. **APIs & Services → OAuth consent screen:**
   - User type **External**, fill in the app name + your email, **Save**.
   - Leave **Publishing status = Testing**.
   - **Test users → Add users →** add every Google account that will run the app
     (yourself + colleagues). Only these accounts can sign in.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - Application type **Desktop app** → *Create*.
   - **Download JSON** — this is your `client_secret.json`.

## 2. Install the secret

Put the downloaded file (named exactly `client_secret.json`) in the app-support
folder:

| OS      | Folder |
|---------|--------|
| macOS   | `~/Library/Application Support/InvoiceGSTRLinker/` |
| Windows | `%APPDATA%\InvoiceGSTRLinker\` |
| Linux   | `~/.config/InvoiceGSTRLinker/` |

(The folder is created the first time you open the app; you can also make it by
hand.)

## 3. Use it

Open the app → **Source: ☁️ Google Drive → Connect Google Drive**. Your browser
opens once to grant access; the token is cached afterwards. Then paste the Drive
**folder link or id** of your invoice tree, give the **output folder** a name,
and **Run**. Results (master workbook + a folder of the detected invoices) are
written to a *new* Drive folder — your originals are never modified.

## Good to know

- **Scopes.** The app requests `drive.readonly` (to read your tree) and
  `drive.file` (to create its own output folder). It cannot see or touch files it
  didn't create, other than reading the folder you point it at.
- **Weekly re-consent.** While the OAuth client is in **Testing** mode, Google
  expires the sign-in about every 7 days, so you'll click "Connect" again
  occasionally. That's expected for an internal tool and avoids Google's app
  verification review. (Publishing the client to *Production* for wider use would
  require that review because `drive.readonly` is a restricted scope.)
- **Interrupted runs resume.** A large scan checkpoints continuously; if the app
  closes or the network drops, just start the same folder again and it picks up
  where it left off.
- **Only in-scope files download.** The app reads only PDFs under
  `.../Payments/Kotak/...`, so out-of-scope parts of the Drive never transfer.
