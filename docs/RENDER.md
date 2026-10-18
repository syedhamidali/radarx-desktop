# Deploying the web version on Render

Two services, defined in `render.yaml` at the repo root:

| Service | What | Notes |
|---|---|---|
| `xradar-engine` | Docker web service running `python server.py --web` | WebSocket + `/health` + download links. Needs >= 2 GB RAM (the `standard` plan). |
| `xradar-web` | Static site built with `npm run build:web` | Talks to the engine over `wss://`. |

## Steps

1. Push the repo to GitHub (already done) and in Render choose **New -> Blueprint**, pick the repo.
2. Render creates both services. Two values cannot be known in advance, so Render asks for them:
   - on **xradar-web**: `VITE_ENGINE_URL` = the engine's address with `wss://`, e.g. `wss://xradar-engine.onrender.com`
   - on **xradar-engine**: `XRADAR_ALLOWED_ORIGINS` = the static site's address, e.g. `https://xradar-web.onrender.com`
3. Deploy both. Open the static site's URL.

The first request after the engine has been idle can take 30-60 s (cold start on small plans); the app
shows an "Engine waking up" banner meanwhile.

## What the public can and cannot do

Web mode (`XRADAR_WEB=1`) is built for anonymous visitors: they can upload a file (size-limited), open the latest NEXRAD
scan from the public AWS archive, run the modules and download the results. They cannot open files that are on the
server, and each visitor's files live in a private temporary folder that is deleted when they leave.
Limits are environment variables (see `docs/WEB.md`): `XRADAR_MAX_SESSIONS`, `XRADAR_MAX_UPLOAD_MB`,
`XRADAR_MAX_OPEN_FILES`, `XRADAR_MODULE_TIMEOUT_S`, `XRADAR_IDLE_TIMEOUT_S`.

## Cost and sizing

The engine is the expensive part: memory is roughly 300 MB per open volume. With `XRADAR_MAX_SESSIONS=6` and
`XRADAR_MAX_OPEN_FILES=2` a 2 GB instance is a sensible start. Lower the limits before you lower the plan.

## Try it locally

```bash
docker build -t xradar-engine python/
docker run --rm -p 10000:10000 -e XRADAR_ALLOWED_ORIGINS='*' xradar-engine
VITE_ENGINE_URL=ws://localhost:10000 npm run dev:web
```
