"""Tests for single-owner Telegram dispatch and approval brokering."""

import threading
import time
import unittest

from telegram_bridge import (
    TelegramApprovalDecision,
    TelegramApprovalRequest,
    TelegramBridgeConfig,
    TelegramBridgeError,
    TelegramDecisionAction,
)
from telegram_dispatcher import (
    ApprovalDispatchStatus,
    TelegramApprovalBroker,
    TelegramApprovalGateway,
    TelegramUpdateDispatcher,
    StandaloneTelegramApprovalRuntime,
    parse_approval_callback,
)


def make_approval(request_id="detector-id"):
    """Return a compact deterministic approval request."""

    return TelegramApprovalRequest(
        request_id=request_id,
        task_id=7,
        task_title="Dispatcher test",
        workdir="/tmp/project",
        command="pytest -q",
        reason="test",
        context="Approve this command? [y/N]",
    )


def make_callback(request_id, action, callback_id="callback-1", chat_id=123, update_id=1):
    """Return one Telegram approval callback update."""

    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 456},
            "message": {"message_id": 99, "chat": {"id": chat_id}},
            "data": f"durex:{request_id}:{action}",
        },
    }


class FakeApprovalTransport:
    """In-memory transport used to observe dispatcher behavior."""

    def __init__(self, poll_results=None):
        self.config = TelegramBridgeConfig(
            bot_token="fake",
            allowed_chat_id=123,
            approval_timeout_seconds=1,
        )
        self.poll_results = list(poll_results or [])
        self.poll_calls = []
        self.approvals = []
        self.contexts = []
        self.callback_answers = []

    def poll_updates(self, timeout=20, allowed_updates=None):
        self.poll_calls.append((timeout, allowed_updates))
        if not self.poll_results:
            raise KeyboardInterrupt
        result = self.poll_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def send_approval_request(self, approval):
        self.approvals.append(approval)
        return len(self.approvals)

    def send_context(self, approval):
        self.contexts.append(approval)

    def answer_callback_query(self, callback_query_id, text=""):
        self.callback_answers.append((callback_query_id, text))

    def send_message(self, text, reply_markup=None):
        return 1

    def get_file(self, file_id):
        return {}

    def download_file(self, file_path, destination, max_bytes):
        return destination


class TelegramApprovalBrokerTests(unittest.TestCase):
    """Verify synchronization, idempotency, and conservative fallbacks."""

    def test_show_context_is_nonterminal_and_final_callback_completes_once(self):
        broker = TelegramApprovalBroker()
        approval = make_approval("wire-id")
        broker.register(approval)

        context = broker.resolve_callback(
            TelegramApprovalDecision(
                request_id="wire-id",
                action=TelegramDecisionAction.SHOW_CONTEXT,
                source="telegram",
            ),
            "context-callback",
        )
        final = broker.resolve_callback(
            TelegramApprovalDecision(
                request_id="wire-id",
                action=TelegramDecisionAction.APPROVE,
                source="telegram",
            ),
            "final-callback",
        )
        decision = broker.wait_for_decision("wire-id", timeout_seconds=0)
        duplicate = broker.resolve_callback(
            TelegramApprovalDecision(
                request_id="wire-id",
                action=TelegramDecisionAction.DENY,
                source="telegram",
            ),
            "replayed-callback",
        )

        self.assertEqual(context.status, ApprovalDispatchStatus.CONTEXT)
        self.assertEqual(context.approval, approval)
        self.assertEqual(final.status, ApprovalDispatchStatus.FINAL)
        self.assertEqual(decision.action, TelegramDecisionAction.APPROVE)
        self.assertEqual(duplicate.status, ApprovalDispatchStatus.DUPLICATE)

    def test_timeout_uses_configured_conservative_decision(self):
        broker = TelegramApprovalBroker()
        broker.register(make_approval("timeout-id"), TelegramDecisionAction.DENY)

        decision = broker.wait_for_decision("timeout-id", timeout_seconds=0)

        self.assertEqual(decision.action, TelegramDecisionAction.DENY)
        self.assertEqual(decision.source, "timeout")

    def test_shutdown_releases_waiter_conservatively(self):
        broker = TelegramApprovalBroker()
        broker.register(make_approval("shutdown-id"), TelegramDecisionAction.STOP)
        decisions = []
        waiter = threading.Thread(
            target=lambda: decisions.append(
                broker.wait_for_decision("shutdown-id", timeout_seconds=30)
            )
        )
        waiter.start()

        broker.shutdown()
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(decisions[0].action, TelegramDecisionAction.STOP)
        self.assertEqual(decisions[0].source, "shutdown")

    def test_shutdown_never_applies_an_approve_timeout_default(self):
        broker = TelegramApprovalBroker()
        broker.register(make_approval("shutdown-deny"), TelegramDecisionAction.APPROVE)

        broker.shutdown()
        decision = broker.wait_for_decision("shutdown-deny", timeout_seconds=0)

        self.assertEqual(decision.action, TelegramDecisionAction.DENY)
        self.assertEqual(decision.source, "shutdown")

    def test_completed_wire_token_cannot_be_registered_again(self):
        broker = TelegramApprovalBroker()
        approval = make_approval("one-use-token")
        broker.register(approval)
        broker.wait_for_decision("one-use-token", timeout_seconds=0)

        with self.assertRaisesRegex(TelegramBridgeError, "already completed"):
            broker.register(approval)


