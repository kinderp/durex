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


if __name__ == "__main__":
    unittest.main()
