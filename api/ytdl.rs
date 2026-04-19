use redis::{AsyncCommands, Client as RedisClient};
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue};
use rusty_ytdl::{choose_format, Video, VideoOptions};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use url::form_urlencoded;
use vercel_runtime::{run, service_fn, Error, Request, Response};

const LOCK_TTL_SECONDS: u64 = 5 * 60;
const CACHE_TTL_SECONDS: u64 = 30 * 24 * 60 * 60;
const MAX_DOWNLOAD_BYTES: usize = 30 * 1024 * 1024;

#[derive(Deserialize, Serialize, Clone)]
struct BlobPutResponse {
    url: String,
    #[serde(rename = "downloadUrl")]
    download_url: String,
    pathname: String,
    #[serde(rename = "contentType")]
    content_type: Option<String>,
}

#[derive(Deserialize, Serialize, Clone)]
struct CachedPlayback {
    video_id: String,
    playback_url: String,
    pathname: Option<String>,
    mime_type: String,
    cached_at: String,
}

fn json_response(status: u16, payload: Value) -> Result<Response<Value>, Error> {
    Ok(Response::builder()
        .status(status)
        .header("Content-Type", "application/json")
        .header("Cache-Control", "no-store")
        .body(payload)?)
}

fn query_value(req: &Request, key: &str) -> Option<String> {
    req.uri().query().and_then(|query| {
        form_urlencoded::parse(query.as_bytes()).find_map(|(candidate_key, candidate_value)| {
            if candidate_key == key {
                Some(candidate_value.into_owned())
            } else {
                None
            }
        })
    })
}

fn extract_secret(req: &Request) -> String {
    let query_secret = query_value(req, "secret")
        .map(|value| value.trim().to_string())
        .unwrap_or_default();

    if !query_secret.is_empty() {
        return query_secret;
    }

    req.headers()
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .map(|value| value.trim())
        .and_then(|value| {
            value
                .strip_prefix("Bearer ")
                .or_else(|| value.strip_prefix("bearer "))
        })
        .map(|value| value.trim().to_string())
        .unwrap_or_default()
}

fn extract_input(req: &Request) -> String {
    for key in ["videoId", "url", "arg", "q", "query"] {
        if let Some(value) = query_value(req, key) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                if key == "videoId" {
                    return format!("https://www.youtube.com/watch?v={trimmed}");
                }
                return trimmed.to_string();
            }
        }
    }

    String::new()
}

fn make_blob_path(video_id: &str, extension: &str) -> String {
    format!("audio/{video_id}.{extension}")
}

fn cache_key(video_id: &str) -> String {
    format!("music:ytdl:video:{video_id}")
}

fn lock_key(video_id: &str) -> String {
    format!("music:ytdl:video:lock:{video_id}")
}

async fn get_redis_connection() -> Result<Option<redis::aio::MultiplexedConnection>, Error> {
    let redis_url = match std::env::var("REDIS_URL") {
        Ok(value) if !value.trim().is_empty() => value,
        _ => return Ok(None),
    };

    let client = RedisClient::open(redis_url)?;
    Ok(Some(client.get_multiplexed_tokio_connection().await?))
}

async fn get_cached_playback(video_id: &str) -> Result<Option<CachedPlayback>, Error> {
    let Some(mut connection) = get_redis_connection().await? else {
        return Ok(None);
    };

    let cached: Option<String> = connection.get(cache_key(video_id)).await?;
    match cached {
        Some(value) => Ok(Some(serde_json::from_str::<CachedPlayback>(&value)?)),
        None => Ok(None),
    }
}

async fn set_cached_playback(video_id: &str, value: &CachedPlayback) -> Result<(), Error> {
    let Some(mut connection) = get_redis_connection().await? else {
        return Ok(());
    };

    let encoded = serde_json::to_string(value)?;
    let _: () = connection
        .set_ex(cache_key(video_id), encoded, CACHE_TTL_SECONDS)
        .await?;
    Ok(())
}