class TelegramUpdateDispatcherTests(unittest.TestCase):
    """Verify callback namespaces, authorization, and one polling loop."""

    def make_dispatcher(self, transport=None, handler=None, broker=None, **kwargs):
        transport = transport or FakeApprovalTransport()
        broker = broker or TelegramApprovalBroker()
        return TelegramUpdateDispatcher(
            transport=transport,
            approval_broker=broker,
            update_handler=handler,
            retry_base_seconds=0,
            retry_max_seconds=0,
            **kwargs,
        )

    def test_interleaved_messages_and_callback_namespaces_are_routed(self):
        transport = FakeApprovalTransport()
        broker = TelegramApprovalBroker()
        approval = make_approval("active-id")
        broker.register(approval)
        routed = []
        dispatcher = self.make_dispatcher(transport, routed.append, broker)
        message = {"update_id": 1, "message": {"chat": {"id": 123}, "text": "/status"}}
        control_callback = {
            "update_id": 2,
            "callback_query": {
                "id": "control",
                "message": {"chat": {"id": 123}},
                "data": "durextasks:refresh",
            },
        }

        dispatcher.dispatch_update(message)
        dispatcher.dispatch_update(make_callback("active-id", "show_context", "context", 123, 3))
        dispatcher.dispatch_update(control_callback)
        dispatcher.dispatch_update(make_callback("active-id", "approve", "approve", 123, 4))
        decision = broker.wait_for_decision("active-id", timeout_seconds=0)

        self.assertEqual(routed, [message, control_callback])
        self.assertEqual(transport.contexts, [approval])
        self.assertEqual(decision.action, TelegramDecisionAction.APPROVE)
        self.assertEqual(
            transport.callback_answers,
            [("context", "Context sent"), ("approve", "Durex: approve")],
        )

    def test_unauthorized_malformed_and_stale_callbacks_are_rejected(self):
        transport = FakeApprovalTransport()
        broker = TelegramApprovalBroker()
        broker.register(make_approval("active-id"))
        dispatcher = self.make_dispatcher(transport, broker=broker)

        dispatcher.dispatch_update(make_callback("active-id", "approve", "unauthorized", 999, 1))
        dispatcher.dispatch_update(make_callback("active-id", "timeout", "malformed", 123, 2))
        dispatcher.dispatch_update(make_callback("missing-id", "approve", "stale", 123, 3))

        decision = broker.wait_for_decision("active-id", timeout_seconds=0)
        self.assertEqual(decision.source, "timeout")
        self.assertEqual(
            transport.callback_answers,
            [("malformed", "Invalid approval"), ("stale", "Approval expired")],
        )

    def test_duplicate_updates_and_callbacks_cannot_change_final_decision(self):
        transport = FakeApprovalTransport()
        broker = TelegramApprovalBroker()
        broker.register(make_approval("active-id"))
        dispatcher = self.make_dispatcher(transport, broker=broker)
        approve = make_callback("active-id", "approve", "same-callback", 123, 10)

        dispatcher.dispatch_update(approve)
        dispatcher.dispatch_update(approve)
        dispatcher.dispatch_update(make_callback("active-id", "deny", "late-callback", 123, 11))
        decision = broker.wait_for_decision("active-id", timeout_seconds=0)

        self.assertEqual(decision.action, TelegramDecisionAction.APPROVE)
        self.assertEqual(
            transport.callback_answers,
            [("same-callback", "Durex: approve"), ("late-callback", "Approval already handled")],
        )

    def test_polling_retries_without_starting_an_approval_poller(self):
        callback = make_callback("active-id", "deny")
        transport = FakeApprovalTransport(
            [TelegramBridgeError("temporary failure"), [callback]]
        )
        broker = TelegramApprovalBroker()
        broker.register(make_approval("active-id"))
        errors = []
        dispatcher = self.make_dispatcher(
            transport,
            broker=broker,
            on_poll_error=errors.append,
        )

        with self.assertRaises(KeyboardInterrupt):
            dispatcher.run_forever()
        decision = broker.wait_for_decision("active-id", timeout_seconds=0)

        self.assertEqual(len(transport.poll_calls), 3)
        self.assertEqual([str(error) for error in errors], ["temporary failure"])
        self.assertEqual(decision.action, TelegramDecisionAction.DENY)

    def test_callback_parser_rejects_invalid_chat_shape(self):
        callback = make_callback("active-id", "approve")["callback_query"]
        callback["message"]["chat"]["id"] = None

        self.assertIsNone(parse_approval_callback(callback, allowed_chat_id=123))


