"""Check strict RabbitMQ publishing, delivery and safe failure behavior."""

from collections import deque
import inspect
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Callable
import time

import pytest

from adapters import rabbitmq
from adapters.rabbitmq import BrokerUnavailable, RabbitQueue

_URL: str = "amqp://guest:guest@localhost:5672/%2F"
_QUEUE: str = "taskmaster.operations.test"
_VALID: bytes = b'{"version":1,"operation_id":"op-1"}'


class _Ack:
    """Stand in for pika.spec.Basic.Ack in the fake transport."""

    def __init__(self, delivery_tag: int, multiple: bool = False) -> None:
        self.delivery_tag: int = delivery_tag
        self.multiple: bool = multiple


class _Nack:
    """Stand in for pika.spec.Basic.Nack in the fake transport."""

    def __init__(self, delivery_tag: int, multiple: bool = False) -> None:
        self.delivery_tag: int = delivery_tag
        self.multiple: bool = multiple


class _Frame:
    """Wrap one method the way Pika passes a confirm frame."""

    def __init__(self, method: Any) -> None:
        self.method: Any = method


class _Properties:
    """Record BasicProperties fields used by the adapter."""

    def __init__(self, delivery_mode: int = 1, content_type: str | None = None) -> None:
        self.delivery_mode: int = delivery_mode
        self.content_type: str | None = content_type


class _URLParameters:
    """Record URL settings and allow the adapter to tighten them."""

    def __init__(self, url: str) -> None:
        self.url: str = url
        self.connection_attempts: int = 0
        self.socket_timeout: float = 0.0
        self.stack_timeout: float = 0.0
        self.blocked_connection_timeout: float = 0.0


class _FakeChannel:
    """Emulate the Pika 1.5 thread-safe channel surface used by the adapter."""

    def __init__(self, broker: "_FakeBroker") -> None:
        self.broker: _FakeBroker = broker
        self.is_open: bool = True
        self.aborted: bool = False
        self.next_tag: int = 1
        self.queue_declare_kwargs: dict[str, Any] | None = None
        self.qos_kwargs: dict[str, Any] | None = None
        self.confirm_timeout: float | None = None
        self.confirm_callback: Callable[[Any], None] | None = None
        self.return_callback: Callable[..., None] | None = None
        self.publishes: list[dict[str, Any]] = []
        self.ack_calls: list[int] = []
        self.nack_calls: list[tuple[int, bool]] = []

    def queue_declare(self, **kwargs: Any) -> object:
        self.queue_declare_kwargs = dict(kwargs)
        return object()

    def basic_qos(self, **kwargs: Any) -> object:
        self.qos_kwargs = dict(kwargs)
        return object()

    def confirm_delivery(self, callback: Callable[[Any], None], timeout: float | None = None) -> object:
        self.confirm_callback = callback
        self.confirm_timeout = timeout
        return object()

    def add_on_return_callback(self, callback: Callable[..., None]) -> None:
        self.return_callback = callback

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: _Properties | None = None,
        mandatory: bool = False,
        on_publish: Callable[[int], None] | None = None,
    ) -> None:
        if self.broker.publish_error is not None:
            raise self.broker.publish_error
        tag: int = self.next_tag
        self.next_tag += 1
        self.publishes.append({
            "exchange": exchange,
            "routing_key": routing_key,
            "body": body,
            "properties": properties,
            "mandatory": mandatory,
        })
        if on_publish is not None:
            on_publish(tag)
        behavior: str = self.broker.publish_behavior
        if behavior == "ack":
            self.confirm(tag)
        elif behavior == "nack":
            self.nack(tag)
        elif behavior == "return":
            self.returned(tag)
            self.confirm(tag)

    def confirm(self, delivery_tag: int, multiple: bool = False) -> None:
        if self.confirm_callback is not None:
            self.confirm_callback(_Frame(_Ack(delivery_tag, multiple)))

    def nack(self, delivery_tag: int, multiple: bool = False) -> None:
        if self.confirm_callback is not None:
            self.confirm_callback(_Frame(_Nack(delivery_tag, multiple)))

    def returned(self, delivery_tag: int) -> None:
        if self.return_callback is not None:
            self.return_callback(self, SimpleNamespace(delivery_tag=delivery_tag), _Properties(), b"")

    def basic_get(
        self, queue: str, auto_ack: bool = False, timeout: float | None = None,
    ) -> tuple[Any, Any, Any]:
        if self.broker.get_error is not None:
            raise self.broker.get_error
        if not self.broker.messages:
            return (None, None, None)
        delivery_tag: int
        body: Any
        delivery_tag, body = self.broker.messages.popleft()
        return (SimpleNamespace(delivery_tag=delivery_tag, redelivered=False), _Properties(), body)

    def basic_ack(self, delivery_tag: int = 0, multiple: bool = False) -> None:
        if self.broker.ack_error is not None:
            raise self.broker.ack_error
        self.ack_calls.append(delivery_tag)

    def basic_nack(self, delivery_tag: int = 0, multiple: bool = False, requeue: bool = True) -> None:
        self.nack_calls.append((delivery_tag, requeue))

    def abort(self, timeout: float | None = None) -> None:
        self.aborted = True
        self.is_open = False