async fn acquire_lock(video_id: &str) -> Result<bool, Error> {
    let Some(mut connection) = get_redis_connection().await? else {
        return Ok(true);
    };

    let lock_key = lock_key(video_id);
    let acquired: bool = connection.set_nx(&lock_key, "1").await?;
    if acquired {
        let _: bool = connection.expire(lock_key, LOCK_TTL_SECONDS as i64).await?;
    }
    Ok(acquired)
}

async fn release_lock(video_id: &str) -> Result<(), Error> {
    let Some(mut connection) = get_redis_connection().await? else {
        return Ok(());
    };

    let _: usize = connection.del(lock_key(video_id)).await?;
    Ok(())
}

async fn collect_stream_bytes(video: &Video) -> Result<Vec<u8>, Error> {
    let stream = video.stream().await?;
    let mut body = Vec::with_capacity(stream.content_length().min(MAX_DOWNLOAD_BYTES));

    while let Some(chunk) = stream.chunk().await? {
        if body.len() + chunk.len() > MAX_DOWNLOAD_BYTES {
            return Err(format!(
                "Audio stream exceeds {} MB function limit",
                MAX_DOWNLOAD_BYTES / 1024 / 1024
            )
            .into());
        }
        body.extend_from_slice(&chunk);
    }

    Ok(body)
}

async fn upload_to_blob(
    pathname: &str,
    content_type: &str,
    body: Vec<u8>,
) -> Result<BlobPutResponse, Error> {
    let token = std::env::var("BLOB_READ_WRITE_TOKEN").map_err(|_| "BLOB_READ_WRITE_TOKEN missing")?;

    let url = format!(
        "https://vercel.com/api/blob/?pathname={}",
        url::form_urlencoded::byte_serialize(pathname.as_bytes()).collect::<String>()
    );

    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {token}"))?,
    );
    headers.insert("x-vercel-blob-access", HeaderValue::from_static("public"));
    headers.insert("x-add-random-suffix", HeaderValue::from_static("0"));
    headers.insert("x-allow-overwrite", HeaderValue::from_static("1"));
    headers.insert("x-content-type", HeaderValue::from_str(content_type)?);

    let client = reqwest::Client::new();
    let response = client
        .put(url)
        .headers(headers)
        .body(body)
        .send()
        .await?;

    if !response.status().is_success() {
        let status = response.status();
        let details = response
            .text()
            .await
            .unwrap_or_else(|_| "Unable to read blob API response body".to_string());
        return Err(format!("Blob API request failed ({status}): {details}").into());
    }

    Ok(response.json::<BlobPutResponse>().await?)
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    let service = service_fn(handler);
    run(service).await
}

