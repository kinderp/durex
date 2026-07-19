#!/usr/bin/env python3
"""Single-owner Telegram update dispatch and approval coordination."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum
import secrets
import threading
from typing import Callable, Optional, Protocol

from runtime_contracts import TelegramTransport, TelegramTransportConfig
from telegram_bridge import (
    DEFAULT_TELEGRAM_API_TIMEOUT_SECONDS,
    TelegramApprovalDecision,
    TelegramApprovalRequest,
    TelegramBridgeError,
    TelegramDecisionAction,
)


APPROVAL_CALLBACK_NAMESPACE = "durex"
APPROVAL_CALLBACK_ACTIONS = frozenset(
    {
        TelegramDecisionAction.APPROVE,
        TelegramDecisionAction.DENY,
        TelegramDecisionAction.SHOW_CONTEXT,
        TelegramDecisionAction.STOP,
    }
)
DEFAULT_DEDUPLICATION_ENTRIES = 1024
MAX_APPROVAL_CALLBACK_API_CALLS = 2
_standalone_dispatcher_lock = threading.Lock()


class ApprovalDecisionProvider(Protocol):
    """Decision boundary consumed by the PTY runner."""

    def request_decision(self, approval: TelegramApprovalRequest) -> TelegramApprovalDecision:
        """Publish one request and wait for a final human decision."""


class TelegramApprovalTransportConfig(TelegramTransportConfig, Protocol):
    """Approval timing and fallback settings exposed by the transport."""

    @property
    def approval_timeout_seconds(self) -> int:
        """Return the maximum wait for one human decision."""

    @property
    def timeout_default_decision(self) -> TelegramDecisionAction:
        """Return the decision used when an approval times out."""


class TelegramApprovalTransport(TelegramTransport, Protocol):
    """Outbound Telegram operations needed by approval dispatch."""

    @property
    def config(self) -> TelegramApprovalTransportConfig:
        """Return authorization and approval timing settings."""

    def send_approval_request(self, approval: TelegramApprovalRequest) -> int:
        """Send one approval prompt and return its Telegram message id."""

    def send_context(self, approval: TelegramApprovalRequest) -> None:
        """Send expanded context for one active approval."""


class ApprovalDispatchStatus(str, Enum):
    """Outcome of routing one authorized approval callback."""

    FINAL = "final"
    CONTEXT = "context"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True)
class ApprovalDispatchResult:
    """Broker result returned to the Telegram dispatcher."""

    status: ApprovalDispatchStatus
    approval: Optional[TelegramApprovalRequest] = None


@dataclass
class _PendingApproval:
    """Mutable synchronization state for one active approval request."""

    approval: TelegramApprovalRequest
    timeout_default: TelegramDecisionAction
    event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[TelegramApprovalDecision] = None


class TelegramApprovalBroker:
    """Thread-safe registry joining dispatcher callbacks to PTY waits."""

    def __init__(self, max_deduplication_entries: int = DEFAULT_DEDUPLICATION_ENTRIES) -> None:
        if max_deduplication_entries < 1:
            raise ValueError("max_deduplication_entries must be positive")
        self._max_deduplication_entries = max_deduplication_entries
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingApproval] = {}
        self._seen_callback_ids: OrderedDict[str, None] = OrderedDict()
        self._completed_request_ids: OrderedDict[str, None] = OrderedDict()
        self._closed = False

    def register(
        self,
        approval: TelegramApprovalRequest,
        timeout_default: TelegramDecisionAction = TelegramDecisionAction.DENY,
    ) -> None:
        """Register a request before its Telegram message becomes visible."""

        if timeout_default not in {
            TelegramDecisionAction.APPROVE,
            TelegramDecisionAction.DENY,
            TelegramDecisionAction.STOP,
        }:
            raise ValueError("timeout_default must be a final decision")

        with self._lock:
            if self._closed:
                raise TelegramBridgeError("Telegram approval broker is shut down.")
            if approval.request_id in self._pending:
                raise TelegramBridgeError(
                    f"Telegram approval request is already pending: {approval.request_id}"
                )
            if approval.request_id in self._completed_request_ids:
                raise TelegramBridgeError(
                    f"Telegram approval request token was already completed: {approval.request_id}"
                )
            self._pending[approval.request_id] = _PendingApproval(
                approval=approval,
                timeout_default=timeout_default,
            )

    def cancel(self, request_id: str) -> None:
        """Remove a request whose outbound Telegram send failed."""

        with self._lock:
            self._pending.pop(request_id, None)

    def resolve_callback(
        self,
        decision: TelegramApprovalDecision,
        callback_query_id: str,
    ) -> ApprovalDispatchResult:
        """Apply one callback at most once without completing context requests."""

        with self._lock:
            if callback_query_id in self._seen_callback_ids:
                return ApprovalDispatchResult(ApprovalDispatchStatus.DUPLICATE)
            self._remember(self._seen_callback_ids, callback_query_id)

            pending = self._pending.get(decision.request_id)
            if pending is None:
                status = (
                    ApprovalDispatchStatus.DUPLICATE
                    if decision.request_id in self._completed_request_ids
                    else ApprovalDispatchStatus.STALE
                )
                return ApprovalDispatchResult(status)

            if pending.decision is not None:
                return ApprovalDispatchResult(ApprovalDispatchStatus.DUPLICATE)

            if decision.action == TelegramDecisionAction.SHOW_CONTEXT:
                return ApprovalDispatchResult(
                    ApprovalDispatchStatus.CONTEXT,
                    approval=pending.approval,
                )

            pending.decision = decision
            pending.event.set()
            return ApprovalDispatchResult(
                ApprovalDispatchStatus.FINAL,
                approval=pending.approval,
            )

    def wait_for_decision(self, request_id: str, timeout_seconds: float) -> TelegramApprovalDecision:
        """Wait for a final callback or apply the request's conservative timeout."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise TelegramBridgeError(f"Telegram approval request is not pending: {request_id}")

        pending.event.wait(max(0.0, timeout_seconds))

        with self._lock:
            current = self._pending.get(request_id)
            if current is not pending:
                raise TelegramBridgeError(f"Telegram approval request was cancelled: {request_id}")
            if pending.decision is None:
                pending.decision = TelegramApprovalDecision(
                    request_id=request_id,
                    action=pending.timeout_default,
                    source="timeout",
                )
            self._pending.pop(request_id, None)
            self._remember(self._completed_request_ids, request_id)
            return pending.decision

    def shutdown(self) -> None:
        """Release every waiter with its configured conservative decision."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            for pending in self._pending.values():
                if pending.decision is None:
                    shutdown_action = (
                        TelegramDecisionAction.STOP
                        if pending.timeout_default == TelegramDecisionAction.STOP
                        else TelegramDecisionAction.DENY
                    )
                    pending.decision = TelegramApprovalDecision(
                        request_id=pending.approval.request_id,
                        action=shutdown_action,
                        source="shutdown",
                    )
                    pending.event.set()

    def _remember(self, values: OrderedDict[str, None], key: str) -> None:
        values[key] = None
        values.move_to_end(key)
        while len(values) > self._max_deduplication_entries:
            values.popitem(last=False)


class TelegramApprovalGateway:
    """Publish approvals through Telegram and wait only on the broker."""

    def __init__(
        self,
        transport: TelegramApprovalTransport,
        broker: TelegramApprovalBroker,
        timeout_seconds: float = 900,
        timeout_default: TelegramDecisionAction = TelegramDecisionAction.DENY,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(12),
    ) -> None:
        self.transport = transport
        self.broker = broker
        self.timeout_seconds = timeout_seconds
        self.timeout_default = timeout_default
        self.token_factory = token_factory

    def request_decision(self, approval: TelegramApprovalRequest) -> TelegramApprovalDecision:
        """Register, publish, and await one uniquely correlated approval."""

        callback_token = self.token_factory()
        if not callback_token or ":" in callback_token:
            raise TelegramBridgeError("Telegram approval callback token is invalid.")
        wire_approval = replace(approval, request_id=callback_token)
        self.broker.register(wire_approval, timeout_default=self.timeout_default)
        try:
            self.transport.send_approval_request(wire_approval)
        except BaseException:
            self.broker.cancel(callback_token)
            raise

        decision = self.broker.wait_for_decision(callback_token, self.timeout_seconds)
        return replace(decision, request_id=approval.request_id)


def parse_approval_callback(
    callback: dict,
    allowed_chat_id: int,
) -> Optional[TelegramApprovalDecision]:
    """Validate and normalize one callback in the approval namespace."""

    chat_id = _callback_chat_id(callback)
    if chat_id != allowed_chat_id:
        return None

    message = callback.get("message")
    if not isinstance(message, dict):
        return None

    data = callback.get("data")
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != APPROVAL_CALLBACK_NAMESPACE or not parts[1]:
        return None
    try:
        action = TelegramDecisionAction(parts[2])
    except ValueError:
        return None
    if action not in APPROVAL_CALLBACK_ACTIONS:
        return None

    user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
    return TelegramApprovalDecision(
        request_id=parts[1],
        action=action,
        source="telegram",
        telegram_user_id=user.get("id"),
        telegram_message_id=message.get("message_id"),
    )


def _callback_chat_id(callback: dict) -> Optional[int]:
    """Return a callback chat id without trusting Telegram payload types."""

    message = callback.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    try:
        return int(chat.get("id"))
    except (TypeError, ValueError):
        return None


class TelegramUpdateDispatcher:
    """Sole process-level owner of Telegram ``getUpdates`` polling."""

    def __init__(
        self,
        transport: TelegramApprovalTransport,
        approval_broker: TelegramApprovalBroker,
        update_handler: Optional[Callable[[dict], object]] = None,
        poll_timeout_seconds: int = 20,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        on_poll_error: Optional[Callable[[TelegramBridgeError], None]] = None,
        max_deduplication_entries: int = DEFAULT_DEDUPLICATION_ENTRIES,
        allowed_updates: Optional[list[str]] = None,
    ) -> None:
        if max_deduplication_entries < 1:
            raise ValueError("max_deduplication_entries must be positive")
        self.transport = transport
        self.approval_broker = approval_broker
        self.update_handler = update_handler
        self.poll_timeout_seconds = poll_timeout_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.on_poll_error = on_poll_error
        self.allowed_updates = tuple(
            allowed_updates
            if allowed_updates is not None
            else ["message", "callback_query"]
        )
        self._stop_event = threading.Event()
        self._seen_update_ids: OrderedDict[int, None] = OrderedDict()
        self._max_deduplication_entries = max_deduplication_entries

    def dispatch_update(self, update: dict) -> None:
        """Route one update by callback namespace or message shape."""

        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if update_id in self._seen_update_ids:
                return
            self._seen_update_ids[update_id] = None
            while len(self._seen_update_ids) > self._max_deduplication_entries:
                self._seen_update_ids.popitem(last=False)

        callback = update.get("callback_query")
        callback_data = callback.get("data") if isinstance(callback, dict) else None
        if isinstance(callback_data, str) and callback_data.startswith(
            f"{APPROVAL_CALLBACK_NAMESPACE}:"
        ):
            self._dispatch_approval_callback(callback)
            return

        if self.update_handler is not None:
            self.update_handler(update)

    def _dispatch_approval_callback(self, callback: dict) -> None:
        callback_query_id = callback.get("id")
        if not isinstance(callback_query_id, str) or not callback_query_id:
            return
        decision = parse_approval_callback(callback, self.transport.config.allowed_chat_id)
        if decision is None:
            if _callback_chat_id(callback) == self.transport.config.allowed_chat_id:
                self.transport.answer_callback_query(
                    callback_query_id,
                    text="Invalid approval",
                )
            return

        result = self.approval_broker.resolve_callback(decision, callback_query_id)
        if result.status == ApprovalDispatchStatus.CONTEXT and result.approval is not None:
            self.transport.send_context(result.approval)
            acknowledgement = "Context sent"
        elif result.status == ApprovalDispatchStatus.FINAL:
            acknowledgement = f"Durex: {decision.action.value}"
        elif result.status == ApprovalDispatchStatus.DUPLICATE:
            acknowledgement = "Approval already handled"
        else:
            acknowledgement = "Approval expired"
        self.transport.answer_callback_query(callback_query_id, text=acknowledgement)

    def run_forever(self) -> None:
        """Poll and dispatch until stopped, retrying transient transport errors."""

        retry_delay = self.retry_base_seconds
        try:
            while not self._stop_event.is_set():
                try:
                    updates = self.transport.poll_updates(
                        timeout=self.poll_timeout_seconds,
                        allowed_updates=list(self.allowed_updates),
                    )
                except TelegramBridgeError as exc:
                    if self.on_poll_error is not None:
                        self.on_poll_error(exc)
                    if self._stop_event.wait(retry_delay):
                        break
                    retry_delay = min(retry_delay * 2, self.retry_max_seconds)
                    continue

                retry_delay = self.retry_base_seconds
                for update in updates:
                    if self._stop_event.is_set():
                        break
                    try:
                        self.dispatch_update(update)
                    except TelegramBridgeError as exc:
                        if self.on_poll_error is not None:
                            self.on_poll_error(exc)
        finally:
            self.approval_broker.shutdown()

    def stop(self) -> None:
        """Request dispatcher shutdown and release approval waiters."""

        self._stop_event.set()
        self.approval_broker.shutdown()


class StandaloneTelegramApprovalRuntime:
    """Dispatcher thread used by standalone ``run --telegram`` mode."""

    def __init__(
        self,
        transport: TelegramApprovalTransport,
        poll_timeout_seconds: int = 1,
    ) -> None:
        self.broker = TelegramApprovalBroker()
        config = transport.config
        self.gateway = TelegramApprovalGateway(
            transport=transport,
            broker=self.broker,
            timeout_seconds=config.approval_timeout_seconds,
            timeout_default=config.timeout_default_decision,
        )
        self.dispatcher = TelegramUpdateDispatcher(
            transport=transport,
            approval_broker=self.broker,
            poll_timeout_seconds=poll_timeout_seconds,
            allowed_updates=["callback_query"],
        )
        self._thread: Optional[threading.Thread] = None

    def _run_dispatcher(self) -> None:
        """Run the poll loop and release process ownership only after exit."""

        try:
            self.dispatcher.run_forever()
        finally:
            _standalone_dispatcher_lock.release()

    def start(self) -> ApprovalDecisionProvider:
        """Start the sole polling thread and return its decision gateway."""

        if self._thread is not None:
            raise TelegramBridgeError("Standalone Telegram approval runtime is already started.")
        if not _standalone_dispatcher_lock.acquire(blocking=False):
            raise TelegramBridgeError(
                "Another standalone Telegram update dispatcher is still active."
            )
        self._thread = threading.Thread(
            target=self._run_dispatcher,
            name="durex-telegram-dispatcher",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            _standalone_dispatcher_lock.release()
            raise
        return self.gateway

    def close(self) -> None:
        """Stop polling and wait for bounded in-flight transport calls."""

        self.dispatcher.stop()
        if self._thread is not None:
            shutdown_timeout = max(
                2,
                self.dispatcher.poll_timeout_seconds + 6,
                (
                    DEFAULT_TELEGRAM_API_TIMEOUT_SECONDS
                    * MAX_APPROVAL_CALLBACK_API_CALLS
                )
                + 1,
            )
            self._thread.join(timeout=shutdown_timeout)
            if self._thread.is_alive():
                raise TelegramBridgeError("Telegram update dispatcher did not stop.")