class _FakeConnection:
    """Provide one fake channel and record the bounded connect timeout."""

    def __init__(self, broker: "_FakeBroker", parameters: _URLParameters, timeout: float | None) -> None:
        self.broker: _FakeBroker = broker
        self.parameters: _URLParameters = parameters
        self.timeout: float | None = timeout
        self.channel_obj: _FakeChannel = _FakeChannel(broker)
        self.channel_timeout: float | None = None
        self.aborted: bool = False

    def channel(self, timeout: float | None = None) -> _FakeChannel:
        self.channel_timeout = timeout
        return self.channel_obj

    def abort(self, timeout: float | None = None) -> None:
        self.aborted = True


class _FakeBroker:
    """Supply the Pika module surface, transport faults and queued deliveries."""

    def __init__(self) -> None:
        self.connection_error: BaseException | None = None
        self.publish_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.ack_error: BaseException | None = None
        self.publish_behavior: str = "ack"
        self.messages: deque[tuple[int, Any]] = deque()
        self.connections: list[_FakeConnection] = []
        self.pika: SimpleNamespace = SimpleNamespace(
            URLParameters=_URLParameters,
            Connection=self.connect,
            BasicProperties=_Properties,
            DeliveryMode=SimpleNamespace(Persistent=2),
            spec=SimpleNamespace(Basic=SimpleNamespace(Ack=_Ack, Nack=_Nack)),
        )

    def connect(self, parameters: _URLParameters, timeout: float | None = None) -> _FakeConnection:
        if self.connection_error is not None:
            raise self.connection_error
        connection: _FakeConnection = _FakeConnection(self, parameters, timeout)
        self.connections.append(connection)
        return connection

    @property
    def channel(self) -> _FakeChannel:
        return self.connections[-1].channel_obj


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> _FakeBroker:
    """Install the fake Pika module; broker() returns its transport controls."""
    fake: _FakeBroker = _FakeBroker()
    monkeypatch.setattr(rabbitmq, "_load_pika", lambda: fake.pika)
    return fake


@pytest.fixture
def queue(broker: _FakeBroker) -> RabbitQueue:
    """Create a queue with a short bounded confirm wait; queue() returns it."""
    return RabbitQueue(_URL, _QUEUE, 100, timeout=0.5)


def test_configuration_rejects_unsafe_or_unbounded_values() -> None:
    """Assert invalid URLs, queue caps and timeouts fail early; test returns None."""
    with pytest.raises(ValueError) as rejected:
        RabbitQueue("http://user:secret@localhost/", _QUEUE, 1)
    assert "secret" not in str(rejected.value)
    for url in ("", "http://localhost/", "amqp://", "amqp://host/ path", 7):
        with pytest.raises(ValueError):
            RabbitQueue(url, _QUEUE, 1)  # type: ignore[arg-type]
    for name in ("", "\x00bad", "q" * 256, "\ud800", 7):
        with pytest.raises(ValueError):
            RabbitQueue(_URL, name, 1)  # type: ignore[arg-type]
    for cap in (0, -1, True, 2.5, rabbitmq.MAX_QUEUE_LENGTH + 1):
        with pytest.raises(ValueError):
            RabbitQueue(_URL, _QUEUE, cap)  # type: ignore[arg-type]
    for timeout in (0, -1, float("nan"), float("inf"), rabbitmq.MAX_TIMEOUT_SECONDS + 1, "5"):
        with pytest.raises(ValueError):
            RabbitQueue(_URL, _QUEUE, 1, timeout=timeout)  # type: ignore[arg-type]


