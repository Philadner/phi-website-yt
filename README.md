# phi website yt

Standalone Vercel Python service for YouTube audio download/upload.

## Env vars

- `YTDL_SECRET`
- `BLOB_READ_WRITE_TOKEN`
- `REDIS_URL`
- `YTDL_POT_PROVIDER_URL` (optional; external bgutil provider base URL such as `http://your-provider:4416`)
- `YTDL_POT_PROVIDER_SERVER_HOME` (optional; local bgutil server path for script mode)
- `YTDL_POT_TRACE` (optional; set to `true` for yt-dlp PO-token debug logging)

## Endpoint

- `GET /api/ytdl?videoId=...&secret=...`
- Also accepts `url`, `arg`, `q`, or `query`

## Response

Returns JSON with the uploaded Blob URL plus basic metadata.

## Runtime

- `api/ytdl.py` runs on Vercel Python
- Downloading is handled by `yt-dlp`
- Dependencies are managed with `uv` via `pyproject.toml` and `uv.lock`
- `vercel.json` excludes `target/` and other non-runtime files from the Python function bundle

## YouTube PO Tokens

- The app is configured for `yt-dlp`'s `mweb` client with PO-token auto-fetch enabled
- The repo installs `bgutil-ytdlp-pot-provider`
- On Vercel, the recommended setup is to run the bgutil provider as a separate HTTP service and set `YTDL_POT_PROVIDER_URL`
