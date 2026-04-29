import base64
import binascii
import glob
import json
import os
import tempfile
import time
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

import redis
import requests
from flask import Flask, jsonify, request
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LOCK_TTL_SECONDS = 5 * 60
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024

app = Flask(__name__)


def json_response(status: int, payload: dict):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def query_value(key: str) -> Optional[str]:
    value = request.args.get(key)
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def extract_secret() -> str:
    query_secret = query_value("secret")
    if query_secret:
        return query_secret

    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def extract_input() -> str:
    for key in ("videoId", "url", "arg", "q", "query"):
        value = query_value(key)
        if value:
            if key == "videoId":
                return f"https://www.youtube.com/watch?v={value}"
            return value
    return ""


def extract_video_id(input_value: str, info: dict) -> str:
    if info.get("id"):
        return str(info["id"])

    parsed = urlparse(input_value)
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    if video_id:
        return video_id

    path = parsed.path.strip("/")
    if parsed.netloc.endswith("youtu.be") and path:
        return path

    return "unknown"


def make_blob_path(video_id: str, extension: str) -> str:
    return f"audio/{video_id}.{extension}"


def cache_key(video_id: str) -> str:
    return f"music:ytdl:video:{video_id}"


def lock_key(video_id: str) -> str:
    return f"music:ytdl:video:lock:{video_id}"


def request_id() -> str:
    return request.headers.get("x-vercel-id", "unknown-request-id")


def log_failure(stage: str, video_id: str, error: str) -> None:
    print(f"[ytdl][{request_id()}] {stage} failed for {video_id}: {error}", flush=True)


def cookie_config_source() -> Optional[str]:
    if os.getenv("YTDL_COOKIE_FILE", "").strip():
        return "file"
    if os.getenv("YTDL_COOKIES_B64", "").strip():
        return "base64"
    if os.getenv("YTDL_COOKIES", "").strip():
        return "raw"
    return None


def get_redis_connection():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    return redis.from_url(redis_url, decode_responses=True)


def get_cached_playback(video_id: str):
    connection = get_redis_connection()
    if connection is None:
        return None

    cached = connection.get(cache_key(video_id))
    if not cached:
        return None
    return json.loads(cached)


def set_cached_playback(video_id: str, value: dict) -> None:
    connection = get_redis_connection()
    if connection is None:
        return

    connection.setex(cache_key(video_id), CACHE_TTL_SECONDS, json.dumps(value))


def acquire_lock(video_id: str) -> bool:
    connection = get_redis_connection()
    if connection is None:
        return True

    return bool(connection.set(lock_key(video_id), "1", ex=LOCK_TTL_SECONDS, nx=True))


def release_lock(video_id: str) -> None:
    connection = get_redis_connection()
    if connection is None:
        return

    connection.delete(lock_key(video_id))


def upload_to_blob(pathname: str, content_type: str, body: bytes) -> dict:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN missing")

    url = f"https://vercel.com/api/blob/?pathname={quote(pathname, safe='')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-vercel-blob-access": "public",
        "x-add-random-suffix": "0",
        "x-allow-overwrite": "1",
        "x-content-type": content_type,
    }

    response = requests.put(url, headers=headers, data=body, timeout=120)
    if not response.ok:
        raise RuntimeError(f"Blob API request failed ({response.status_code}): {response.text}")

    return response.json()


def find_downloaded_file(temp_dir: str, video_id: str) -> Optional[str]:
    matches = [
        path
        for path in glob.glob(os.path.join(temp_dir, f"{video_id}.*"))
        if not path.endswith(".part")
    ]
    if not matches:
        return None
    matches.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return matches[0]


