import base64
import binascii
import glob
import hashlib
import hmac
import importlib.metadata
import json
import os
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from typing import Optional
from urllib.parse import parse_qs, urlparse

import redis
import requests
from flask import Flask, Response, jsonify, redirect, request, stream_with_context
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LOCK_TTL_SECONDS = 5 * 60
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
STARTER_CHUNK_BYTES = 64 * 1024
STARTER_MAX_FUTURE_SECONDS = 5 * 60

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


def verify_starter_signature(video_id: str, expires: str, signature: str) -> bool:
    configured_secret = os.getenv("YTDL_SECRET", "").strip()
    if not configured_secret or not video_id or not expires or not signature:
        return False

    try:
        expires_at = int(expires)
    except ValueError:
        return False

    current_time = int(time.time())
    if expires_at < current_time or expires_at > current_time + STARTER_MAX_FUTURE_SECONDS:
        return False

    expected = hmac.new(
        configured_secret.encode("utf-8"),
        f"{video_id}:{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


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


def log_event(stage: str, **fields) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if value is not None and key not in {"secret", "cookie", "cookies", "authorization"}
    }
    print(
        json.dumps(
            {
                "service": "ytdl",
                "stage": stage,
                "requestId": request_id(),
                **safe_fields,
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )


def log_failure(stage: str, video_id: str, error: str, **fields) -> None:
    log_event(stage, ok=False, videoId=video_id, error=error, **fields)


class YtdlLogger:
    def __init__(self, stage: str):
        self.stage = stage

    def debug(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        log_event("yt_dlp_warning", ok=False, ytdlStage=self.stage, message=message)

    def error(self, message: str) -> None:
        log_event("yt_dlp_error", ok=False, ytdlStage=self.stage, message=message)


def cookie_config_source() -> Optional[str]:
    if os.getenv("YTDL_COOKIE_FILE", "").strip():
        return "file"
    if os.getenv("YTDL_COOKIES_B64", "").strip():
        return "base64"
    if os.getenv("YTDL_COOKIES", "").strip():
        return "raw"
    return None


@lru_cache(maxsize=1)
def get_redis_connection():
    redis_url = (
        os.getenv("UPSTASH_REDIS_KV_REDIS_URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
    )
    if not redis_url:
        return None
    return redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def get_cached_playback(video_id: str):
    connection = get_redis_connection()
    if connection is None:
        return None

    try:
        cached = connection.get(cache_key(video_id))
        if not cached:
            return None
        return json.loads(cached)
    except Exception as error:
        log_failure("redis_cache_get", video_id, str(error))
        return None


def set_cached_playback(video_id: str, value: dict) -> None:
    connection = get_redis_connection()
    if connection is None:
        return

    try:
        connection.setex(cache_key(video_id), CACHE_TTL_SECONDS, json.dumps(value))
    except Exception as error:
        log_failure("redis_cache_set", video_id, str(error))


def acquire_lock(video_id: str) -> bool:
    connection = get_redis_connection()
    if connection is None:
        return True

    try:
        return bool(connection.set(lock_key(video_id), "1", ex=LOCK_TTL_SECONDS, nx=True))
    except Exception as error:
        log_failure("redis_lock_acquire", video_id, str(error))
        return True


def release_lock(video_id: str) -> None:
    connection = get_redis_connection()
    if connection is None:
        return

    try:
        connection.delete(lock_key(video_id))
    except Exception as error:
        log_failure("redis_lock_release", video_id, str(error))


def cached_playback_response(cached: dict, input_value: str, video_url: str):
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


def get_blob_playback(video_id: str):
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        return None

    try:
        from vercel.blob import BlobClient

        with BlobClient(token=token) as client:
            result = client.list_objects(prefix=f"audio/{video_id}.", limit=1)
            if not result.blobs:
                return None

            blob = result.blobs[0]
            details = client.head(blob.url)
            return {
                "video_id": video_id,
                "playback_url": blob.url,
                "pathname": blob.pathname,
                "mime_type": details.content_type or "audio/mpeg",
                "cached_at": str(int(blob.uploaded_at.timestamp())),
            }
    except Exception as error:
        log_failure("blob_cache_lookup", video_id, str(error))
        return None


def upload_to_blob(pathname: str, content_type: str, body: bytes) -> dict:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN missing")

    from vercel.blob import BlobClient

    with BlobClient(token=token) as client:
        blob = client.put(
            pathname,
            body,
            access="public",
            content_type=content_type,
            add_random_suffix=False,
            overwrite=True,
        )

    return {
        "url": blob.url,
        "downloadUrl": getattr(blob, "download_url", None) or getattr(blob, "downloadUrl", None) or blob.url,
        "pathname": blob.pathname,
        "contentType": getattr(blob, "content_type", None) or getattr(blob, "contentType", None),
    }


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


def build_extractor_args(player_clients: Optional[list[str]] = None) -> dict:
    extractor_args = {}
    youtube_args = {}

    if player_clients:
        youtube_args["player_client"] = player_clients

    if is_truthy(os.getenv("YTDL_POT_TRACE", "")):
        youtube_args["pot_trace"] = ["true"]

    if youtube_args:
        extractor_args["youtube"] = youtube_args

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

    cookie_contents = normalise_cookie_contents(cookie_contents)
    cookie_path = os.path.join(temp_dir, "youtube-cookies.txt")
    with open(cookie_path, "w", encoding="utf-8") as cookie_handle:
        cookie_handle.write(cookie_contents)
        if not cookie_contents.endswith("\n"):
            cookie_handle.write("\n")

    return cookie_path


def normalise_cookie_contents(cookie_contents: str) -> str:
    lines = []
    for line in cookie_contents.splitlines():
        if not line or line.startswith("#"):
            lines.append(line)
            continue
        if len(line.split("\t")) == 7:
            lines.append(line)

    return "\n".join(lines)


def build_ydl_opts(
    temp_dir: str,
    *,
    format_selector: Optional[str] = None,
    log_stage: str = "yt_dlp",
    player_clients: Optional[list[str]] = None,
):
    js_runtimes = discover_js_runtimes()
    opts = {
        "quiet": True,
        "no_warnings": False,
        "logger": YtdlLogger(log_stage),
        "noplaylist": True,
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "cachedir": False,
        "nopart": True,
        "retries": 1,
        "extractor_retries": 1,
        "source_address": "0.0.0.0",
        "extractor_args": build_extractor_args(player_clients),
    }

    if js_runtimes:
        opts["js_runtimes"] = js_runtimes

    if format_selector:
        opts["format"] = format_selector

    cookie_file = build_cookie_file(temp_dir)
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


def format_summary(info: dict) -> dict:
    formats = info.get("formats") or []
    downloadable = [item for item in formats if item.get("url")]
    audio = [
        item
        for item in downloadable
        if item.get("acodec") not in (None, "none") and item.get("vcodec") in (None, "none")
    ]
    muxed = [
        item
        for item in downloadable
        if item.get("acodec") not in (None, "none") and item.get("vcodec") not in (None, "none")
    ]

    return {
        "total": len(formats),
        "downloadable": len(downloadable),
        "audioOnly": len(audio),
        "muxed": len(muxed),
        "sample": [
            {
                "formatId": item.get("format_id"),
                "ext": item.get("ext"),
                "acodec": item.get("acodec"),
                "vcodec": item.get("vcodec"),
                "filesize": item.get("filesize") or item.get("filesize_approx"),
            }
            for item in (audio or muxed or downloadable)[:5]
        ],
    }


def select_starter_format(info: dict):
    candidates = []
    for item in info.get("formats") or []:
        size = item.get("filesize") or item.get("filesize_approx")
        protocol = str(item.get("protocol") or "")
        if not isinstance(size, (int, float)) or size <= 0 or size > MAX_DOWNLOAD_BYTES:
            continue
        if not item.get("url") or protocol not in {"http", "https"}:
            continue
        if item.get("acodec") in (None, "none") or item.get("vcodec") not in (None, "none"):
            continue
        candidates.append(item)

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: (
            float(item.get("abr") or item.get("tbr") or 0),
            int(item.get("filesize") or 0),
        ),
    )


def content_type_for_extension(extension: str) -> str:
    return {
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "webm": "audio/webm",
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "ogg": "audio/ogg",
    }.get(extension.lower(), "application/octet-stream")


def open_starter_upstream(input_value: str, video_id: str):
    attempts = (
        ("web_embedded", ["web_embedded"]),
        ("default", None),
        ("web_creator", ["web_creator"]),
    )
    last_error = None

    for attempt_name, player_clients in attempts:
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                with YoutubeDL(
                    build_ydl_opts(
                        temp_dir,
                        format_selector="all",
                        log_stage=f"starter_{attempt_name}",
                        player_clients=player_clients,
                    )
                ) as probe:
                    info = probe.extract_info(input_value, download=False)

            selected = select_starter_format(info)
            if not selected:
                raise RuntimeError("No supported audio-only format is available")

            upstream = requests.get(
                selected["url"],
                headers=selected.get("http_headers") or {},
                stream=True,
                timeout=(5, 30),
            )
            upstream.raise_for_status()

            content_length = int(
                upstream.headers.get("Content-Length") or selected.get("filesize") or 0
            )
            if content_length <= 0 or content_length > MAX_DOWNLOAD_BYTES:
                upstream.close()
                raise RuntimeError("Starter stream exceeds the service download limit")

            log_event(
                "starter_open",
                ok=True,
                videoId=video_id,
                attempt=attempt_name,
                formatId=selected.get("format_id"),
                bodySize=content_length,
                extension=selected.get("ext"),
            )
            return upstream, selected, content_length
        except Exception as error:
            last_error = error
            log_failure(
                "starter_open_attempt",
                video_id,
                str(error),
                attempt=attempt_name,
            )

    raise RuntimeError(str(last_error or "Starter stream unavailable"))


def starter_stream_response():
    video_id = query_value("videoId") or ""
    expires = query_value("expires") or ""
    signature = query_value("signature") or ""
    if not verify_starter_signature(video_id, expires, signature):
        return json_response(401, {"error": "Invalid or expired starter signature"})

    cached = get_cached_playback(video_id)
    if not cached:
        cached = get_blob_playback(video_id)
        if cached:
            set_cached_playback(video_id, cached)
    if cached:
        return redirect(cached["playback_url"], code=302)

    input_value = f"https://www.youtube.com/watch?v={video_id}"
    try:
        upstream, selected, content_length = open_starter_upstream(input_value, video_id)
    except Exception as error:
        log_failure("starter_open", video_id, str(error))
        return json_response(
            503,
            {
                "error": "Starter stream unavailable",
                "message": str(error),
                "videoId": video_id,
                "requestId": request_id(),
            },
        )

    def generate():
        bytes_sent = 0
        try:
            for chunk in upstream.iter_content(chunk_size=STARTER_CHUNK_BYTES):
                if not chunk:
                    continue
                bytes_sent += len(chunk)
                yield chunk
        except Exception as error:
            log_failure("starter_stream", video_id, str(error), bytesSent=bytes_sent)
        finally:
            upstream.close()
            log_event("starter_complete", ok=True, videoId=video_id, bytesSent=bytes_sent)

    extension = str(selected.get("ext") or "bin")
    response = Response(
        stream_with_context(generate()),
        status=200,
        content_type=content_type_for_extension(extension),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Length"] = str(content_length)
    response.headers["Accept-Ranges"] = "none"
    response.headers["X-Starter-Format"] = str(selected.get("format_id") or "unknown")
    return response


def audio_format_selector(formats: dict) -> str:
    if formats["audioOnly"] > 0:
        return (
            "bestaudio[filesize<30M]/bestaudio[filesize_approx<30M]/bestaudio/"
            "best[acodec!=none][vcodec!=none][filesize<30M]/"
            "best[acodec!=none][vcodec!=none][filesize_approx<30M]/"
            "best[acodec!=none][vcodec!=none]"
        )
    return "best[acodec!=none][vcodec!=none][filesize<30M]/best[acodec!=none][vcodec!=none][filesize_approx<30M]/best[acodec!=none][vcodec!=none]"


def has_audio_only_format(formats: dict) -> bool:
    return formats["audioOnly"] > 0


def get_ffmpeg_binary() -> Optional[str]:
    configured = os.getenv("FFMPEG_BINARY", "").strip()
    if configured:
        return configured
    return shutil.which("ffmpeg")


def discover_js_runtimes() -> dict:
    configured = os.getenv("YTDL_JS_RUNTIME", "").strip()
    if configured:
        if ":" in configured:
            name, path = configured.split(":", 1)
            return {name.strip(): {"path": path.strip()}}
        return {configured: {}}

    bundled_node = get_bundled_node_binary()
    if bundled_node:
        return {"node": {"path": bundled_node}}

    for name in ("deno", "node", "bun", "quickjs"):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}

    return {}


def get_bundled_node_binary() -> Optional[str]:
    try:
        import nodejs_wheel.executable as nodejs_executable
    except Exception:
        return None

    binary_name = "node.exe" if os.name == "nt" else "node"
    bin_dir = (
        nodejs_executable.ROOT_DIR
        if os.name == "nt"
        else os.path.join(nodejs_executable.ROOT_DIR, "bin")
    )
    node_path = os.path.join(bin_dir, binary_name)
    if os.path.exists(node_path):
        return node_path
    return None


def js_runtime_summary(runtimes: Optional[dict] = None) -> dict:
    values = runtimes if runtimes is not None else discover_js_runtimes()
    return {
        "configured": bool(os.getenv("YTDL_JS_RUNTIME", "").strip()),
        "selected": list(values.keys()),
        "bundledNode": get_bundled_node_binary() is not None,
        "available": {
            "deno": shutil.which("deno") is not None,
            "node": shutil.which("node") is not None,
            "bun": shutil.which("bun") is not None,
            "quickjs": shutil.which("quickjs") is not None,
        },
    }


def package_versions() -> dict:
    versions = {}
    for package in ("yt-dlp", "yt-dlp-ejs", "nodejs-wheel-binaries", "vercel"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def pot_provider_status() -> Optional[dict]:
    provider_url = os.getenv("YTDL_POT_PROVIDER_URL", "").strip()
    if not provider_url:
        return None

    try:
        response = requests.get(provider_url.rstrip("/") + "/ping", timeout=5)
        return {
            "ok": response.ok,
            "status": response.status_code,
        }
    except Exception as error:
        return {
            "ok": False,
            "error": str(error),
        }


def extract_audio_from_video(input_path: str) -> str:
    ffmpeg = get_ffmpeg_binary()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to extract audio from muxed video formats")

    extension = os.path.splitext(input_path)[1].lower()
    if extension == ".webm":
        output_path = os.path.splitext(input_path)[0] + ".audio.webm"
    else:
        output_path = os.path.splitext(input_path)[0] + ".audio.m4a"

    copy_result = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, "-vn", "-c:a", "copy", output_path],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if copy_result.returncode == 0:
        return output_path

    mp3_path = os.path.splitext(input_path)[0] + ".audio.mp3"
    transcode_result = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, "-vn", "-b:a", "128k", mp3_path],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if transcode_result.returncode != 0:
        message = transcode_result.stderr or copy_result.stderr or "ffmpeg audio extraction failed"
        raise RuntimeError(message[-1000:])

    return mp3_path


def debug_payload(input_value: Optional[str] = None):
    payload = {
        "ok": True,
        "hasCookieConfig": cookie_config_source() is not None,
        "cookieSource": cookie_config_source(),
        "hasPotProviderUrl": bool(os.getenv("YTDL_POT_PROVIDER_URL", "").strip()),
        "hasPotProviderServerHome": bool(os.getenv("YTDL_POT_PROVIDER_SERVER_HOME", "").strip()),
        "potProvider": pot_provider_status(),
        "hasFfmpeg": get_ffmpeg_binary() is not None,
        "jsRuntime": js_runtime_summary(),
        "packages": package_versions(),
        "potTrace": is_truthy(os.getenv("YTDL_POT_TRACE", "")),
    }

    if input_value:
        try:
            with YoutubeDL(
                build_ydl_opts(tempfile.gettempdir(), format_selector="all", log_stage="debug_probe")
            ) as probe:
                info = probe.extract_info(input_value, download=False)
            formats = format_summary(info)
            payload["probe"] = {
                "ok": True,
                "videoId": extract_video_id(input_value, info),
                "videoUrl": info.get("webpage_url") or info.get("original_url") or input_value,
                "title": info.get("title"),
                "formats": formats,
                "selectedFormat": audio_format_selector(formats)
                if formats["audioOnly"] > 0 or formats["muxed"] > 0
                else None,
                "willExtractAudio": formats["audioOnly"] == 0 and formats["muxed"] > 0,
            }
        except DownloadError as error:
            payload["probe"] = {
                "ok": False,
                "error": "Probe failed",
                "message": str(error),
            }

    return json_response(
        200,
        payload,
    )


@app.get("/api/ytdl")
@app.get("/api/ytdl-stream")
@app.get("/")
def handler():
    if request.method != "GET":
        return json_response(405, {"error": "Method not allowed"})

    configured_secret = os.getenv("YTDL_SECRET", "").strip()
    if not configured_secret:
        return json_response(500, {"error": "YTDL_SECRET missing"})

    if request.path == "/api/ytdl-stream" or is_truthy(query_value("stream") or ""):
        return starter_stream_response()

    if extract_secret() != configured_secret:
        return json_response(401, {"error": "Unauthorised"})

    if is_truthy(query_value("debug") or ""):
        return debug_payload(extract_input())

    input_value = extract_input()
    if not input_value:
        return json_response(
            400,
            {"error": "One input argument is required via videoId, url, arg, q, or query"},
        )

    requested_video_id = extract_video_id(input_value, {})
    if requested_video_id != "unknown":
        cached = get_cached_playback(requested_video_id)
        if not cached:
            cached = get_blob_playback(requested_video_id)
            if cached:
                set_cached_playback(requested_video_id, cached)
        if cached:
            return cached_playback_response(cached, input_value, input_value)

    try:
        with YoutubeDL(build_ydl_opts(tempfile.gettempdir(), format_selector="all", log_stage="probe")) as probe:
            info = probe.extract_info(input_value, download=False)
    except DownloadError as error:
        log_failure("probe", "unknown", str(error), input=input_value)
        return json_response(
            400,
            {
                "error": "Invalid YouTube input",
                "message": str(error),
                "requestId": request_id(),
            },
        )

    video_id = extract_video_id(input_value, info)
    video_url = info.get("webpage_url") or info.get("original_url") or input_value
    formats = format_summary(info)
    selected_format = audio_format_selector(formats) if formats["audioOnly"] > 0 or formats["muxed"] > 0 else None
    log_event(
        "probe",
        ok=True,
        videoId=video_id,
        title=info.get("title"),
        formats=formats,
        selectedFormat=selected_format,
        willExtractAudio=formats["audioOnly"] == 0 and formats["muxed"] > 0,
        cookieSource=cookie_config_source(),
        hasFfmpeg=get_ffmpeg_binary() is not None,
        potProvider=pot_provider_status(),
        jsRuntime=js_runtime_summary(),
        packages=package_versions(),
    )
    if formats["audioOnly"] == 0 and formats["muxed"] == 0:
        log_failure("format_selection", video_id, "no playable formats", formats=formats)
        return json_response(
            422,
            {
                "error": "No playable formats available",
                "message": "YouTube did not return a downloadable audio-only or muxed audio/video stream for this video.",
                "videoId": video_id,
                "videoUrl": video_url,
                "formats": formats,
            },
        )

    cached = get_cached_playback(video_id)
    if cached:
        return cached_playback_response(cached, input_value, video_url)

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
            return cached_playback_response(cached, input_value, video_url)

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
            downloaded_path = None
            download_error = None
            download_attempts = (
                ("web_creator", ["web_creator"]),
                ("default", None),
                ("web_embedded", ["web_embedded"]),
            )

            for attempt_number, (attempt_name, player_clients) in enumerate(download_attempts, start=1):
                attempt_dir = os.path.join(temp_dir, f"attempt-{attempt_number}")
                os.makedirs(attempt_dir, exist_ok=True)
                log_event(
                    "stream_download_attempt",
                    ok=True,
                    videoId=video_id,
                    attempt=attempt_name,
                    selectedFormat=selected_format,
                )
                with YoutubeDL(
                    build_ydl_opts(
                        attempt_dir,
                        format_selector=selected_format,
                        log_stage=f"download_{attempt_name}",
                        player_clients=player_clients,
                    )
                ) as ydl:
                    try:
                        ydl.download([input_value])
                    except DownloadError as error:
                        download_error = error
                        log_failure(
                            "stream_download_attempt",
                            video_id,
                            str(error),
                            attempt=attempt_name,
                            selectedFormat=selected_format,
                        )
                        continue

                downloaded_path = find_downloaded_file(attempt_dir, video_id)
                if downloaded_path:
                    break

            if not downloaded_path:
                log_failure(
                    "stream_download",
                    video_id,
                    str(download_error or "downloaded file not found"),
                    formats=formats,
                    selectedFormat=selected_format,
                    willExtractAudio=not has_audio_only_format(formats),
                )
                return json_response(
                    503,
                    {
                        "error": "yt-dlp download failed",
                        "message": str(download_error or "Downloaded file not found"),
                        "videoId": video_id,
                        "videoUrl": video_url,
                        "formats": formats,
                        "requestId": request_id(),
                    },
                )

            if not has_audio_only_format(formats):
                try:
                    log_event(
                        "audio_extract_start",
                        ok=True,
                        videoId=video_id,
                        sourceExtension=os.path.splitext(downloaded_path)[1],
                    )
                    downloaded_path = extract_audio_from_video(downloaded_path)
                except Exception as error:
                    log_failure(
                        "audio_extract",
                        video_id,
                        str(error),
                        hasFfmpeg=get_ffmpeg_binary() is not None,
                        formats=formats,
                    )
                    return json_response(
                        503,
                        {
                            "error": "Audio extraction failed",
                            "message": str(error),
                            "videoId": video_id,
                            "videoUrl": video_url,
                            "formats": formats,
                            "requestId": request_id(),
                        },
                    )

            body_size = os.path.getsize(downloaded_path)
            if body_size > MAX_DOWNLOAD_BYTES:
                log_failure(
                    "size_check",
                    video_id,
                    "audio stream too large",
                    bodySize=body_size,
                    limit=MAX_DOWNLOAD_BYTES,
                    extension=os.path.splitext(downloaded_path)[1].lstrip(".") or "bin",
                )
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
            content_type = content_type_for_extension(extension)
            blob_path = make_blob_path(video_id, extension)

            with open(downloaded_path, "rb") as downloaded_file:
                body = downloaded_file.read()

        try:
            blob = upload_to_blob(blob_path, content_type, body)
        except Exception as error:
            log_failure("blob_upload", video_id, str(error), pathname=blob_path, contentType=content_type)
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
        log_event(
            "complete",
            ok=True,
            videoId=video_id,
            source="download",
            pathname=blob.get("pathname"),
            contentType=blob.get("contentType") or content_type,
            bodySize=len(body),
        )

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
