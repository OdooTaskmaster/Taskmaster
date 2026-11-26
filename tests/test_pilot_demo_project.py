"""Test the demo board fixture and shell safeguards without an Odoo database."""

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from scripts import pilot_demo_project as demo


class PilotDemoProjectTest(TestCase):
    """Verify the fixture and the boundary between the seed and the ORM."""

    def test_fixture_covers_the_board_and_reference_cases(self) -> None:
        """Keep the 24 scenarios diverse and use only the three participant slots."""
        cards: dict[str, demo.Card] = {card.key: card for card in demo.CARDS}
        self.assertEqual(len(demo.CARDS), 24)
        self.assertEqual(len(cards), len(demo.CARDS))
        self.assertEqual(len({card.title for card in cards.values()}), len(cards))
        self.assertTrue(all(card.description.strip() for card in cards.values()))
        self.assertEqual({card.stage for card in cards.values()}, {stage[0] for stage in demo._STAGES})
        self.assertEqual({owner for card in cards.values() for owner in card.owners}, {0, 1, 2})
        self.assertEqual((cards["retrieval"].age_days, cards["retrieval"].due_days), (0, -2))
        self.assertEqual((cards["tone"].age_days, cards["tone"].due_days), (2, 3))
        self.assertEqual(cards["kb_owners"].owners, ())
        self.assertIsNone(cards["kb_owners"].due_days)
        self.assertEqual(cards["redaction"].age_days, 7)
        self.assertEqual(cards["offline"].age_days, 6)
        self.assertEqual(cards["scope"].state, "1_done")
        self.assertEqual(cards["retired"].state, "1_canceled")
        self.assertEqual(cards["maintenance"].due_days, 45)

    def test_deadline_follows_local_calendar_and_stays_relative(self) -> None:
        """Convert the same local offsets to UTC on different anchor days."""
        project_tz: timezone = timezone(timedelta(hours=3))
        today: date = datetime(2026, 11, 30, 22, tzinfo=UTC).astimezone(project_tz).date()
        self.assertEqual(demo._deadline(today, project_tz, -2), datetime(2026, 11, 29, 14))
        self.assertEqual(demo._deadline(today, project_tz, 45), datetime(2027, 1, 15, 14))
        self.assertEqual(demo._deadline(today + timedelta(days=7), project_tz, 45), datetime(2027, 1, 22, 14))
        self.assertIs(demo._deadline(today, project_tz, None), False)

    def test_database_guard_runs_before_orm_access(self) -> None:
        """Reject a different database without querying models or committing."""
        env: MagicMock = MagicMock()
        env.cr.dbname = "aic_pilot"
        with self.assertRaisesRegex(demo.PilotDemoError, "does not match"):
            demo.run(env, "another_db")
        env.ref.assert_not_called()
        env.__getitem__.assert_not_called()
        env.cr.commit.assert_not_called()

    def test_participants_keep_login_order_and_require_project_access(self) -> None:
        """Use the configured order even if the ORM returns users unordered."""
        env: MagicMock = MagicMock()
        env.company.id = 8
        users: MagicMock = env["res.users"]
        accounts: list[MagicMock] = [MagicMock(login=login, id=index) for index, login in enumerate(demo.DEFAULT_LOGINS, 1)]
        for account in accounts:
            account.has_group.return_value = True
        users.search.return_value = accounts[::-1]

        self.assertEqual(demo._participants(env, demo.DEFAULT_LOGINS), (1, 2, 3))
        self.assertIn(("company_ids", "in", [8]), users.search.call_args.args[0])
        accounts[2].has_group.return_value = False
        with self.assertRaisesRegex(demo.PilotDemoError, "active internal Project users"):
            demo._participants(env, demo.DEFAULT_LOGINS)

    def test_existing_board_is_unchanged_unless_refresh_is_requested(self) -> None:
        """Update the dates only, on the marked tasks of the marked project."""
        env: MagicMock = MagicMock()
        env.cr.dbname = "aic_pilot"
        env.cr.now.return_value = datetime(2026, 11, 26, 12)
        env.cr.savepoint.side_effect = nullcontext
        project: MagicMock = MagicMock(id=71, active=True)
        tasks: dict[str, MagicMock] = {card.key: MagicMock(project_id=project) for card in demo.CARDS}
        records: dict[str, MagicMock] = {"project": project, **tasks}
        command: SimpleNamespace = SimpleNamespace()

        with (
            patch.dict("sys.modules", {"odoo.fields": SimpleNamespace(Command=command)}),
            patch.object(demo, "_find_record", side_effect=lambda env, key, model: records[key]),
        ):
            self.assertEqual(demo.run(env, "aic_pilot", timezone="UTC"), (71, 24))
            project.with_context.assert_not_called()
            for task in tasks.values():
                task.with_context.assert_not_called()

            self.assertEqual(demo.run(env, "aic_pilot", timezone="UTC", refresh=True), (71, 24))

            tasks["retrieval"].project_id = MagicMock()
            with self.assertRaisesRegex(demo.PilotDemoError, "has moved"):
                demo.run(env, "aic_pilot", timezone="UTC", refresh=True)

        project.with_context.return_value.write.assert_called_once_with({"date": date(2027, 1, 10)})
        for card in demo.CARDS:
            tasks[card.key].with_context.return_value.write.assert_called_once_with({
                "date_deadline": demo._deadline(date(2026, 11, 26), UTC, card.due_days),
            })
        env.cr.execute.assert_not_called()
        env.cr.commit.assert_not_called()

    def test_seed_builds_project_stages_assignments_and_relative_dates(self) -> None:
        """Age only new synthetic cards and leave the commit to the operator."""
        env: MagicMock = MagicMock()
        env.cr.dbname = "aic_pilot"
        env.cr.now.return_value = datetime(2026, 11, 26, 12)
        env.cr.savepoint.side_effect = nullcontext
        models: dict[str, MagicMock] = {
            model: MagicMock() for model in ("project.project", "project.task.type", "project.task", "ir.model.data")
        }
        env.__getitem__.side_effect = models.__getitem__
        projects: MagicMock = env["project.project"].with_context.return_value
        projects.with_context.return_value.search_count.return_value = 0
        projects._fields = {"allow_timesheets": object()}
        projects.create.return_value = SimpleNamespace(id=71, _name="project.project")
        stages: MagicMock = env["project.task.type"]
        stages.create.return_value = [SimpleNamespace(id=index) for index in range(1, 6)]
        tasks: MagicMock = env["project.task"].with_context.return_value
        created: MagicMock = tasks.create.return_value
        created.__len__.return_value = len(demo.CARDS)
        created.__iter__.return_value = [
            SimpleNamespace(id=index, _name="project.task") for index in range(100, 124)
        ]
        command: SimpleNamespace = SimpleNamespace(
            set=lambda ids: (6, 0, ids), clear=lambda: (5, 0, 0), link=lambda record_id: (4, record_id, 0),
        )
        with (
            patch.dict("sys.modules", {"odoo.fields": SimpleNamespace(Command=command)}),
            patch.object(demo, "_find_record", return_value=[]),
            patch.object(demo, "_participants", return_value=(21, 22, 23)),
        ):
            self.assertEqual(demo.run(env, "aic_pilot", timezone="UTC"), (71, 24))

        project_vals: dict[str, object] = projects.create.call_args.args[0]
        self.assertEqual(project_vals["privacy_visibility"], "followers")
        self.assertEqual(project_vals["allowed_internal_user_ids"], [(6, 0, [21, 22, 23])])
        self.assertIs(project_vals["allow_timesheets"], False)
        self.assertEqual(len(stages.create.call_args.args[0]), 5)
        card_values: dict[str, dict[str, object]] = {vals["name"]: vals for vals in tasks.create.call_args.args[0]}
        for card in demo.CARDS:
            vals: dict[str, object] = card_values[card.title]
            self.assertEqual(vals["project_id"], 71)
            self.assertEqual(vals["state"], card.state)
            self.assertEqual(vals["user_ids"], [(6, 0, [21 + owner for owner in card.owners])])
            self.assertEqual(vals["date_deadline"], demo._deadline(date(2026, 11, 26), UTC, card.due_days))
        self.assertEqual(env["ir.model.data"].create.call_count, 25)
        self.assertEqual(
            [call.args[1] for call in env.cr.execute.call_args_list],
            [(datetime(2026, 11, 26, 12) - timedelta(days=card.age_days), index, 71)
             for index, card in enumerate(demo.CARDS, start=100) if card.age_days],
        )
        env.cr.commit.assert_not_called()
