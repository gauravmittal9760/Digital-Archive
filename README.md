# Digital Archive

Private personal media archive built with Flask, SQLAlchemy, Neon PostgreSQL, Cloudinary and local-first media intelligence.

## Features
- Username/password accounts, optional email 2-step verification, account recovery and lockout protection.
- Photos, videos and documents; albums, favorites, timeline, trash, version history and expiring share links.
- Per-user storage accounting, duplicate detection, ZIP export and Cloudinary-backed private storage.
- Local MobileNetV2 image recognition (ONNX Runtime), text-based classification and optional Gemini vision.
- Optional local Tesseract OCR for English (and Hindi if `hin.traineddata` is installed).
- Smart search, admin dashboard, activity history and scheduled trash cleanup.
- Installable PWA shell and **On This Day / Memory Rewind**. The service worker caches only public static assets and an offline notice; it never caches private pages or media.

## Local setup (Windows / macOS / Linux)
```bash
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # Windows CMD; on macOS/Linux use: cp .env.example .env
python app.py
```

Edit `.env` and set `SECRET_KEY`. For local development, SQLite and local file storage are used automatically when Cloudinary is not configured. Add Cloudinary and Brevo credentials only when you want those integrations.

### Tesseract OCR
Install the Tesseract executable separately from the Python packages. Set `ENABLE_OCR=true`. If the executable is not on PATH, set `TESSERACT_CMD` to its full path, for example `C:\Program Files\Tesseract-OCR\tesseract.exe`. English OCR uses `eng`; Hindi is optional and requires `hin.traineddata` in the Tesseract `tessdata` folder. `pytesseract` is not required because this project calls the executable directly.

### Tests
```bash
pytest -q
```
Tests use SQLite and do not require live Cloudinary, Brevo or Gemini credentials. ONNX tests are skipped if ONNX Runtime is unavailable.

## Render deployment
The included `render.yaml` installs Tesseract English data and installs Python dependencies. Set `DATABASE_URL` to your Neon PostgreSQL URL, Cloudinary credentials, `PUBLIC_BASE_URL`, Brevo credentials, strong admin credentials and `CRON_SECRET` in Render. Keep `APP_ENV=production`. The service requires persistent external services: local disk is not durable on Render.

The GitHub Actions workflow can call `/cron/purge` using `SITE_URL` and `CRON_SECRET` repository secrets.

## Important privacy behaviour
- User pages and media are never cached by the PWA service worker. Offline mode shows a generic offline notice only.
- Local OCR runs on the server where Tesseract is installed. Optional Gemini analysis sends downsized images to Google's API only when configured and enabled by the user.
- Never commit `.env`, API keys, database URLs or production credentials.

## New in this version
- **Private Vault** (`/vault`): separate passphrase, AES-256-GCM envelope encryption, recovery key, 10-minute auto-lock, 20 MB per file. Vault files are hidden from library/search/shares. Lose the passphrase *and* recovery key = files are unrecoverable. Needs the `cryptography` package (in requirements.txt). Unlocked-vault sessions are stored in the database (data key encrypted with a key derived from `SECRET_KEY`, 10-minute sliding expiry, deleted on lock), so several workers/instances share them safely. Keep `SECRET_KEY` identical on every instance.
- **Expiry tracker + e-mail reminders**: expiry dates are read from document text (passport, insurance, warranty…), editable on the file page; reminders go out 30/7/1 days before (toggle in Settings → Privacy). Runs from the background loop and from `/cron/purge`.
- **Smart album suggestions** (trips/events and big categories, one click) and **Storage optimizer** tips on the Storage page.
- **Ask Your Archive** (`/ask`), **Review queue** (`/review`), **share-link passwords / view-only / access history**.
- Reliability: atomic OTP cooldown, persistent AI-job resume, faster duplicate detection, storage-quota check on replace, DB indexes, stale-while-revalidate service worker, CI workflow (`.github/workflows/tests.yml`).

## Security notes
- **Media URLs expire.** Photos/thumbnails/avatars are served from `/dl/<token>`; each token is HMAC-signed and valid for 30-60 minutes (`MEDIA_URL_TTL_MINUTES`, default 30). Within a window the URL is identical, so browser caching still works. Images stream through the app; videos redirect to a 5-minute Cloudinary download URL. Documents and vault files are never public.
- **Deletions are crash-safe.** Vault erase (and vault file delete) commit the database change and a `storage_cleanup` job in one transaction; the background job/`/cron/purge` retries Cloudinary deletions until they succeed, so the DB never points at a deleted file and nothing is orphaned.
- **Quota is race-free.** The final quota check runs in the same transaction as the insert, serialised per user (process lock + PostgreSQL advisory lock).
