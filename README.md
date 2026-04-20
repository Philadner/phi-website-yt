# phi website yt

Standalone Vercel Python service for YouTube audio download/upload.

## Env vars

- `YTDL_SECRET`
- `BLOB_READ_WRITE_TOKEN`
- `REDIS_URL`

## Endpoint

- `GET /api/ytdl?videoId=...&secret=...`
- Also accepts `url`, `arg`, `q`, or `query`

## Response

Returns JSON with the uploaded Blob URL plus basic metadata.

## Runtime

- `api/ytdl.py` runs on Vercel Python
- Downloading is handled by `yt-dlp`
