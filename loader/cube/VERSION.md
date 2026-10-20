Cube Core `v1.7.40` (pinned 2026-09-17, latest stable tag on Docker Hub `cubejs/cube` at that time — `latest`/`v1`/`v1.7` all resolved to the same build).

Running in `CUBEJS_DEV_MODE=true`, which disables API authentication entirely — fine for local-only use. Set `NODE_ENV=production` (and keep `CUBEJS_API_SECRET` in sync between `cube/.env` and the root `.env`) before exposing this beyond localhost.
