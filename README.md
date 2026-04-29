# phi website yt

Standalone Vercel Python service for YouTube audio download/upload.

## Env vars

- `YTDL_SECRET`
- `BLOB_READ_WRITE_TOKEN`
- `REDIS_URL`
- `CRON_SECRET` (recommended; Vercel will send it as a Bearer token for cron requests)
- `YTDL_POT_PROVIDER_URL` (optional; external bgutil provider base URL such as `http://your-provider:4416`)
- `YTDL_POT_PROVIDER_SERVER_HOME` (optional; local bgutil server path for script mode)
- `YTDL_POT_TRACE` (optional; set to `true` for yt-dlp PO-token debug logging)
- `YTDL_COOKIES_B64` (optional; base64-encoded Netscape cookies.txt export for YouTube authentication)
- `YTDL_COOKIES` (optional; raw Netscape cookies.txt contents, useful for local testing)
- `YTDL_COOKIE_FILE` (optional; path to a Netscape cookies.txt file, useful for local testing)
- `FFMPEG_BINARY` (optional; path to `ffmpeg`, used to extract audio if YouTube only returns muxed video)
- `YTDL_JS_RUNTIME` (optional; JavaScript runtime for YouTube challenge solving, e.g. `node` or `node:/path/to/node`)

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
- `GET /api/pot-provider-ping` pings the provider's `/ping` endpoint so Vercel Cron can keep it warm

## YouTube Bot Check / Sign-in Errors

If YouTube returns `Sign in to confirm you're not a bot`, pass authenticated YouTube cookies to `yt-dlp`.

1. Export YouTube cookies from a browser in Netscape `cookies.txt` format.
2. Base64 encode the exported file.
3. Set the encoded value as `YTDL_COOKIES_B64` in Vercel.
4. Redeploy or restart the function.

For local testing, you can set `YTDL_COOKIE_FILE` to the exported file path instead.
