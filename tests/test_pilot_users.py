"""Verify the pilot account script without an Odoo database."""

from contextlib import nullcontext, redirect_stdout
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from scripts import pilot_users


class PilotUsersTest(TestCase):
    """Exercise provisioning, archiving, and the test database guard."""

    def setUp(self) -> None:
        """Set up the smallest shell and ORM doubles needed by the script."""
        self.env: MagicMock = MagicMock()
        self.users: MagicMock = MagicMock()
        self.markers: MagicMock = MagicMock()
        self.env.cr.dbname = "aic_test"
        self.env.cr.savepoint.side_effect = nullcontext
        self.env.__getitem__.side_effect = {"res.users": self.users, "ir.model.data": self.markers}.__getitem__
        self.users.with_context.return_value = self.users
        self.users.search.return_value = []
        self.markers.search.return_value = []

    def test_seed_creates_exactly_seven_accounts_with_configured_roles(self) -> None:
        """Verify all fixture email logins, passwords, and ownership markers."""
        lines: list[str] = [f'{account.key} = "fixture-{account.key}"' for account in pilot_users._ACCOUNTS]
        config: bytes = ('database = "aic_test"\n[passwords]\n' + "\n".join(lines)).encode()
        self.env.ref.side_effect = lambda xmlid, **_: SimpleNamespace(
            id=2 if xmlid == "project.group_project_manager" else 1,
        )
        self.users.create.return_value = SimpleNamespace(id=33)
        command: SimpleNamespace = SimpleNamespace(set=lambda ids: (6, 0, ids))
        odoo_fields: SimpleNamespace = SimpleNamespace(Command=command)
        output: StringIO = StringIO()
        with (
            patch("pathlib.Path.open", return_value=BytesIO(config)),
            patch.dict("sys.modules", {"odoo.fields": odoo_fields}),
            redirect_stdout(output),
        ):
            pilot_users.run(self.env, "seed", "test.toml")

        accounts: list[dict[str, object]] = [call.args[0] for call in self.users.create.call_args_list]
        logins: list[str] = [f"aic-pilot-{key}@example.test" for key in ("lead", "a1", "a2", "a3", "b1", "b2", "b3")]
        self.assertEqual(
            [account["login"] for account in accounts],
            logins,
        )
        self.assertEqual([account["email"] for account in accounts], logins)
        self.assertEqual([account["group_ids"] for account in accounts], [[(6, 0, [2])]] + [[(6, 0, [1])]] * 6)
        self.assertEqual(accounts[1]["password"], "fixture-a1")
        self.assertEqual(self.markers.create.call_count, 7)
        self.assertNotIn("fixture-", output.getvalue())
        self.env.cr.commit.assert_called_once_with()

    def test_seed_updates_old_email_logins_without_creating_accounts(self) -> None:
        """Upgrade owned accounts, preserve existing email, and avoid repeat writes."""
        accounts: list[MagicMock] = []
        markers: list[MagicMock] = []
        for index, account in enumerate(pilot_users._ACCOUNTS, start=1):
            user: MagicMock = MagicMock(id=index, login=account.login, email=account.login, active=True)
            marker: MagicMock = MagicMock(model="res.users", res_id=index)
            user.__len__.return_value = marker.__len__.return_value = 1
            accounts.append(user)
            markers.append(marker)
        accounts[0].login = "aic-pilot-lead"
        accounts[0].email = False
        accounts[1].email = "custom@example.test"
        self.users.search.side_effect = accounts
        self.markers.search.side_effect = markers
        passwords: dict[str, str] = {account.key: "fixture" for account in pilot_users._ACCOUNTS}

        with patch.object(pilot_users, "_read_config", return_value=passwords), redirect_stdout(StringIO()):
            pilot_users.run(self.env, "seed", "test.toml")

        self.assertIn(("login", "in", ["aic-pilot-lead@example.test", "aic-pilot-lead"]),
                      self.users.search.call_args_list[0].args[0])
        accounts[0].write.assert_called_once_with({
            "login": "aic-pilot-lead@example.test",
            "email": "aic-pilot-lead@example.test",
        })
        for user in accounts[1:]:
            user.write.assert_not_called()
        self.users.create.assert_not_called()
        self.env.cr.commit.assert_called_once_with()

    def test_mismatched_database_prevents_any_orm_access(self) -> None:
        """Reject a shell pointed at a different database."""
        with patch("pathlib.Path.open", return_value=BytesIO(b'database = "other"')):
            with self.assertRaisesRegex(pilot_users.PilotUsersError, "does not match"):
                pilot_users.run(self.env, "clean", "test.toml")
        self.env.__getitem__.assert_not_called()
        self.env.cr.commit.assert_not_called()

    def test_clean_archives_only_an_owned_account_without_passwords(self) -> None:
        """Archive using the matching ownership record, without deletion."""
        user: MagicMock = MagicMock(id=21, active=True)
        marker: MagicMock = MagicMock(model="res.users", res_id=21)
        user.__len__.return_value = 1
        marker.__len__.return_value = 1
        self.users.search.side_effect = [user] + [[]] * 6
        self.markers.search.side_effect = [marker] + [[]] * 6

        with patch("pathlib.Path.open", return_value=BytesIO(b'database = "aic_test"')), redirect_stdout(StringIO()):
            pilot_users.run(self.env, "clean", "test.toml")
        user.write.assert_called_once_with({"active": False})
        user.unlink.assert_not_called()
        self.users.create.assert_not_called()
        self.env.cr.commit.assert_called_once_with()

    def test_clean_refuses_a_login_without_an_ownership_marker(self) -> None:
        """Reject a collision instead of archiving an unrelated account."""
        user: MagicMock = MagicMock(id=21, active=True)
        user.__len__.return_value = 1
        self.users.search.return_value = user
        with patch("pathlib.Path.open", return_value=BytesIO(b'database = "aic_test"')):
            with self.assertRaisesRegex(pilot_users.PilotUsersError, "ownership conflict"):
                pilot_users.run(self.env, "clean", "test.toml")
        user.write.assert_not_called()
        self.env.cr.commit.assert_not_called()