def test_publish_declares_the_approved_contract_and_confirms(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert exact queue, JSON, delivery mode and confirm settings; return None."""
    assert queue.publish("op-1") is True
    channel: _FakeChannel = broker.channel
    assert channel.queue_declare_kwargs == {
        "queue": _QUEUE,
        "durable": True,
        "arguments": {
            "x-queue-type": "classic",
            "x-max-length": 100,
            "x-overflow": "reject-publish",
        },
        "timeout": 0.5,
    }
    assert channel.qos_kwargs == {"prefetch_count": 1, "timeout": 0.5}
    assert channel.confirm_timeout == 0.5
    assert broker.connections[0].timeout == 0.5
    assert broker.connections[0].channel_timeout == 0.5
    assert broker.connections[0].parameters.connection_attempts == 1
    assert broker.connections[0].parameters.socket_timeout == 0.5
    assert broker.connections[0].parameters.stack_timeout == 0.5
    assert broker.connections[0].parameters.blocked_connection_timeout == 0.5
    assert len(channel.publishes) == 1
    published: dict[str, Any] = channel.publishes[0]
    assert published["exchange"] == ""
    assert published["routing_key"] == _QUEUE
    assert published["body"] == _VALID
    assert published["mandatory"] is True
    assert published["properties"].delivery_mode == 2
    assert published["properties"].content_type == "application/json"


def test_publish_is_false_on_nack_or_mandatory_return(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert broker rejection and an unroutable return are false; return None."""
    broker.publish_behavior = "nack"
    assert queue.publish("op-1") is False
    broker.publish_behavior = "return"
    assert queue.publish("op-2") is False
    assert broker.channel.return_callback is not None
    assert [published["body"] for published in broker.channel.publishes] == [
        b'{"version":1,"operation_id":"op-1"}',
        b'{"version":1,"operation_id":"op-2"}',
    ]
    assert len(broker.connections) == 1
    assert broker.channel.aborted is False


def test_publish_timeout_is_bounded_and_reconnects(broker: _FakeBroker) -> None:
    """Assert a missing confirm returns false within bound and forces reconnect."""
    queue: RabbitQueue = RabbitQueue(_URL, _QUEUE, 100, timeout=0.05)
    broker.publish_behavior = "silent"
    started: float = time.monotonic()
    assert queue.publish("op-1") is False
    assert time.monotonic() - started < 2.0
    assert broker.connections[0].channel_obj.aborted is True
    assert broker.connections[0].aborted is True
    broker.publish_behavior = "ack"
    assert queue.publish("op-2") is True
    assert len(broker.connections) == 2


def test_publish_is_false_on_missing_client_or_transport_failure(
    broker: _FakeBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert an absent client and transport faults never raise from publish."""
    monkeypatch.setattr(rabbitmq, "_load_pika", lambda: (_ for _ in ()).throw(BrokerUnavailable()))
    missing: RabbitQueue = RabbitQueue(_URL, _QUEUE, 100, timeout=0.05)
    assert missing.publish("op-1") is False
    monkeypatch.setattr(rabbitmq, "_load_pika", lambda: broker.pika)
    broker.connection_error = RuntimeError("connect-failed")
    assert missing.publish("op-2") is False
    broker.connection_error = None
    broker.publish_error = RuntimeError("publish-failed")
    assert missing.publish("op-3") is False
    assert broker.connections[-1].aborted is True


def test_publish_rejects_invalid_operation_id_before_the_broker(broker: _FakeBroker) -> None:
    """Assert malformed opaque keys return false without connecting."""
    queue: RabbitQueue = RabbitQueue(_URL, _QUEUE, 100)
    invalid_ids: tuple[Any, ...] = ("", "-op", "op.1", "a" * 129, "тест", 7)
    for operation_id in invalid_ids:
        assert queue.publish(operation_id) is False  # type: ignore[arg-type]
    assert broker.connections == []


def test_receive_empty_queue_returns_false(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert an empty basic_get consumes nothing and returns False."""
    calls: list[str] = []
    assert queue.receive(calls.append) is False
    assert calls == []
    assert broker.channel.ack_calls == []


def test_receive_acknowledges_only_after_handler_returns(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert the handler sees no ack and exactly one ack follows; return None."""
    broker.messages.append((11, _VALID))
    calls: list[str] = []

    def handle(operation_id: str) -> None:
        calls.append(operation_id)
        assert broker.channel.ack_calls == []

    assert queue.receive(handle) is True
    assert calls == ["op-1"]
    assert broker.channel.ack_calls == [11]
    assert broker.channel.nack_calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"null",
        b'{"version":1}',
        b'{"version":1,"operation_id":"op-1","extra":1}',
        b'{"version":2,"operation_id":"op-1"}',
        b'{"version":1.0,"operation_id":"op-1"}',
        b'{"version":true,"operation_id":"op-1"}',
        b'{"version":NaN,"operation_id":"op-1"}',
        b'{"version":1,"operation_id":1}',
        b'{"version":1,"operation_id":"-bad"}',
        b'{"version":1,"operation_id":"op.1"}',
        b'{"version":1,"operation_id":"' + b"a" * 129 + b'"}',
        b'{"version":1,"version":1,"operation_id":"op-1"}',
        '{"version":1,"operation_id":"тест"}'.encode(),
        b"x" * (rabbitmq.MAX_MESSAGE_BYTES + 1),
        None,
    ],
)
def test_receive_discards_malformed_messages(
    queue: RabbitQueue, broker: _FakeBroker, body: Any,
) -> None:
    """Assert malformed bodies are acked, never handled, and report True."""
    broker.messages.append((23, body))
    calls: list[str] = []
    assert queue.receive(calls.append) is True
    assert calls == []
    assert broker.channel.ack_calls == [23]


def test_receive_handler_error_requeues_and_propagates(
    queue: RabbitQueue, broker: _FakeBroker,
) -> None:
    """Assert a handler failure closes to requeue and keeps the original error."""
    broker.messages.append((5, _VALID))

    def handle(_operation_id: str) -> None:
        raise RuntimeError("handler-failed")

    with pytest.raises(RuntimeError, match="handler-failed"):
        queue.receive(handle)
    assert broker.channel.ack_calls == []
    assert broker.channel.aborted is True
    assert broker.connections[0].aborted is True


def test_receive_transport_and_ack_failures_raise_broker_unavailable(
    queue: RabbitQueue, broker: _FakeBroker,
) -> None:
    """Assert connection, read and ack faults are sanitized broker errors."""
    broker.connection_error = RuntimeError("connect-failed")
    with pytest.raises(BrokerUnavailable):
        queue.receive(lambda _operation_id: None)
    broker.connection_error = None
    broker.get_error = RuntimeError("read-failed")
    with pytest.raises(BrokerUnavailable):
        queue.receive(lambda _operation_id: None)
    broker.get_error = None
    broker.messages.append((7, _VALID))
    broker.ack_error = RuntimeError("ack-failed")
    with pytest.raises(BrokerUnavailable):
        queue.receive(lambda _operation_id: None)
    assert broker.channel.aborted is True


def test_receive_requires_callable_handler_and_reports_missing_client(
    broker: _FakeBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert a bad callback is a caller error and a missing client is safe."""
    queue: RabbitQueue = RabbitQueue(_URL, _QUEUE, 100)
    with pytest.raises(TypeError):
        queue.receive("not-callable")  # type: ignore[arg-type]
    assert broker.connections == []
    monkeypatch.setattr(rabbitmq, "_load_pika", lambda: (_ for _ in ()).throw(BrokerUnavailable()))
    with pytest.raises(BrokerUnavailable) as failure:
        queue.receive(lambda _operation_id: None)
    assert str(failure.value) == "The message broker is unavailable."


def test_receive_is_not_reentrant(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert a handler cannot start a second basic_get on the same worker."""
    broker.messages.append((3, _VALID))
    nested: list[str] = []

    def handle(operation_id: str) -> None:
        nested.append(operation_id)
        with pytest.raises(RuntimeError, match="not reentrant"):
            queue.receive(lambda _nested_id: None)

    assert queue.receive(handle) is True
    assert nested == ["op-1"]
    assert broker.channel.ack_calls == [3]


def test_broken_client_import_is_a_safe_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert a broken optional client cannot leak its import error."""

    def broken(_name: str) -> Any:
        raise RuntimeError("http://user:secret@localhost/")

    monkeypatch.setattr(rabbitmq, "importlib", SimpleNamespace(import_module=broken))
    with pytest.raises(BrokerUnavailable) as failure:
        rabbitmq._load_pika()
    assert str(failure.value) == "The message broker is unavailable."


def test_close_is_idempotent_and_allows_a_new_connection(queue: RabbitQueue, broker: _FakeBroker) -> None:
    """Assert close releases handles and a later operation reconnects safely."""
    assert queue.publish("op-1") is True
    queue.close()
    queue.close()
    assert broker.connections[0].channel_obj.aborted is True
    assert broker.connections[0].aborted is True
    assert queue.publish("op-2") is True
    assert len(broker.connections) == 2
    assert broker.connections[1].aborted is False


def test_rabbitmq_module_imports_without_pika() -> None:
    """Assert Pika stays an optional dependency at module import time."""
    script: str = (
        "import sys; sys.modules['pika'] = None; "
        "import adapters.rabbitmq as module; print(module.RabbitQueue.__name__)"
    )
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "RabbitQueue"


def test_pika_thread_safe_api_is_present_when_installed() -> None:
    """Assert the installed Pika exposes the 1.5 thread-safe API the adapter needs."""
    pika: Any = pytest.importorskip("pika")
    thread_safe: Any = pytest.importorskip("pika.adapters.thread_safe_connection")
    assert pika.Connection is thread_safe.Connection
    publish: set[str] = set(inspect.signature(thread_safe.Channel.basic_publish).parameters)
    assert {"mandatory", "on_publish"} <= publish
    confirm: set[str] = set(inspect.signature(thread_safe.Channel.confirm_delivery).parameters)
    assert {"ack_nack_callback", "timeout"} <= confirm
    get: set[str] = set(inspect.signature(thread_safe.Channel.basic_get).parameters)
    assert {"auto_ack", "timeout"} <= get
    assert hasattr(thread_safe.Channel, "add_on_return_callback")
    assert hasattr(thread_safe.Connection, "abort")
