"""Publish and consume strict operation references through RabbitMQ."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
import importlib
import json
import math
import re
import threading
from typing import Any
from urllib.parse import urlsplit

MAX_QUEUE_LENGTH: int = 2**31 - 1
MAX_TIMEOUT_SECONDS: float = 30.0
MAX_MESSAGE_BYTES: int = 512
_OPERATION_ID: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_QUEUE_ARGUMENTS_KEY: str = "x-queue-type"


class BrokerUnavailable(Exception):
    """Report an unavailable broker or Pika client without internal details.

    Example: raise BrokerUnavailable() produces a fixed safe message.
    """

    __slots__ = ()

    def __init__(self) -> None:
        """Construct a fixed message with no inputs; BrokerUnavailable() returns the error."""
        super().__init__("The message broker is unavailable.")


class _InvalidMessage(Exception):
    """Mark one consumed body as malformed so the caller can discard it safely."""

    __slots__ = ()


class _PendingPublish:
    """Track the single publication in flight until RabbitMQ settles it.

    The tag arrives from the publisher callback; a mandatory return settles the
    publication as failed before the broker's confirmation arrives. Example:
    pending.wait(5.0) returns True only for a routed and confirmed message.
    """

    __slots__ = ("_lock", "_tag", "_done", "_confirmed", "_returned", "_deferred")

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._tag: int | None = None
        self._done: threading.Event = threading.Event()
        self._confirmed: bool = False
        self._returned: bool = False
        self._deferred: tuple[int, bool, bool] | None = None

    def record_tag(self, delivery_tag: int) -> None:
        """Remember the delivery tag reported by basic_publish.

        Example: channel.basic_publish(..., on_publish=pending.record_tag)
        lets a later confirm match this publication.
        """
        with self._lock:
            if self._done.is_set() or type(delivery_tag) is not int or delivery_tag <= 0:
                return
            self._tag = delivery_tag
            deferred: tuple[int, bool, bool] | None = self._deferred
            self._deferred = None
            if deferred is not None:
                self._settle(*deferred)

    def confirm(self, delivery_tag: int, multiple: bool, acknowledged: bool) -> None:
        """Settle the publication from one Basic.Ack or Basic.Nack frame.

        Example: pending.confirm(1, False, True) records a single ack.
        """
        with self._lock:
            if self._done.is_set():
                return
            if self._tag is None:
                self._deferred = (delivery_tag, multiple, acknowledged)
                return
            self._settle(delivery_tag, multiple, acknowledged)

    def mark_returned(self) -> None:
        """Settle the publication as unroutable when RabbitMQ returns it.

        Example: pending.mark_returned() makes wait() report failure.
        """
        with self._lock:
            self._returned = True
            self._done.set()

    def wait(self, timeout: float) -> bool:
        """Wait for one confirmation and report a routed, acknowledged message.

        Example: pending.wait(5.0) returns False on a nack, a mandatory
        return or a bounded timeout.
        """
        self._done.wait(timeout)
        with self._lock:
            return self._confirmed and not self._returned

    def settled(self) -> bool:
        """Return True once a confirm or mandatory return has been processed.

        Example: pending.settled() is False when the bounded wait expired
        without any broker callback.
        """
        with self._lock:
            return self._done.is_set()

    def _settle(self, delivery_tag: int, multiple: bool, acknowledged: bool) -> None:
        """Apply one confirm under the caller's lock; no value is returned."""
        if type(self._tag) is not int:
            return
        if multiple:
            matches: bool = delivery_tag >= self._tag
        else:
            matches = delivery_tag == self._tag
        if not matches:
            return
        self._confirmed = acknowledged
        self._done.set()