def is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_extractor_args() -> dict:
    extractor_args = {
        "youtube": {
            "player_client": ["default", "mweb"],
            "fetch_pot": ["auto"],
        }
    }

    if is_truthy(os.getenv("YTDL_POT_TRACE", "")):
        extractor_args["youtube"]["pot_trace"] = ["true"]

    provider_url = os.getenv("YTDL_POT_PROVIDER_URL", "").strip()
    if provider_url:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [provider_url]}

    provider_server_home = os.getenv("YTDL_POT_PROVIDER_SERVER_HOME", "").strip()
    if provider_server_home:
        extractor_args["youtubepot-bgutilscript"] = {"server_home": [provider_server_home]}

    return extractor_args


def build_cookie_file(temp_dir: str) -> Optional[str]:
    cookie_file = os.getenv("YTDL_COOKIE_FILE", "").strip()
    if cookie_file:
        return cookie_file

    encoded_cookies = os.getenv("YTDL_COOKIES_B64", "").strip()
    raw_cookies = os.getenv("YTDL_COOKIES", "").strip()
    if not encoded_cookies and not raw_cookies:
        return None

    if encoded_cookies:
        try:
            cookie_contents = base64.b64decode(encoded_cookies).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise RuntimeError(f"YTDL_COOKIES_B64 is not valid UTF-8 base64: {error}") from error
    else:
        cookie_contents = raw_cookies

    cookie_path = os.path.join(temp_dir, "youtube-cookies.txt")
    with open(cookie_path, "w", encoding="utf-8") as cookie_handle:
        cookie_handle.write(cookie_contents)
        if not cookie_contents.endswith("\n"):
            cookie_handle.write("\n")

    return cookie_path


def build_ydl_opts(temp_dir: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "cachedir": False,
        "nopart": True,
        "retries": 1,
        "extractor_retries": 1,
        "extractor_args": build_extractor_args(),
    }

    cookie_file = build_cookie_file(temp_dir)
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


def debug_payload():
    return json_response(
        200,
        {
            "ok": True,
            "hasCookieConfig": cookie_config_source() is not None,
            "cookieSource": cookie_config_source(),
            "hasPotProviderUrl": bool(os.getenv("YTDL_POT_PROVIDER_URL", "").strip()),
            "hasPotProviderServerHome": bool(os.getenv("YTDL_POT_PROVIDER_SERVER_HOME", "").strip()),
            "potTrace": is_truthy(os.getenv("YTDL_POT_TRACE", "")),
        },
    )


