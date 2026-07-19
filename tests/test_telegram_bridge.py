"""Tests for Telegram transport file handling."""

from http.client import IncompleteRead
from io import BytesIO
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from telegram_bridge import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    TELEGRAM_TRUNCATION_MARKER,
    TelegramApprovalBridge,
    TelegramApprovalRequest,
    TelegramBridgeConfig,
    TelegramBridgeError,
)


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

    def test_api_call_validates_json_and_top_level_envelope(self):
        """Malformed bodies and non-boolean success flags must be bridge failures."""

        for payload in (
            b"not-json",
            b"[]",
            b'{"ok": "false", "result": []}',
            b'{"ok": 1, "result": []}',
        ):
            with self.subTest(payload=payload), mock.patch(
                "telegram_bridge.request.urlopen",
                return_value=StubResponse(payload),
            ):
                with self.assertRaises(TelegramBridgeError):
                    self.bridge.api_call("getUpdates", {})

    def test_api_call_normalizes_truncated_http_read(self):
        """Incomplete HTTP bodies must remain retryable bridge failures."""

        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = IncompleteRead(
            b'{"ok": true',
            4,
        )
        with mock.patch(
            "telegram_bridge.request.urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(TelegramBridgeError, "request failed"):
                self.bridge.api_call("getUpdates", {})

    def test_send_message_validates_result_and_message_id(self):
        """Malformed send results must remain dispatcher-safe bridge errors."""

        malformed_responses = (
            ({"ok": True}, "invalid result"),
            ({"ok": True, "result": []}, "invalid result"),
            ({"ok": True, "result": {}}, "invalid message id"),
            ({"ok": True, "result": {"message_id": True}}, "invalid message id"),
            ({"ok": True, "result": {"message_id": "7"}}, "invalid message id"),
        )
        for response, error_text in malformed_responses:
            with self.subTest(response=response), mock.patch.object(
                self.bridge,
                "api_call",
                return_value=response,
            ):
                with self.assertRaisesRegex(TelegramBridgeError, error_text):
                    self.bridge.send_message("test")

        with mock.patch.object(
            self.bridge,
            "api_call",
            return_value={"ok": True, "result": {"message_id": 7}},
        ):
            self.assertEqual(self.bridge.send_message("test"), 7)

    def test_outbound_messages_are_bounded_and_context_keeps_tail(self):
        """Telegram text limits must not hide the newest approval context."""

        oversized = "x" * (TELEGRAM_MESSAGE_MAX_CHARS + 100)
        approval = TelegramApprovalRequest(
            request_id="wire-id",
            task_id=7,
            task_title="Bounded context",
            workdir="/tmp/project",
            command="pytest -q",
            reason="test",
            context=f"old-{oversized}-newest-context",
        )
        responses = []

        def capture(_method, payload, request_timeout=30):
            responses.append(payload)
            return {"ok": True, "result": {"message_id": len(responses)}}

        with mock.patch.object(self.bridge, "api_call", side_effect=capture):
            self.bridge.send_message(oversized)
            self.bridge.send_context(approval)

        normal_text = responses[0]["text"]
        context_text = responses[1]["text"]
        self.assertEqual(len(normal_text), TELEGRAM_MESSAGE_MAX_CHARS)
        self.assertTrue(normal_text.endswith(TELEGRAM_TRUNCATION_MARKER))
        self.assertLessEqual(len(context_text), TELEGRAM_MESSAGE_MAX_CHARS)
        self.assertIn("Context for request wire-id", context_text)
        self.assertIn(TELEGRAM_TRUNCATION_MARKER, context_text)
        self.assertTrue(context_text.endswith("newest-context"))

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