@dataclass(slots=True)
class _ChannelState:
    """Hold one connection, its channel and the publication in flight."""

    pika: Any
    connection: Any
    channel: Any
    pending: _PendingPublish | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class RabbitQueue:
    """Publish operation IDs to, and consume them from, one durable RabbitMQ queue.

    The queue is classic and durable, capped by max_length with the
    reject-publish overflow policy. Each operation is one persistent JSON body
    with exactly version 1 and an opaque operation_id. The connection uses the
    thread-safe Pika 1.5 client, one channel, prefetch 1 and at most one
    publication or delivery outstanding. timeout bounds every broker call and
    confirm wait. Example: RabbitQueue(url, "taskmaster.operations.db", 1000).
    """

    def __init__(self, url: str, queue: str, max_length: int, *, timeout: float = 5.0) -> None:
        """Validate configuration without importing Pika or connecting.

        Example: RabbitQueue("amqp://guest:guest@localhost/", "operations", 100)
        creates a lazy queue handle.
        """
        self._url: str = _checked_url(url)
        self._queue: str = _checked_queue(queue)
        self._max_length: int = _checked_max_length(max_length)
        self._timeout: float = _checked_timeout(timeout)
        self._lock: threading.RLock = threading.RLock()
        self._state: _ChannelState | None = None
        self._receiving: bool = False

    def publish(self, operation_id: str) -> bool:
        """Publish one persistent reference and report a routed confirmation.

        Return False when the operation ID is not a bounded ASCII opaque key,
        Pika is missing, the transport fails, RabbitMQ rejects the message, the
        queue cannot route it, or the bounded confirm wait expires. Example:
        queue.publish("op-7f3a") returns True after ack.
        """
        try:
            checked_id: str = _checked_operation_id(operation_id)
        except ValueError:
            return False
        body: bytes = _encode_message(checked_id)
        with self._lock:
            try:
                state: _ChannelState = self._channel_state()
            except BrokerUnavailable:
                return False
            pending: _PendingPublish = _PendingPublish()
            with state.lock:
                state.pending = pending
            try:
                state.channel.basic_publish(
                    exchange="",
                    routing_key=self._queue,
                    body=body,
                    properties=state.pika.BasicProperties(
                        delivery_mode=state.pika.DeliveryMode.Persistent,
                        content_type="application/json",
                    ),
                    mandatory=True,
                    on_publish=pending.record_tag,
                )
                if pending.wait(self._timeout):
                    return True
            except Exception:
                pass
            finally:
                with state.lock:
                    if state.pending is pending:
                        state.pending = None
            if pending.settled():
                return False
            self._reset()
            return False

    def receive(self, handle: Callable[[str], None]) -> bool:
        """Consume at most one message and acknowledge it only after handle returns.

        Return False for an empty queue. A malformed message is acknowledged
        and discarded, returning True without calling handle. If handle raises,
        the connection is closed so RabbitMQ requeues the message and the
        original exception is propagated unchanged. Transport and
        acknowledgement failures raise BrokerUnavailable. Example:
        queue.receive(process_operation) returns True after one valid message.
        """
        if not callable(handle):
            raise TypeError("handle must be callable.")
        with self._lock:
            if self._receiving:
                raise RuntimeError("receive is not reentrant.")
            self._receiving = True
            try:
                state: _ChannelState = self._channel_state()
                channel: Any = state.channel
                try:
                    method, _properties, body = channel.basic_get(
                        self._queue, auto_ack=False, timeout=self._timeout,
                    )
                except Exception:
                    self._reset()
                    raise BrokerUnavailable() from None
                if method is None:
                    return False
                delivery_tag: Any = getattr(method, "delivery_tag", None)
                if type(delivery_tag) is not int or delivery_tag <= 0:
                    self._reset()
                    raise BrokerUnavailable()
                try:
                    operation_id: str = _decode_message(body)
                except _InvalidMessage:
                    try:
                        channel.basic_ack(delivery_tag)
                    except Exception:
                        self._reset()
                        raise BrokerUnavailable() from None
                    return True
                try:
                    handle(operation_id)
                except BaseException:
                    self._reset()
                    raise
                try:
                    channel.basic_ack(delivery_tag)
                except Exception:
                    self._reset()
                    raise BrokerUnavailable() from None
                return True
            finally:
                self._receiving = False

    def close(self) -> None:
        """Close the channel and connection, releasing their background thread.

        A later publish or receive opens a new connection. Example:
        queue.close() is safe to call more than once.
        """
        with self._lock:
            self._reset()

    def _channel_state(self) -> _ChannelState:
        """Return an open channel, reconnecting lazily or raise BrokerUnavailable.

        Example: queue._channel_state() declares the durable capped queue once
        per connection.
        """
        state: _ChannelState | None = self._state
        if state is not None and state.channel.is_open:
            return state
        self._reset()
        pika_module: Any = _load_pika()
        connection: Any = None
        channel: Any = None
        try:
            parameters: Any = pika_module.URLParameters(self._url)
            parameters.connection_attempts = 1
            parameters.socket_timeout = self._timeout
            parameters.stack_timeout = self._timeout
            parameters.blocked_connection_timeout = self._timeout
            connection = pika_module.Connection(parameters, timeout=self._timeout)
            channel = connection.channel(timeout=self._timeout)
            channel.basic_qos(prefetch_count=1, timeout=self._timeout)
            state = _ChannelState(pika=pika_module, connection=connection, channel=channel)
            ack_class: Any = pika_module.spec.Basic.Ack
            channel.confirm_delivery(
                lambda frame: self._on_confirm(state, frame, ack_class),
                timeout=self._timeout,
            )
            channel.add_on_return_callback(
                lambda returned_channel, method, properties, body: self._on_return(state),
            )
            channel.queue_declare(
                queue=self._queue,
                durable=True,
                arguments={
                    _QUEUE_ARGUMENTS_KEY: "classic",
                    "x-max-length": self._max_length,
                    "x-overflow": "reject-publish",
                },
                timeout=self._timeout,
            )
            self._state = state
            return state
        except Exception:
            _abort(channel, self._timeout)
            _abort(connection, self._timeout)
            raise BrokerUnavailable() from None

    def _on_confirm(self, state: _ChannelState, method_frame: Any, ack_class: Any) -> None:
        """Route one Basic.Ack or Basic.Nack to the publication in flight.

        Example: Pika calls this on its channel worker; no value is returned.
        """
        method: Any = getattr(method_frame, "method", None)
        if method is None:
            return
        with state.lock:
            pending: _PendingPublish | None = state.pending
        if pending is None:
            return
        delivery_tag: Any = getattr(method, "delivery_tag", None)
        if type(delivery_tag) is not int:
            return
        pending.confirm(
            delivery_tag,
            bool(getattr(method, "multiple", False)),
            isinstance(method, ack_class),
        )

    def _on_return(self, state: _ChannelState) -> None:
        """Mark a mandatory publication as unroutable before its ack arrives.

        Example: Pika calls this on its channel worker; no value is returned.
        """
        with state.lock:
            pending: _PendingPublish | None = state.pending
        if pending is not None:
            pending.mark_returned()

    def _reset(self) -> None:
        """Drop the current connection without letting cleanup errors escape.

        Example: queue._reset() is used after a timeout or handler failure.
        """
        state: _ChannelState | None = self._state
        self._state = None
        if state is None:
            return
        with state.lock:
            state.pending = None
        _abort(state.channel, self._timeout)
        _abort(state.connection, self._timeout)