@app.get("/api/ytdl")
@app.get("/")
def handler():
    if request.method != "GET":
        return json_response(405, {"error": "Method not allowed"})

    configured_secret = os.getenv("YTDL_SECRET", "").strip()
    if not configured_secret:
        return json_response(500, {"error": "YTDL_SECRET missing"})

    if extract_secret() != configured_secret:
        return json_response(401, {"error": "Unauthorised"})

    if is_truthy(query_value("debug") or ""):
        return debug_payload()

    input_value = extract_input()
    if not input_value:
        return json_response(
            400,
            {"error": "One input argument is required via videoId, url, arg, q, or query"},
        )

    try:
        with YoutubeDL(build_ydl_opts(tempfile.gettempdir())) as probe:
            info = probe.extract_info(input_value, download=False)
    except DownloadError as error:
        return json_response(
            400,
            {
                "error": "Invalid YouTube input",
                "message": str(error),
            },
        )

    video_id = extract_video_id(input_value, info)
    video_url = info.get("webpage_url") or info.get("original_url") or input_value

    cached = get_cached_playback(video_id)
    if cached:
        return json_response(
            200,
            {
                "ok": True,
                "runtime": "python",
                "source": "cache",
                "input": input_value,
                "videoId": cached["video_id"],
                "videoUrl": video_url,
                "blob": {
                    "url": cached["playback_url"],
                    "downloadUrl": cached["playback_url"],
                    "pathname": cached.get("pathname"),
                    "contentType": cached["mime_type"],
                },
                "cachedAt": cached["cached_at"],
            },
        )

    if not acquire_lock(video_id):
        return json_response(
            202,
            {
                "ok": False,
                "status": "preparing",
                "videoId": video_id,
                "videoUrl": video_url,
            },
        )

    try:
        cached = get_cached_playback(video_id)
        if cached:
            return json_response(
                200,
                {
                    "ok": True,
                    "runtime": "python",
                    "source": "cache",
                    "input": input_value,
                    "videoId": cached["video_id"],
                    "videoUrl": video_url,
                    "blob": {
                        "url": cached["playback_url"],
                        "downloadUrl": cached["playback_url"],
                        "pathname": cached.get("pathname"),
                        "contentType": cached["mime_type"],
                    },
                    "cachedAt": cached["cached_at"],
                },
            )

        declared_size = info.get("filesize") or info.get("filesize_approx")
        if isinstance(declared_size, int) and declared_size > MAX_DOWNLOAD_BYTES:
            return json_response(
                413,
                {
                    "error": "Audio stream too large for Vercel function",
                    "message": (
                        f"Selected stream is {declared_size // 1024 // 1024} MB; "
                        f"function limit is {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB"
                    ),
                    "videoId": video_id,
                    "videoUrl": video_url,
                },
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                with YoutubeDL(build_ydl_opts(temp_dir)) as ydl:
                    ydl.download([input_value])
            except DownloadError as error:
                log_failure("stream_download", video_id, str(error))
                return json_response(
                    503,
                    {
                        "error": "yt-dlp download failed",
                        "message": str(error),
                        "videoId": video_id,
                        "videoUrl": video_url,
                        "requestId": request_id(),
                    },
                )

            downloaded_path = find_downloaded_file(temp_dir, video_id)
            if not downloaded_path:
                log_failure("stream_download", video_id, "downloaded file not found")
                return json_response(
                    503,
                    {
                        "error": "yt-dlp download failed",
                        "message": "Downloaded file not found",
                        "videoId": video_id,
                        "videoUrl": video_url,
                        "requestId": request_id(),
                    },
                )

            body_size = os.path.getsize(downloaded_path)
            if body_size > MAX_DOWNLOAD_BYTES:
                return json_response(
                    413,
                    {
                        "error": "Audio stream too large for Vercel function",
                        "message": (
                            f"Selected stream is {body_size // 1024 // 1024} MB; "
                            f"function limit is {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB"
                        ),
                        "videoId": video_id,
                        "videoUrl": video_url,
                    },
                )

            extension = os.path.splitext(downloaded_path)[1].lstrip(".") or "bin"
            content_type = {
                "m4a": "audio/mp4",
                "mp4": "audio/mp4",
                "webm": "audio/webm",
                "mp3": "audio/mpeg",
                "opus": "audio/ogg",
                "ogg": "audio/ogg",
            }.get(extension, "application/octet-stream")
            blob_path = make_blob_path(video_id, extension)

            with open(downloaded_path, "rb") as downloaded_file:
                body = downloaded_file.read()

        try:
            blob = upload_to_blob(blob_path, content_type, body)
        except Exception as error:
            log_failure("blob_upload", video_id, str(error))
            return json_response(
                503,
                {
                    "error": "Blob upload failed",
                    "message": str(error),
                    "videoId": video_id,
                    "videoUrl": video_url,
                    "pathname": blob_path,
                    "requestId": request_id(),
                },
            )

        cached = {
            "video_id": video_id,
            "playback_url": blob["url"],
            "pathname": blob.get("pathname"),
            "mime_type": blob.get("contentType") or content_type,
            "cached_at": str(int(time.time())),
        }
        set_cached_playback(video_id, cached)

        return json_response(
            200,
            {
                "ok": True,
                "runtime": "python",
                "source": "download",
                "input": input_value,
                "videoId": video_id,
                "videoUrl": video_url,
                "blob": {
                    "url": blob["url"],
                    "downloadUrl": blob.get("downloadUrl", blob["url"]),
                    "pathname": blob.get("pathname"),
                    "contentType": blob.get("contentType") or content_type,
                },
                "cachedAt": cached["cached_at"],
            },
        )
    finally:
        release_lock(video_id)