async fn handler(req: Request) -> Result<Response<Value>, Error> {
    if req.method().as_str() != "GET" {
        return json_response(405, json!({ "error": "Method not allowed" }));
    }

    let configured_secret = match std::env::var("YTDL_SECRET") {
        Ok(value) if !value.trim().is_empty() => value,
        _ => return json_response(500, json!({ "error": "YTDL_SECRET missing" })),
    };

    if extract_secret(&req) != configured_secret.trim() {
        return json_response(401, json!({ "error": "Unauthorised" }));
    }

    let input = extract_input(&req);
    if input.is_empty() {
        return json_response(
            400,
            json!({
                "error": "One input argument is required via videoId, url, arg, q, or query"
            }),
        );
    }

    let video_options = VideoOptions::default();

    let video = match Video::new_with_options(input.clone(), video_options.clone()) {
        Ok(video) => video,
        Err(error) => {
            return json_response(
                400,
                json!({
                    "error": "Invalid YouTube input",
                    "message": error.to_string(),
                }),
            )
        }
    };

    let video_id = video.get_video_id();
    let video_url = video.get_video_url();

    if let Some(cached) = get_cached_playback(&video_id).await? {
        return json_response(
            200,
            json!({
                "ok": true,
                "runtime": "rust",
                "source": "cache",
                "input": input,
                "videoId": cached.video_id,
                "videoUrl": video_url,
                "blob": {
                    "url": cached.playback_url,
                    "downloadUrl": cached.playback_url,
                    "pathname": cached.pathname,
                    "contentType": cached.mime_type,
                },
                "cachedAt": cached.cached_at,
            }),
        );
    }

    if !acquire_lock(&video_id).await? {
        return json_response(
            202,
            json!({
                "ok": false,
                "status": "preparing",
                "videoId": video_id,
                "videoUrl": video_url,
            }),
        );
    }

    let response = async {
        if let Some(cached) = get_cached_playback(&video_id).await? {
            return json_response(
                200,
                json!({
                    "ok": true,
                    "runtime": "rust",
                    "source": "cache",
                    "input": input,
                    "videoId": cached.video_id,
                    "videoUrl": video_url,
                    "blob": {
                        "url": cached.playback_url,
                        "downloadUrl": cached.playback_url,
                        "pathname": cached.pathname,
                        "contentType": cached.mime_type,
                    },
                    "cachedAt": cached.cached_at,
                }),
            );
        }

        let info = match video.get_info().await {
            Ok(info) => info,
            Err(error) => {
                return json_response(
                    502,
                    json!({
                        "error": "rusty_ytdl lookup failed",
                        "message": error.to_string(),
                        "videoId": video_id,
                        "videoUrl": video_url,
                    }),
                )
            }
        };

        let selected_format = match choose_format(&info.formats, &video_options) {
            Ok(format) => format,
            Err(error) => {
                return json_response(
                    502,
                    json!({
                        "error": "No downloadable format found",
                        "message": error.to_string(),
                        "videoId": video_id,
                        "videoUrl": video_url,
                    }),
                )
            }
        };

        if let Some(content_length) = selected_format.content_length.as_deref() {
            if let Ok(content_length) = content_length.parse::<u64>() {
                if content_length > MAX_DOWNLOAD_BYTES as u64 {
                    return json_response(
                        413,
                        json!({
                            "error": "Audio stream too large for Vercel function",
                            "message": format!(
                                "Selected stream is {} MB; function limit is {} MB",
                                content_length / 1024 / 1024,
                                MAX_DOWNLOAD_BYTES / 1024 / 1024,
                            ),
                            "videoId": video_id,
                            "videoUrl": video_url,
                        }),
                    );
                }
            }
        }

        let content_type = format!(
            "{}/{}",
            selected_format.mime_type.mime.type_(),
            selected_format.mime_type.mime.subtype()
        );
        let extension = selected_format.mime_type.container.clone();
        let blob_path = make_blob_path(&video_id, &extension);

        let body = match collect_stream_bytes(&video).await {
            Ok(body) => body,
            Err(error) => {
                return json_response(
                    502,
                    json!({
                        "error": "rusty_ytdl download failed",
                        "message": error.to_string(),
                        "videoId": video_id,
                        "videoUrl": video_url,
                    }),
                )
            }
        };

        let blob = match upload_to_blob(&blob_path, &content_type, body).await {
            Ok(blob) => blob,
            Err(error) => {
                return json_response(
                    502,
                    json!({
                        "error": "Blob upload failed",
                        "message": error.to_string(),
                        "videoId": video_id,
                        "videoUrl": video_url,
                        "pathname": blob_path,
                    }),
                )
            }
        };

        let cached = CachedPlayback {
            video_id: video_id.clone(),
            playback_url: blob.url.clone(),
            pathname: Some(blob.pathname.clone()),
            mime_type: blob.content_type.clone().unwrap_or_else(|| content_type.clone()),
            cached_at: chrono_like_timestamp(),
        };

        set_cached_playback(&video_id, &cached).await?;

        json_response(
            200,
            json!({
                "ok": true,
                "runtime": "rust",
                "source": "download",
                "input": input,
                "videoId": video_id,
                "videoUrl": video_url,
                "blob": {
                    "url": blob.url,
                    "downloadUrl": blob.download_url,
                    "pathname": blob.pathname,
                    "contentType": blob.content_type.unwrap_or(content_type),
                },
                "cachedAt": cached.cached_at,
            }),
        )
    }
    .await;

    release_lock(&video_id).await?;
    response
}

fn chrono_like_timestamp() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{now}")
}