def _load_pika() -> Any:
    """Import Pika on first use so the dependency remains optional.

    Example: adapters.rabbitmq._load_pika() returns the installed module or
    raises BrokerUnavailable when it cannot be imported.
    """
    try:
        return importlib.import_module("pika")
    except Exception:
        raise BrokerUnavailable() from None


def _checked_url(value: str) -> str:
    """Return an unmodified amqp or amqps URL without echoing it in errors.

    Example: _checked_url("amqp://guest:guest@localhost/") returns the URL.
    """
    if (not isinstance(value, str) or not value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("url must be an amqp or amqps URL.")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("url must be an amqp or amqps URL.") from None
    if parsed.scheme not in {"amqp", "amqps"} or not parsed.netloc:
        raise ValueError("url must be an amqp or amqps URL.")
    return value


def _checked_queue(value: str) -> str:
    """Return a non-empty queue name of at most 255 UTF-8 bytes without controls.

    Example: _checked_queue("taskmaster.operations.db") returns the name.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("queue must be a non-empty name of at most 255 UTF-8 bytes.")
    try:
        size: int = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("queue must be a non-empty name of at most 255 UTF-8 bytes.") from None
    if size > 255 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("queue must be a non-empty name of at most 255 UTF-8 bytes.")
    return value


def _checked_max_length(value: int) -> int:
    """Return a positive queue cap that RabbitMQ can accept.

    Example: _checked_max_length(1000) returns 1000.
    """
    if type(value) is not int or not 1 <= value <= MAX_QUEUE_LENGTH:
        raise ValueError("max_length must be a positive integer.")
    return value


def _checked_timeout(value: float) -> float:
    """Return a finite, positive and bounded number of seconds.

    Example: _checked_timeout(5) returns 5.0.
    """
    if (type(value) not in (int, float) or isinstance(value, bool)
            or not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_SECONDS):
        raise ValueError(
            f"timeout must be a finite number of seconds between 0 and {MAX_TIMEOUT_SECONDS:g}."
        )
    return float(value)


def _checked_operation_id(value: str) -> str:
    """Return a bounded ASCII opaque key or raise ValueError without echoing it.

    Example: _checked_operation_id("op-7f3a") returns the key.
    """
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,127}.")
    return value


def _encode_message(operation_id: str) -> bytes:
    """Serialize exactly version 1 and the operation ID as strict compact JSON.

    Example: _encode_message("op-1") returns
    b'{"version":1,"operation_id":"op-1"}'.
    """
    return json.dumps(
        {"version": 1, "operation_id": operation_id},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _decode_message(body: Any) -> str:
    """Return the operation ID from a strict body or raise _InvalidMessage.

    Reject non-ASCII, duplicate keys, extra or missing fields, a version other
    than the integer 1, an invalid key and any body over MAX_MESSAGE_BYTES.
    Example: _decode_message(b'{"version":1,"operation_id":"op-1"}') returns
    "op-1".
    """
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_MESSAGE_BYTES:
        raise _InvalidMessage()
    try:
        text: str = bytes(body).decode("ascii")
        data: Any = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise _InvalidMessage() from None
    if not isinstance(data, dict) or set(data) != {"version", "operation_id"}:
        raise _InvalidMessage()
    version: Any = data["version"]
    operation_id: Any = data["operation_id"]
    if type(version) is not int or version != 1:
        raise _InvalidMessage()
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise _InvalidMessage()
    return operation_id


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object while rejecting duplicate JSON keys.

    Example: json.loads('{"a":1}', object_pairs_hook=_unique_object) returns
    {"a": 1}.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Reject NaN and Infinity, which are not part of strict JSON.

    Example: json.loads('NaN', parse_constant=_reject_constant) raises
    ValueError.
    """
    raise ValueError("non-finite number")


def _abort(handle: Any, timeout: float) -> None:
    """Close one Pika handle while suppressing every cleanup error.

    Example: _abort(channel, 5.0) leaves the caller free to raise the original
    error.
    """
    if handle is None:
        return
    with suppress(Exception):
        handle.abort(timeout=timeout)


__all__ = ["BrokerUnavailable", "RabbitQueue"]
