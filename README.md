# phi website yt

Standalone Vercel Rust service for YouTube audio download/upload.

## Env vars

- `YTDL_SECRET`
- `BLOB_READ_WRITE_TOKEN`
- `REDIS_URL`
- `YTDL_PO_TOKEN` (optional; passed to `rusty_ytdl` `RequestOptions.po_token`)
- `YTDL_COOKIES` (optional; YouTube cookies string)

## Endpoint

- `GET /api/ytdl?videoId=...&secret=...`
- Also accepts `url`, `arg`, `q`, or `query`

## Response

Returns JSON with the uploaded Blob URL plus basic metadata.

## Getting a `YTDL_PO_TOKEN`

Short version: generate/capture it in a logged-in browser session, then paste into Vercel env vars.

1. Open a private/incognito browser window and sign into YouTube.
2. Open DevTools → Network.
3. Play the target video.
4. Find either:
   - a `youtubei/v1/player` request containing `serviceIntegrityDimensions.poToken`, or
   - a `videoplayback` request URL containing a `pot=` query parameter.
5. Copy the token value and set:
   - `YTDL_PO_TOKEN=<that token>`
   - (recommended) `YTDL_COOKIES=<cookie header string from same session>`
6. Redeploy.

Notes:
- Tokens can expire or be tied to session/client context, so refresh when downloads start failing again.
- The upstream reference guide is yt-dlp’s PO token guide:
  https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
