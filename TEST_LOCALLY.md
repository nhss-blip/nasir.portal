# Test the updated portal locally

This is the complete updated application, including images and the licensed Urdu font. It contains no existing database, uploads, signing key or account credentials.

Use Python 3.10 or newer (validated with Python 3.12). Extract the ZIP and open a terminal in the nasir.portal folder.

## Windows

    py -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    .venv\Scripts\python.exe run_local.py

## Linux / macOS

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python run_local.py

On first start, choose your own test admin password when prompted. Open http://127.0.0.1:8080 on the same machine. Staff sign-in uses username admin and the password you chose. Press Ctrl+C in the terminal to stop.

The launcher always stores this test instance in test-runtime/data and test-runtime/uploads beside the code. It binds only to your machine and does not point at your production directories. Run it again to keep using the same test accounts/content.

## What to test

- Homepage links, phone layout, announcements and magazines.
- School search and combined announcement search/category filtering.
- Staff sign-in, publishing an English/Urdu announcement and uploading a real PDF.
- Editing, deletion confirmation, older-content pagination and backup downloads.
- Adding an editor account, signing out and changing passwords.

Run the automated checks with the selected virtual-environment Python:

    python -m unittest discover -s tests -v

Use the virtual-environment executable shown above in place of python if the environment is not activated.

The Student / Staff homepage split is deferred. ERP and Nextcloud links still use the existing school LAN addresses, so they work only where those services are reachable. New test content starts empty. Existing staff accounts and uploaded files have deliberately been omitted from this download.

For a separate Proxmox/Nginx test container or a migration test using a private copy of your existing data, follow README.md and use separate persistent directories. The run_local.py launcher is for local testing. Deploying this version requires a fresh staff sign-in, while existing passwords/content are preserved by the migration.
