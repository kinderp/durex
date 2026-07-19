"""Tests for transport-neutral runtime protocol compatibility."""

import inspect
from typing import get_type_hints
import unittest

from runtime_contracts import TelegramTransport
from telegram_bridge import TelegramApprovalBridge


class RuntimeContractTests(unittest.TestCase):
    """Keep structural contracts substitutable by runtime implementations."""

    def test_telegram_download_contract_matches_bridge_types(self):
        """Protocol-valid download values must be accepted by the bridge."""

        protocol_hints = get_type_hints(TelegramTransport.download_file)
        bridge_hints = get_type_hints(TelegramApprovalBridge.download_file)

        self.assertIs(protocol_hints["file_path"], bridge_hints["file_path"])
        self.assertIs(protocol_hints["destination"], bridge_hints["destination"])
        self.assertIs(protocol_hints["max_bytes"], bridge_hints["max_bytes"])
        self.assertIs(protocol_hints["return"], bridge_hints["return"])

        protocol_max_bytes = inspect.signature(
            TelegramTransport.download_file
        ).parameters["max_bytes"]
        bridge_max_bytes = inspect.signature(
            TelegramApprovalBridge.download_file
        ).parameters["max_bytes"]
        self.assertIs(protocol_max_bytes.default, inspect.Parameter.empty)
        self.assertIsInstance(bridge_max_bytes.default, int)
        self.assertGreater(bridge_max_bytes.default, 0)

    def test_callback_acknowledgement_text_matches_bridge_type(self):
        """Callback text should not permit values rejected by the bridge API."""

        protocol_hints = get_type_hints(TelegramTransport.answer_callback_query)
        bridge_hints = get_type_hints(TelegramApprovalBridge.answer_callback_query)

        self.assertIs(protocol_hints["text"], bridge_hints["text"])
        self.assertEqual(
            inspect.signature(TelegramTransport.answer_callback_query).parameters[
                "text"
            ].default,
            inspect.signature(TelegramApprovalBridge.answer_callback_query).parameters[
                "text"
            ].default,
        )


if __name__ == "__main__":
    unittest.main()
