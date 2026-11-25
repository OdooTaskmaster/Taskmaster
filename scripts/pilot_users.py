"""Provision or archive the test accounts from an Odoo shell."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, NamedTuple

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


class PilotAccount(NamedTuple):
    """Identify a fixture, its display name, and its Project role."""

    key: str
    name: str
    group_xmlid: str

    @property
    def login(self) -> str:
        """Return the test email used to sign in to Odoo."""
        return f"{self.legacy_login}@example.test"

    @property
    def legacy_login(self) -> str:
        """Return the original username used by existing fixtures."""
        return f"aic-pilot-{self.key}"


_NAMESPACE: str = "project_ai_conductor_pilot_users"
_MEMBER_GROUP: str = "project.group_project_user"
_ACCOUNTS: tuple[PilotAccount, ...] = (
    PilotAccount("lead", "AIC Pilot - Project Lead", "project.group_project_manager"),
    PilotAccount("a1", "AIC Pilot - Team A 1", _MEMBER_GROUP),
    PilotAccount("a2", "AIC Pilot - Team A 2", _MEMBER_GROUP),
    PilotAccount("a3", "AIC Pilot - Team A 3", _MEMBER_GROUP),
    PilotAccount("b1", "AIC Pilot - Team B 1", _MEMBER_GROUP),
    PilotAccount("b2", "AIC Pilot - Team B 2", _MEMBER_GROUP),
    PilotAccount("b3", "AIC Pilot - Team B 3", _MEMBER_GROUP),
)
Action = Literal["seed", "clean"]


class PilotUsersError(RuntimeError):
    """Report a provisioning error without including secrets."""


def _read_config(path: Path, action: Action, database: str) -> dict[str, str]:
    """Validate the target database and read passwords for seeding only."""
    try:
        stream: BinaryIO
        with path.open("rb") as stream:
            config: dict[str, object] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        raise PilotUsersError("Cannot read the pilot users TOML configuration.") from None

    configured_database: object = config.get("database")
    if not configured_database or configured_database != database:
        raise PilotUsersError("The configuration database does not match this Odoo shell.")
    if action == "clean":
        return {}

    table: object = config.get("passwords")
    if not isinstance(table, dict):
        raise PilotUsersError("Configure passwords for the seven pilot accounts.")
    passwords: dict[str, str] = {}
    for account in _ACCOUNTS:
        value: object = table.get(account.key)
        if not isinstance(value, str) or not value.strip():
            raise PilotUsersError(f"Set a nonempty password for '{account.key}'.")
        passwords[account.key] = value
    return passwords


def _find_account(users: BaseModel, markers: BaseModel, account: PilotAccount) -> BaseModel:
    """Return a fixture account or refuse to adopt a conflicting login."""
    user: BaseModel = users.search([("login", "in", [account.login, account.legacy_login])])
    marker: BaseModel = markers.search([
        ("module", "=", _NAMESPACE),
        ("name", "=", account.key),
    ])
    if not user and not marker:
        return user
    if (
        len(user) != 1
        or len(marker) != 1
        or marker.model != "res.users"
        or marker.res_id != user.id
    ):
        raise PilotUsersError(f"Fixture ownership conflict for '{account.login}'; no changes applied.")
    return user


def run(env: Environment, action: Action, config_path: str | Path) -> None:
    """Seed or archive fixture accounts atomically in a fresh pilot Odoo shell.

    Commit only on success. Leave existing passwords, roles, and project links
    unchanged; never delete records or create projects or tasks.
    """
    if action not in ("seed", "clean"):
        raise PilotUsersError("Use 'seed' or 'clean'.")
    passwords: dict[str, str] = _read_config(Path(config_path), action, env.cr.dbname)
    users: BaseModel = env["res.users"].with_context(active_test=False, no_reset_password=True)
    markers: BaseModel = env["ir.model.data"]
    groups: dict[str, int] = {}
    if action == "seed":
        for xmlid in {account.group_xmlid for account in _ACCOUNTS}:
            group: BaseModel | None = env.ref(xmlid, raise_if_not_found=False)
            if not group:
                raise PilotUsersError("Install the Project app in the pilot database first.")
            groups[xmlid] = group.id

    changed: int = 0
    try:
        with env.cr.savepoint():
            existing: list[tuple[PilotAccount, BaseModel]] = [
                (account, _find_account(users, markers, account)) for account in _ACCOUNTS
            ]
            for account, user in existing:
                if action == "clean":
                    if user and user.active:
                        user.write({"active": False})
                        changed += 1
                elif not user:
                    from odoo.fields import Command

                    created: BaseModel = users.create({
                        "name": account.name,
                        "login": account.login,
                        "email": account.login,
                        "password": passwords[account.key],
                        "group_ids": [Command.set([groups[account.group_xmlid]])],
                    })
                    markers.create({
                        "module": _NAMESPACE,
                        "name": account.key,
                        "model": "res.users",
                        "res_id": created.id,
                        "noupdate": True,
                    })
                    changed += 1
                else:
                    updates: dict[str, str | bool] = {}
                    if user.login != account.login:
                        updates["login"] = account.login
                    if not user.email:
                        updates["email"] = account.login
                    if not user.active:
                        updates["active"] = True
                    if updates:
                        user.write(updates)
                        changed += 1
        env.cr.commit()
    except PilotUsersError:
        raise
    except Exception:
        env.cr.rollback()
        raise PilotUsersError("Odoo could not apply the pilot account changes.") from None

    outcome: str = "created, updated, or reactivated" if action == "seed" else "archived"
    print(f"Pilot accounts: {changed} {outcome}.")
