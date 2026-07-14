import os
import hashlib
import hmac
import time
import unittest
from unittest.mock import MagicMock, patch

from api import ytdl


class YtdlCacheTests(unittest.TestCase):
    def tearDown(self):
        ytdl.get_redis_connection.cache_clear()

    def test_upstash_native_url_is_preferred(self):
        connection = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "UPSTASH_REDIS_KV_REDIS_URL": "rediss://upstash.example",
                    "REDIS_URL": "rediss://legacy.example",
                },
                clear=False,
            ),
            patch.object(ytdl.redis, "from_url", return_value=connection) as from_url,
        ):
            self.assertIs(ytdl.get_redis_connection(), connection)

        from_url.assert_called_once_with(
            "rediss://upstash.example",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def test_redis_failures_are_treated_as_cache_misses(self):
        connection = MagicMock()
        connection.get.side_effect = TimeoutError("redis unavailable")

        with (
            ytdl.app.test_request_context("/api/ytdl"),
            patch.object(ytdl, "get_redis_connection", return_value=connection),
        ):
            self.assertIsNone(ytdl.get_cached_playback("video123"))
            self.assertTrue(ytdl.acquire_lock("video123"))
            ytdl.release_lock("video123")

    def test_cached_video_id_skips_youtube_probe(self):
        cached = {
            "video_id": "Tb0MC0jFv6M",
            "playback_url": "https://blob.example/teardrop.webm",
            "pathname": "audio/Tb0MC0jFv6M.webm",
            "mime_type": "audio/webm",
            "cached_at": "1234",
        }

        with (
            patch.dict(os.environ, {"YTDL_SECRET": "test-secret"}, clear=False),
            patch.object(ytdl, "get_cached_playback", return_value=cached),
            patch.object(ytdl, "YoutubeDL") as youtube_dl,
        ):
            response = ytdl.app.test_client().get(
                "/api/ytdl?videoId=Tb0MC0jFv6M&secret=test-secret"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "cache")
        youtube_dl.assert_not_called()

    def test_existing_blob_rebuilds_missing_redis_entry(self):
        cached = {
            "video_id": "Tb0MC0jFv6M",
            "playback_url": "https://blob.example/teardrop.webm",
            "pathname": "audio/Tb0MC0jFv6M.webm",
            "mime_type": "audio/webm",
            "cached_at": "1234",
        }

        with (
            patch.dict(os.environ, {"YTDL_SECRET": "test-secret"}, clear=False),
            patch.object(ytdl, "get_cached_playback", return_value=None),
            patch.object(ytdl, "get_blob_playback", return_value=cached),
            patch.object(ytdl, "set_cached_playback") as set_cached,
            patch.object(ytdl, "YoutubeDL") as youtube_dl,
        ):
            response = ytdl.app.test_client().get(
                "/api/ytdl?videoId=Tb0MC0jFv6M&secret=test-secret"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "cache")
        set_cached.assert_called_once_with("Tb0MC0jFv6M", cached)
        youtube_dl.assert_not_called()

    def test_player_client_override_is_scoped_to_youtube(self):
        args = ytdl.build_extractor_args(["web_creator"])

        self.assertEqual(args["youtube"]["player_client"], ["web_creator"])

    def test_starter_signature_is_short_lived_and_valid(self):
        expires = str(int(time.time()) + 60)
        signature = hmac.new(
            b"test-secret",
            f"Tb0MC0jFv6M:{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        with patch.dict(os.environ, {"YTDL_SECRET": "test-secret"}, clear=False):
            self.assertTrue(
                ytdl.verify_starter_signature("Tb0MC0jFv6M", expires, signature)
            )
            self.assertFalse(
                ytdl.verify_starter_signature("different", expires, signature)
            )

    def test_starter_format_uses_best_supported_audio(self):
        selected = ytdl.select_starter_format(
            {
                "formats": [
                    {
                        "format_id": "251",
                        "url": "https://media.example/high",
                        "protocol": "https",
                        "filesize": 5_100_000,
                        "abr": 130,
                        "acodec": "opus",
                        "vcodec": "none",
                    },
                    {
                        "format_id": "250",
                        "url": "https://media.example/starter",
                        "protocol": "https",
                        "filesize": 2_700_000,
                        "abr": 70,
                        "acodec": "opus",
                        "vcodec": "none",
                    },
                    {
                        "format_id": "249",
                        "url": "https://media.example/low",
                        "protocol": "https",
                        "filesize": 2_000_000,
                        "abr": 50,
                        "acodec": "opus",
                        "vcodec": "none",
                    },
                ]
            }
        )

        self.assertEqual(selected["format_id"], "251")

    def test_starter_route_streams_selected_audio(self):
        upstream = MagicMock()
        upstream.iter_content.return_value = [b"abc", b"def"]
        selected = {"format_id": "250", "ext": "webm"}

        with (
            patch.dict(os.environ, {"YTDL_SECRET": "test-secret"}, clear=False),
            patch.object(ytdl, "verify_starter_signature", return_value=True),
            patch.object(ytdl, "get_cached_playback", return_value=None),
            patch.object(ytdl, "get_blob_playback", return_value=None),
            patch.object(
                ytdl,
                "open_starter_upstream",
                return_value=(upstream, selected, 6),
            ),
        ):
            response = ytdl.app.test_client().get(
                "/api/ytdl?stream=1&videoId=Tb0MC0jFv6M&expires=123&signature=sig"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"abcdef")
        self.assertEqual(response.content_type, "audio/webm")
        upstream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
