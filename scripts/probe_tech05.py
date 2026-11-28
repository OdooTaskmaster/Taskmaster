"""Verify the candidate RabbitMQ transport against an isolated Odoo project."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pika

if TYPE_CHECKING:
    from odoo.api import Environment

_MARKER: bytes = b'{"version":1,"operation_id":"synthetic-probe"}'


@contextmanager
def _channel() -> Iterator[pika.adapters.blocking_connection.BlockingChannel]:
    """Open a confirm-enabled channel against the explicitly configured local broker.

    Read TASKMASTER_PROBE_AMQP_URL from the environment; close on exit.
    Example: with _channel() as channel: ... can publish with confirms.
    """
    url: str = os.environ.get("TASKMASTER_PROBE_AMQP_URL", "")
    if not url:
        raise ValueError("Set TASKMASTER_PROBE_AMQP_URL to the isolated broker URL.")
    try:
        parameters: pika.URLParameters = pika.URLParameters(url)
    except (TypeError, ValueError):
        raise ValueError("Invalid probe broker URL.") from None
    if parameters.host != "127.0.0.1" or parameters.port == 5672:
        raise ValueError("Use 127.0.0.1 and a dedicated non-default AMQP port.")
    parameters.connection_attempts = 1
    parameters.socket_timeout = 3
    parameters.stack_timeout = 5
    parameters.blocked_connection_timeout = 5
    with pika.BlockingConnection(parameters) as connection:
        channel: pika.adapters.blocking_connection.BlockingChannel = connection.channel()
        channel.confirm_delivery()
        yield channel


def _declare(channel: pika.adapters.blocking_connection.BlockingChannel, queue: str) -> None:
    """Declare a durable one-slot probe queue that rejects new publications.

    Example: _declare(channel, queue) creates the queue and returns None.
    """
    channel.queue_declare(queue=queue, durable=True, arguments={
        "x-queue-type": "classic", "x-max-length": 1, "x-overflow": "reject-publish",
    })


def _publish(channel: pika.adapters.blocking_connection.BlockingChannel, queue: str) -> None:
    """Publish the fixed persistent marker or raise on a negative confirmation.

    Example: _publish(channel, queue) returns None after broker confirmation.
    """
    channel.basic_publish(
        exchange="", routing_key=queue, body=_MARKER, mandatory=True,
        properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent, content_type="application/json"),
    )


def run(env: "Environment", expected_db: str, user_id: int, project_id: int) -> tuple[str, ...]:
    """Check overflow, redelivery and a new non-elevated Odoo context without Odoo writes.

    The IDs must refer to a synthetic project and an ordinary test user who
    can read it. Example: run(env, 'tech05_test', 7, 3) returns three check labels.
    """
    from odoo import SUPERUSER_ID
    from odoo.api import Environment

    if not expected_db or env.cr.dbname != expected_db or user_id <= SUPERUSER_ID or project_id <= 0:
        raise ValueError("Pass the exact test database and positive fixture IDs for a non-root user.")
    queue: str = f"taskmaster.tech05.{uuid4().hex}"
    try:
        with _channel() as channel:
            _declare(channel, queue)
            _publish(channel, queue)
            try:
                _publish(channel, queue)
            except pika.exceptions.NackError:
                pass
            else:
                raise AssertionError("A full probe queue accepted the second marker.")
            delivery, _, body = channel.basic_get(queue, auto_ack=False)
            if delivery is None or delivery.redelivered or body != _MARKER:
                raise AssertionError("Initial delivery was not the original marker.")
        with _channel() as channel:
            delivery, _, body = channel.basic_get(queue, auto_ack=False)
            if delivery is None or not delivery.redelivered or body != _MARKER:
                raise AssertionError("The unacknowledged marker was not redelivered after disconnect.")
            with env.registry.cursor() as cursor:
                worker: Environment = Environment(cursor, user_id, {}, su=False)
                if (
                    worker.cr.dbname != expected_db or worker.su or worker.uid != user_id
                    or not worker.user.active or not worker.user.has_group("base.group_user")
                    or not worker["project.project"].search_count([("id", "=", project_id)])
                ):
                    raise AssertionError("The fresh Odoo context could not read the synthetic project.")
            channel.basic_ack(delivery.delivery_tag)
            if channel.basic_get(queue)[0] is not None:
                raise AssertionError("The rejected marker unexpectedly entered the queue.")
    finally:
        with _channel() as channel:
            channel.queue_delete(queue=queue)
    return ("overflow rejected", "unacknowledged marker redelivered", "non-elevated Odoo context verified")


def restart_probe(token: str, *, publish: bool) -> str:
    """Publish a marker before broker restart or consume and delete its queue afterward.

    Use a fresh UUID, once for each phase. Example: restart_probe(token,
    publish=False) returns 'persistent marker received' after broker restart.
    """
    queue: str = f"taskmaster.tech05.restart.{UUID(token).hex}"
    with _channel() as channel:
        if publish:
            _declare(channel, queue)
            _publish(channel, queue)
            return "persistent marker confirmed"
        delivery, _, body = channel.basic_get(queue, auto_ack=False)
        if delivery is None or body != _MARKER:
            raise AssertionError("The persistent marker was not found after restart.")
        channel.basic_ack(delivery.delivery_tag)
        channel.queue_delete(queue=queue)
        return "persistent marker received"


def verify_unavailable() -> str:
    """Verify a stopped local broker cannot be mistaken for a confirmed publication.

    Example: verify_unavailable() returns 'broker connection failed' while it is down.
    """
    try:
        with _channel():
            pass
    except pika.exceptions.AMQPConnectionError:
        return "broker connection failed"
    raise AssertionError("The probe broker was still reachable.")
