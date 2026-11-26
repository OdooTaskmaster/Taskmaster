"""Seed a standalone student support-agent Kanban board from an Odoo shell."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html import escape
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


PROJECT_NAME: str = "Учебный проект: ИИ-агент техподдержки"
DEFAULT_LOGINS: tuple[str, str, str] = (
    "aic-pilot-a1@example.test", "aic-pilot-a2@example.test", "aic-pilot-a3@example.test",
)
_MODULE: str = "aic_prj03_demo"
_STAGES: tuple[tuple[str, str, bool], ...] = (
    ("backlog", "Бэклог", False),
    ("planned", "Запланировано", False),
    ("doing", "В работе", False),
    ("review", "На проверке", False),
    ("done", "Готово", True),
)


class Card(NamedTuple):
    """Describe a task, its working state and calendar offsets from the seed date."""

    key: str
    title: str
    description: str
    stage: str
    owners: tuple[int, ...]
    age_days: int
    due_days: int | None
    state: str = "01_in_progress"


CARDS: tuple[Card, ...] = (
    Card("scope", "Согласовать границы первой версии",
         "Ограничить демо вопросами о доступе к курсам, расписании и заявках. Зафиксировать, когда бот направляет к оператору.",
         "done", (0, 1, 2), 20, -14, "1_done"),
    Card("intents", "Собрать каталог обращений студентов",
         "Написать 30 вымышленных обращений: проблемы со входом, поиск курса, смена группы и статус заявки. Разметить категории.",
         "done", (0,), 18, -10, "1_done"),
    Card("kb_sources", "Согласовать источники базы знаний",
         "Задокументировать источники правил и шаблон обновления статьи. В демо использовать только синтетические материалы.",
         "done", (1,), 15, -8, "1_done"),
    Card("ticket_schema", "Описать формат заявки оператору",
         "Утвердить поля: категория, краткое резюме и ссылка на чат. Не переносить в заявку пароль или полный диалог.",
         "done", (2,), 13, -6, "1_done"),
    Card("retired", "Оценить отдельный канал в мессенджере",
         "Закрыть идею для первого спринта: показ пройдёт в тестовой веб-форме. Вернуться к вопросу после пилота.",
         "done", (2,), 12, -5, "1_canceled"),
    Card("retrieval", "Исправить поиск по базе знаний",
         "Разобрать, почему вопросы о восстановлении доступа возвращают статьи про расписание. Добавить тесты на перефразирование.",
         "doing", (1,), 0, -2),
    Card("dedupe", "Убрать дубли при передаче оператору",
         "Воспроизвести повторную отправку формы. Одна цепочка сообщений должна создавать не больше одной тестовой заявки.",
         "review", (2,), 9, -5),
    Card("fallback", "Настроить ответ при отсутствии инструкции",
         "Если подходящей статьи нет, бот прямо сообщает об этом и предлагает уточнение или обращение к оператору.",
         "doing", (0,), 4, 2),
    Card("tone", "Проверить тон ответов по спорным обращениям",
         "На шести ситуациях проверить ясность ответа и отсутствие неподтверждённых обещаний. Согласовать правки с автором сценариев.",
         "review", (0, 2), 2, 3, "02_changes_requested"),
    Card("kb_owners", "Назначить ответственного за ревизию статей",
         "Определить, кто ежемесячно проверяет актуальность заметок, и описать процедуру замены устаревших ссылок.",
         "backlog", (), 0, None),
    Card("redaction", "Маскировать контактные данные в примерах",
         "Подготовить тесты на вымышленных телефонах, почте и номерах заявок. Проверить, что исходные значения не попадают в отчёт.",
         "doing", (1,), 7, 18),
    Card("metrics", "Собрать метрики качества ответов",
         "Для размеченных запросов посчитать долю верных ответов, отказов и переводов к оператору; сохранить таблицу ошибок.",
         "planned", (0, 1), 0, 10),
    Card("web_ui", "Сверстать форму вопроса",
         "Добавить поле ввода, историю ответа и кнопку передачи оператору. Проверить пустое сообщение и мобильную ширину.",
         "doing", (2,), 0, 1),
    Card("citations", "Показывать ссылку на статью в ответе",
         "Каждый ответ из базы знаний должен содержать заголовок и доступную ссылку на использованную статью.",
         "review", (1,), 1, 6, "03_approved"),
    Card("ambiguity", "Уточнять неоднозначные вопросы",
         "Для запроса «не работает» спросить, что именно: вход, открытие курса или отправка задания. Покрыть три ветки тестами.",
         "planned", (0,), 1, 12),
    Card("load", "Измерить время ответа под нагрузкой",
         "Отправить 40 синтетических запросов через тестовую форму. Замерить медиану и 95-й перцентиль, записать параметры прогона.",
         "planned", (2, 1), 3, 21),
    Card("accessibility", "Проверить доступность интерфейса",
         "Пройти форму с клавиатуры, проверить подписи полей, порядок фокуса и читаемость текстов в мобильном браузере.",
         "planned", (2,), 0, 28),
    Card("offline", "Описать поведение при отключении модели",
         "В тестовом окружении имитировать тайм-аут. Пользователь должен получить понятное сообщение, оператору не уходит дубль заявки.",
         "doing", (1, 2), 6, 8),
    Card("faq", "Дополнить статьи про доступ к курсам",
         "Разделить потерю пароля, отсутствие курса в списке и неработающее приглашение. Добавить по два примера на каждую статью.",
         "review", (0,), 5, 7),
    Card("demo", "Подготовить сценарий защиты",
         "Собрать показ на семь минут: ответ со ссылкой, уточнение запроса и передача оператору. Подготовить офлайн-скриншоты.",
         "planned", (0, 2), 0, 35),
    Card("handbook", "Составить памятку оператору",
         "Объяснить категории заявок, правила перенаправления и восстановление контекста из резюме бота. Срок уточнит команда.",
         "backlog", (0,), 0, None),
    Card("languages", "Оценить двуязычный интерфейс",
         "Сравнить качество ответов на русском и английском на вымышленных обращениях. Решить, входит ли перевод в следующий спринт.",
         "backlog", (), 0, 25),
    Card("dry_run", "Провести контрольный прогон перед защитой",
         "По очереди сыграть роли студента, бота и оператора. Зафиксировать ошибки сценария и выбрать, что исправить до защиты.",
         "planned", (0, 1, 2), 2, 39),
    Card("maintenance", "Спланировать сопровождение после защиты",
         "Описать, кто обновит статьи, проверит жалобы и отключит тестовые интеграции после окончания учебного проекта.",
         "backlog", (1,), 0, 45),
)


class PilotDemoError(RuntimeError):
    """Reject a setup that could seed or change the wrong records."""


def _utc_naive(value: datetime) -> datetime:
    """Convert an aware datetime to the UTC-naive format used by the ORM."""
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def _deadline(today: date, tz: tzinfo, days: int | None) -> datetime | bool:
    """Return a 17:00 local deadline on the offset date, or no deadline."""
    if days is None:
        return False
    return _utc_naive(datetime.combine(today + timedelta(days=days), time(17), tzinfo=tz))


def _find_record(env: Environment, key: str, model: str) -> BaseModel:
    """Resolve only the records marked by this script, rejecting invalid markers."""
    marker: BaseModel = env["ir.model.data"].search([("module", "=", _MODULE), ("name", "=", key)])
    if not marker:
        return env[model]
    if marker.model != model:
        raise PilotDemoError(f"Invalid fixture marker: {key}.")
    record: BaseModel = env[model].browse(marker.res_id).exists()
    if not record:
        raise PilotDemoError(f"Missing record for fixture marker: {key}.")
    return record


def _mark(env: Environment, key: str, record: BaseModel) -> None:
    """Register a stable external ID in the demo namespace."""
    env["ir.model.data"].create({
        "module": _MODULE, "name": key, "model": record._name, "res_id": record.id, "noupdate": True,
    })


def _participants(env: Environment, logins: tuple[str, str, str]) -> tuple[int, ...]:
    """Resolve three distinct active Project users in the current company, in login order."""
    if len(logins) != 3 or len(set(logins)) != 3 or not all(logins):
        raise PilotDemoError("Provide three distinct participant logins.")
    users: BaseModel = env["res.users"].search([
        ("login", "in", logins), ("active", "=", True), ("share", "=", False),
        ("company_ids", "in", [env.company.id]),
    ])
    by_login: dict[str, int] = {
        user.login: user.id for user in users if user.has_group("project.group_project_user")
    }
    if len(by_login) != 3:
        raise PilotDemoError("All three participants must be active internal Project users in the current company.")
    return tuple(by_login[login] for login in logins)


def run(
    env: Environment,
    database: str,
    *,
    logins: tuple[str, str, str] = DEFAULT_LOGINS,
    timezone: str = "Europe/Moscow",
    refresh: bool = False,
) -> tuple[int, int]:
    """Create the board or refresh deadlines atomically without committing the shell transaction."""
    if not database or database != env.cr.dbname:
        raise PilotDemoError("The database name does not match this Odoo shell.")
    if not env.ref("project.group_project_user", raise_if_not_found=False):
        raise PilotDemoError("Install the Project app in the test database first.")
    try:
        tz: tzinfo = UTC if timezone == "UTC" else ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise PilotDemoError("Unknown IANA time zone or missing time zone data. Use 'UTC' if needed.") from None

    from odoo.fields import Command

    now: datetime = env.cr.now().replace(tzinfo=UTC).astimezone(tz)
    today: date = now.date()
    flags: dict[str, bool] = {"tracking_disable": True, "mail_auto_subscribe_no_notify": True}
    projects: BaseModel = env["project.project"].with_context(**flags)
    tasks: BaseModel = env["project.task"].with_context(**flags)

    with env.cr.savepoint():
        project: BaseModel = _find_record(env, "project", "project.project")
        if project:
            existing: list[BaseModel] = [_find_record(env, card.key, "project.task") for card in CARDS]
            if not project.active or any(not task or task.project_id != project for task in existing):
                raise PilotDemoError("The demo project is archived or a fixture card is missing or has moved.")
            if refresh:
                project.with_context(**flags).write({"date": today + timedelta(days=45)})
                for card, task in zip(CARDS, existing, strict=True):
                    task.with_context(**flags).write({"date_deadline": _deadline(today, tz, card.due_days)})
            return project.id, len(existing)
        if refresh:
            raise PilotDemoError("Create the demo project before refreshing its deadlines.")
        if projects.with_context(active_test=False).search_count([("name", "=", PROJECT_NAME)]):
            raise PilotDemoError("A project with the demo name exists without the expected fixture marker.")

        participant_ids: tuple[int, ...] = _participants(env, logins)
        project_vals: dict[str, object] = {
            "name": PROJECT_NAME,
            "description": "<p>Три студента разрабатывают и проверяют ИИ-агента для первой линии поддержки учебной платформы.</p>",
            "company_id": env.company.id,
            "user_id": participant_ids[0],
            "privacy_visibility": "followers",
            "allowed_internal_user_ids": [Command.set(list(participant_ids))],
            "date_start": today - timedelta(days=21),
            "date": today + timedelta(days=45),
            "type_ids": [Command.clear()],
        }
        if "allow_timesheets" in projects._fields:
            project_vals["allow_timesheets"] = False
        project = projects.create(project_vals)
        _mark(env, "project", project)

        stages: BaseModel = env["project.task.type"].create([
            {"name": title, "sequence": index * 10, "fold": fold, "project_ids": [Command.link(project.id)]}
            for index, (_, title, fold) in enumerate(_STAGES, start=1)
        ])
        stage_ids: dict[str, int] = {stage[0]: record.id for stage, record in zip(_STAGES, stages, strict=True)}
        created: BaseModel = tasks.create([
            {
                "project_id": project.id,
                "name": card.title,
                "description": f"<p>{escape(card.description)}</p>",
                "stage_id": stage_ids[card.stage],
                "user_ids": [Command.set([participant_ids[index] for index in card.owners])],
                "state": card.state,
                "date_deadline": _deadline(today, tz, card.due_days),
            }
            for card in CARDS
        ])

        # The ORM ignores create_date on create; age only freshly created synthetic cards.
        created.flush_recordset(["create_date", "project_id"])
        for card, task in zip(CARDS, created, strict=True):
            _mark(env, card.key, task)
            if card.age_days:
                env.cr.execute(
                    "UPDATE project_task SET create_date = %s WHERE id = %s AND project_id = %s",
                    (_utc_naive(now - timedelta(days=card.age_days)), task.id, project.id),
                )
        created.invalidate_recordset(["create_date"])
        return project.id, len(created)
