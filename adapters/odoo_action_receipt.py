"""Persist a confirmed write and its receipt in the same Odoo transaction."""

from collections.abc import Callable
from typing import TYPE_CHECKING

if __package__ and "." in __package__:
    from ..application.confirmed_action import Proposal
    from ..application.idempotent_action import AppliedAction, idempotency_key
else:
    from application.confirmed_action import Proposal
    from application.idempotent_action import AppliedAction, idempotency_key

if TYPE_CHECKING:
    from odoo.api import Environment


class OdooActionReceiptWriter:
    """Serialize confirmed writes by key using an internal PostgreSQL receipt."""

    def __init__(self, env: "Environment", write: Callable[["Environment", Proposal], str]) -> None:
        """Bind the request environment and a trusted write using that same cursor.

        The callback must obey normal Odoo ACLs and must not commit or use an external side effect.
        Example: OdooActionReceiptWriter(env, write) can be bound behind the confirmation gate.
        """
        self._env: Environment = env
        self._write: Callable[[Environment, Proposal], str] = write

    def apply(self, actor_id: int, proposal: Proposal, *, key: str) -> AppliedAction:
        """Use the request transaction to reserve, write, and record one exact result.

        A failure rolls back the savepoint even if the confirmation gate catches it.
        Example: apply(7, proposal, key=key) returns AppliedAction('42') without rewriting on repeat.
        """
        env: Environment = self._env
        if type(actor_id) is not int or actor_id <= 0 or env.su or env.uid != actor_id or key != idempotency_key(proposal):
            raise PermissionError("The confirmed write scope is unavailable.")
        with env.cr.savepoint():
            env.cr.execute(
                "INSERT INTO taskmaster_action_receipt (action_key) VALUES (%s) "
                "ON CONFLICT (action_key) DO NOTHING RETURNING id", (key,),
            )
            reserved: tuple[int] | None = env.cr.fetchone()
            if reserved is None:
                env.cr.execute("SELECT reference FROM taskmaster_action_receipt WHERE action_key = %s", (key,))
                existing: tuple[str] | None = env.cr.fetchone()
                if existing is None:
                    raise RuntimeError("The receipt could not be verified.")
                return AppliedAction(existing[0])
            result: AppliedAction = AppliedAction(self._write(env, proposal))
            env.cr.execute("UPDATE taskmaster_action_receipt SET reference = %s WHERE id = %s",
                           (result.reference, reserved[0]))
            return result
