# Digital Archive repair notes

## Changes in this build
- Corrected storage quota estimation to use the processed upload payload and thumbnail rather than a fixed 90% estimate.
- Login history and `last_login_at` are now recorded only after a login is fully completed, including successful optional 2-step verification.
- Added bounded Office ZIP text extraction to reduce decompression-bomb risk.
- Improved Tesseract executable discovery through `TESSERACT_CMD`; OCR selects English automatically when Hindi data is unavailable.
- Added local and Render OCR setup guidance and a complete `.env.example`.
- Added an installable PWA manifest, app icons, install prompt, service worker and generic offline page. The service worker caches only public static assets and never private HTML/media.
- Added the private-account-only On This Day / Memory Rewind page.
- Removed Pillow `getdata()` deprecation warnings by using `get_flattened_data()`.
- Added tests for PWA endpoints, Memory Rewind and completed-login accounting.
- Removed virtual-environment and Python cache files from the deliverable archive.

## Validation performed
- Python compileall: passed.
- JavaScript syntax checks: passed.
- Jinja template compilation: passed.
- PWA manifest JSON parsing: passed.
- Render YAML parsing: passed.
- Pytest: 13 passed, 1 skipped.

The ONNX Runtime integration test was skipped in this Linux review environment because the ZIP contains a Windows virtual environment and the Linux environment does not have a compatible ONNX Runtime binary. Live Neon, Cloudinary, Brevo, Gemini, Tesseract-on-Render and production deployment checks require the user's own credentials/host and were not performed.
