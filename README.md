# Digital Archive

Private, AI-organised media archive (Flask + Neon Postgres + Cloudinary).

**Features** – username login, 2-step e-mail verification, forgot username / password (security question or Brevo OTP),
5-wrong-attempts → 15-minute lock, multi-file upload with image/video/PDF compression, AI auto-categories
(Naive Bayes + pixel features + optional free Gemini vision + OCR), natural-language search ("meri 10th ki marksheet"),
exact + near duplicate detection, per-account storage, 30-day trash auto-purge, light/dark icon toggle,
and a hidden animated admin panel.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env      # fill values
python app.py
pytest                    # tests use SQLite, no keys needed
```
Without `BREVO_API_KEY` (development only) OTP codes are printed in the console/flash message.

## Picture recognition (free, offline)
A MobileNetV2 model (~14 MB, `models/`) recognises animals, butterflies, food, scenery… and stores the words as labels so
searches like “butterfly” or “janwar ki photo” work. Optional extra: `GEMINI_API_KEY` for even richer captions.
Old uploads: *Smart Tools → Auto Categories → Re-scan all with AI*.

## Admin panel
On the normal login page enter `ADMIN_USERNAME` + `ADMIN_PASSWORD` → gateway (Login / Back / Forgot) → `ADMIN_PANEL_PASSWORD`.

## Deploy (Render free, 512 MB)
Push to GitHub → New Web Service (uses `render.yaml`) → set env vars → add `SITE_URL` and `CRON_SECRET` repo secrets
so `.github/workflows/purge.yml` pings `/cron/purge` every 6 hours (trash is deleted even if nobody logs in).
No torch/numpy – all ML is pure Python.
