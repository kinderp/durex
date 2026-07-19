"""Tests for Telegram transport file handling."""

from io import BytesIO
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from telegram_bridge import TelegramApprovalBridge, TelegramBridgeConfig, TelegramBridgeError


class StubResponse(BytesIO):
    """Context-managed byte response used by urlopen tests."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class TelegramBridgeDownloadTests(unittest.TestCase):
    """Verify bounded and recoverable Telegram file downloads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = TelegramApprovalBridge(
            TelegramBridgeConfig(bot_token="fake", allowed_chat_id=123)
        )

    def destination(self) -> Path:
        return Path(self.tmp.name) / "voice.ogg"

    def test_download_file_streams_within_limit(self):
        """A valid response should be streamed to the requested destination."""

        payload = b"voice-data" * 10000
        with mock.patch(
            "telegram_bridge.request.urlopen",
            return_value=StubResponse(payload),
        ):
            result = self.bridge.download_file(
                "voice/file.ogg",
                str(self.destination()),
                max_bytes=len(payload),
            )

        self.assertEqual(result, str(self.destination()))
        self.assertEqual(self.destination().read_bytes(), payload)

    def test_download_file_rejects_over_limit_response(self):
        """Streaming should stop and remove output as soon as the limit is exceeded."""

        with mock.patch(
            "telegram_bridge.request.urlopen",
            return_value=StubResponse(b"123456"),
        ):
            with self.assertRaisesRegex(TelegramBridgeError, "5-byte limit"):
                self.bridge.download_file(
                    "voice/file.ogg",
                    str(self.destination()),
                    max_bytes=5,
                )

        self.assertFalse(self.destination().exists())

    def test_download_file_normalizes_local_write_failure(self):
        """Filesystem failures should be recoverable bridge errors without partial data."""

        with mock.patch(
            "telegram_bridge.request.urlopen",
            return_value=StubResponse(b"voice-data"),
        ), mock.patch("telegram_bridge.Path.open", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TelegramBridgeError, "disk full"):
                self.bridge.download_file(
                    "voice/file.ogg",
                    str(self.destination()),
                )

        self.assertFalse(self.destination().exists())

    def test_poll_updates_bounds_http_timeout_from_long_poll_timeout(self):
        """Dispatcher shutdown should not inherit the generic 30-second timeout."""

        with mock.patch.object(
            self.bridge,
            "api_call",
            return_value={"result": []},
        ) as api_call:
            updates = self.bridge.poll_updates(
                timeout=3,
                allowed_updates=["message", "callback_query"],
            )

        self.assertEqual(updates, [])
        api_call.assert_called_once_with(
            "getUpdates",
            {
                "timeout": 3,
                "allowed_updates": ["message", "callback_query"],
            },
            request_timeout=8,
        )

    def test_api_call_normalizes_malformed_json_and_top_level_shape(self):
        """Malformed HTTP bodies must remain retryable bridge failures."""

        for payload in (b"not-json", b"[]"):
            with self.subTest(payload=payload), mock.patch(
                "telegram_bridge.request.urlopen",
                return_value=StubResponse(payload),
            ):
                with self.assertRaises(TelegramBridgeError):
                    self.bridge.api_call("getUpdates", {})

    def test_poll_updates_rejects_malformed_batch_before_advancing_offset(self):
        """One malformed update must not partially acknowledge its batch."""

        malformed_batches = (
            ([{"update_id": 8}, "invalid"], "invalid update list"),
            ([{}], "invalid update ids"),
            ([{"update_id": True}], "invalid update ids"),
            ([{"update_id": 8}, {"update_id": 8}], "invalid update ids"),
            ([{"update_id": 9}, {"update_id": 8}], "invalid update ids"),
            ([{"update_id": 7}], "invalid update ids"),
        )

        for batch, error_text in malformed_batches:
            with self.subTest(batch=batch):
                self.bridge._last_update_id = 7
                with mock.patch.object(
                    self.bridge,
                    "api_call",
                    return_value={"ok": True, "result": batch},
                ):
                    with self.assertRaisesRegex(TelegramBridgeError, error_text):
                        self.bridge.poll_updates(timeout=0)

                self.assertEqual(self.bridge._last_update_id, 7)


if __name__ == "__main__":
    unittest.main()