class TelegramApprovalGatewayTests(unittest.TestCase):
    """Verify publish-before-wait ordering and callback correlation tokens."""

    def test_gateway_uses_unique_wire_token_and_restores_detector_id(self):
        transport = FakeApprovalTransport()
        broker = TelegramApprovalBroker()
        gateway = TelegramApprovalGateway(
            transport=transport,
            broker=broker,
            timeout_seconds=1,
            token_factory=lambda: "unique-wire-token",
        )
        decisions = []
        waiter = threading.Thread(
            target=lambda: decisions.append(gateway.request_decision(make_approval()))
        )
        waiter.start()
        deadline = time.time() + 1
        while not transport.approvals and time.time() < deadline:
            time.sleep(0.001)

        self.assertEqual(transport.approvals[0].request_id, "unique-wire-token")
        broker.resolve_callback(
            TelegramApprovalDecision(
                request_id="unique-wire-token",
                action=TelegramDecisionAction.APPROVE,
                source="telegram",
            ),
            "callback",
        )
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(decisions[0].request_id, "detector-id")
        self.assertEqual(decisions[0].action, TelegramDecisionAction.APPROVE)

    def test_send_failure_cancels_pending_request(self):
        class FailingTransport(FakeApprovalTransport):
            def send_approval_request(self, approval):
                raise TelegramBridgeError("send failed")

        broker = TelegramApprovalBroker()
        gateway = TelegramApprovalGateway(
            transport=FailingTransport(),
            broker=broker,
            token_factory=lambda: "failed-wire-token",
        )

        with self.assertRaisesRegex(TelegramBridgeError, "send failed"):
            gateway.request_decision(make_approval())
        with self.assertRaisesRegex(TelegramBridgeError, "not pending"):
            broker.wait_for_decision("failed-wire-token", timeout_seconds=0)


class StandaloneTelegramApprovalRuntimeTests(unittest.TestCase):
    """Verify process-level standalone polling ownership."""

    def test_second_runtime_cannot_start_while_first_dispatcher_is_alive(self):
        first = StandaloneTelegramApprovalRuntime(FakeApprovalTransport())
        second = StandaloneTelegramApprovalRuntime(FakeApprovalTransport())
        release = threading.Event()

        def hold_dispatcher():
            release.wait(timeout=2)

        first.dispatcher.run_forever = hold_dispatcher
        first.start()
        try:
            with self.assertRaisesRegex(TelegramBridgeError, "still active"):
                second.start()
        finally:
            release.set()
            first.close()


if __name__ == "__main__":
    unittest.main()
