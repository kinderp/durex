"""Tests for transport-neutral runtime protocol compatibility."""

import inspect
from typing import get_type_hints
import unittest

from runtime_contracts import (
    RunnerInteractionEvent,
    RunnerInteractionState,
    RunnerLifecycle,
    RunnerLifecycleEvent,
    RunnerOutputEvent,
    TelegramTransport,
    TelegramTransportConfig,
)
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

    def test_transport_configuration_is_read_only(self):
        """The protocol should not permit replacing bridge-owned config."""

        config_property = TelegramTransport.__dict__["config"]
        chat_id_property = TelegramTransportConfig.__dict__["allowed_chat_id"]

        self.assertIsInstance(config_property, property)
        self.assertIsNone(config_property.fset)
        self.assertIs(
            get_type_hints(config_property.fget)["return"],
            TelegramTransportConfig,
        )
        self.assertIsInstance(chat_id_property, property)
        self.assertIsNone(chat_id_property.fset)
        self.assertIs(get_type_hints(chat_id_property.fget)["return"], int)

    def test_runner_events_share_run_identity_and_monotonic_sequence(self):
        """Every event kind carries the ordering identity used by consumers."""

        events = [
            RunnerLifecycleEvent(1, RunnerLifecycle.STARTED, run_id="run-1", sequence=1),
            RunnerOutputEvent(1, 2, "chunk", run_id="run-1"),
            RunnerInteractionEvent(
                1,
                "approval-1",
                "approval",
                "Proceed?",
                run_id="run-1",
                sequence=3,
                state=RunnerInteractionState.REQUESTED,
            ),
        ]

        self.assertEqual([event.run_id for event in events], ["run-1"] * 3)
        self.assertEqual([event.sequence for event in events], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
