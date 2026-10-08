# Nasir School Portal

Flask/Waitress application for school service shortcuts, announcements and magazine PDFs. Public reading and protected editor/administrator publishing share templates and styles. Student/staff homepage separation is deferred; the existing service list, including ERP, remains on the single homepage.

For an isolated test on your own computer, follow [TEST_LOCALLY.md](TEST_LOCALLY.md). The included `run_local.py` launcher keeps test data separate and prompts for your chosen admin password.

## Install and run

Requires Python 3.10+ (validated with 3.12), Flask, Waitress, PyMuPDF and Pillow.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# First run: provide SCHOOL_ADMIN_PASSWORD securely to the local process.
# It must contain at least 8 characters. Do not commit it or put it in shell history.
.venv/bin/python -m waitress --listen=127.0.0.1:8080 app:app
```

On Windows, install requirements in the selected Python environment and use `start.bat`. Run from the repository root. A local logo derivative at `images/logo.webp` is used when present; otherwise the tracked PNG is the fallback. Runtime database, signing key, generated images, and uploads are ignored by Git.

Nginx should proxy the application, including `/static/`, `/images/`, public routes and publishing routes. The old root `index.html` is now `templates/home.html`; `/index.html` redirects to `/`. Do not serve the checkout as a static directory: `data/` and `uploads/` must not be directly exposed.

Configure your managed Waitress service to restart on failure. Set Nginx `client_max_body_size 80m` to align with Flask's total request limit and choose upstream timeouts appropriate to your upload size. The Python process needs write access to the configured data/uploads paths.

## Runtime configuration

| Variable | Purpose |
| --- | --- |
| `SCHOOL_ADMIN_PASSWORD` | Creates the first admin account only when the users table is empty. Existing accounts and password hashes are retained. |
| `SCHOOL_DATA_DIR` | Optional absolute persistent data directory; defaults to `data/` beside `app.py`. Contains database and signing key. |
| `SCHOOL_UPLOAD_DIR` | Optional absolute persistent upload directory; defaults to `uploads/`. |
| `BEHIND_PROXY` | Set to `1` only when one trusted Nginx proxy supplies forwarding headers. Restrict direct upstream access and overwrite incoming client forwarding headers. |
| `HTTPS_ONLY` | Set to `1` when browser access uses HTTPS, enabling secure session cookies. Leave unset for an HTTP-only LAN. |

Boolean settings accept `1`, `true`, `yes`, or `on`; `0` and `false` are disabled. Keep persistent paths and the signing key across container replacement. Preserve both database and uploads. Waitress is the production entry point; `python app.py` is for local development.

## Existing data and security migration

The application automatically performs schema version 1 when starting against a legacy database. Before migration, it writes and integrity-checks a SQLite recovery copy under `data/migration-backups/`. It preserves user IDs, usernames, password hashes, roles, posts, magazines, issue metadata and attachment records. It adds permanent random authentication identities, session versions, a revocable session table and indexes.

**Existing staff sessions require a fresh sign-in after rollout.** Deleted accounts cannot transfer access to a replacement user even if SQLite reuses its numeric ID. Password changes revoke other sessions; logout revokes the current session so a copied cookie cannot be replayed. CSRF tokens are required for login and all protected POST operations.

Before production rollout, also preserve an independent copy of `data/` and `uploads/` outside the container. Migration is additive and does not silently repair or delete old invalid dates or orphan attachments. Existing attachment tables are not rebuilt with new foreign keys in this release; current write paths validate parents and clean up failed uploads. Foreign-key enforcement is enabled for declared references and new session records.

Legacy JSON/PDF import and cover backfill are explicit maintenance commands rather than side effects of every startup:

```sh
.venv/bin/python -m portal import-legacy
.venv/bin/python -m portal backfill-covers
```

Run them against the intended persistent directories. Import is repeatable and treats identical filenames in different magazines as separate issues. A bad legacy JSON batch rolls back rather than silently importing part of it. Unreadable cover documents are reported by issue ID.

## Publishing and backups

`/admin` has separate announcement, magazine issue, account, magazine-name, staff-account and backup/activity sections. Editors can publish and manage announcements/issues; administrator-only operations retain their role checks. Both content lists have search and pagination. Deletion requires a confirmation page and a CSRF-protected POST; GET never removes an item. This release has confirmation, not a trash/undo system.

Invalid form dates are rejected before writes. Magazine PDFs are parsed before publication; encrypted, empty, or excessive documents are rejected. Images are validated and limited to 20 megapixels. Announcement attachment limits remain six; invalid attachments are skipped with explicit feedback. Cover rendering is limited to 10 million pixels, with a 2000-page PDF limit. Total uploads remain capped at 80 MB.

Backup downloads include `school.db` and uploaded files, use unique temporary paths, stream a ZIP from disk, and clean up on response close. Publishing file changes and backup creation share a lock within a single Waitress process. If deploying multiple application processes, pause publishing or use an externally coordinated filesystem/database snapshot for a consistent recovery copy. Download backups into a secure location outside the container.

To verify a restore, use a separate empty directory/container with the same code:

1. Extract the trusted backup, retaining `school.db` and `uploads/` together.
2. Point `SCHOOL_DATA_DIR` and `SCHOOL_UPLOAD_DIR` at the restored directories. Keep the production originals untouched.
3. With the restored server stopped, remove restored `auth_sessions` rows so old sessions are not reactivated.
4. Start Waitress and check staff sign-in, announcement attachments, magazine PDFs and covers before any production replacement.

The downloadable archive excludes the signing key. A new key can be generated on a separate restored instance; this signs out previous browser sessions. Restoring the database also restores the account password hashes in that backup.

## Interface and content

Essential links and forms work without JavaScript. Shared styles keep purple/gold school branding; service labels sit outside artwork, narrow screens use flexible single-column cards, and animation respects reduced-motion settings. Announcement search and category filters preserve each other's state. `/search` searches public announcement text and visible magazine issue titles (up to 20 results in each section); Google web search is a separate optional form.

Urdu reading uses bundled Noto Nastaliq Urdu with automatic text direction. The interface remains English. Font source: the Google Fonts `ofl/notonastaliqurdu` repository; copyright/license are in `static/fonts/OFL.txt` (SIL Open Font License 1.1). The shared Nextcloud login text has been removed from the public page; account credentials themselves were not changed. Service addresses are centralized in `portal/services.py`. The `hidden` magazine setting still means unlisted, not private; published files remain available by their URLs.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

Regression tests use disposable SQLite databases, files and synthetic accounts. They cover legacy migration/repeatability, reused account IDs, session revocation, login/POST CSRF, roles, search/filter state, older-content pagination, upload validation/cleanup, confirmation, and concurrent backup/restore integrity. They never read or mutate production data or credentials.

The cloud implementation was also checked using real Waitress and Chromium with synthetic data, at desktop/tablet/phone widths, with reduced motion and JavaScript disabled. Your live Proxmox/Nginx deployment, ERP and Nextcloud destinations require LAN validation after rollout.
