"""Unit tests for the AG-01 evaluation harness."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from contract import extract_json, validate_answer  # noqa: E402
from eval_core import Fixture, ToolServer, collect_allowed_sources  # noqa: E402
from routerai_client import RouterAIClient, RouterAIError  # noqa: E402
from runner import build_payload, run_case  # noqa: E402
from tool_schemas import build_answer_schema, build_tool_schemas  # noqa: E402


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = Fixture.load(BASE_DIR / "fixtures" / "project.json")
        cls.cases = [
            json.loads(line)
            for line in (BASE_DIR / "cases.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_fixture_shape(self) -> None:
        self.assertEqual(len(self.fixture.data["tasks"]), 15)
        comments = sum(len(task.get("comments", [])) for task in self.fixture.data["tasks"])
        self.assertGreaterEqual(comments, 25)
        self.assertLessEqual(comments, 40)
        self.assertEqual(len(self.cases), 10)

    def test_access(self) -> None:
        self.assertTrue(self.fixture.can_access("user_lead", "T-108"))
        self.assertFalse(self.fixture.can_access("user_analyst", "T-108"))
        self.assertFalse(self.fixture.can_access("user_observer", "T-101"))

    def test_card(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute("read_task_card", {})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_id"], "task:T-101")
        self.assertEqual(result["task"]["deadline"], "2026-11-20")
        self.assertEqual(result["task"]["assignee"], "Анна Петрова")

    def test_description_is_truncated(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute("read_task_description", {})
        self.assertTrue(result["truncated"])
        self.assertGreater(result["total_chars"], result["returned_chars"])
        self.assertNotIn("архив старше 2023", result["text"])

    def test_comments_cap_and_truncation(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute(
            "read_task_comments",
            {"date_from": "2026-11-01", "date_to": "2026-11-25", "max_fragments": 2},
        )
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_in_period"], 5)
        self.assertEqual(result["selection"], "latest")

    def test_long_period_is_clamped_not_rejected(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute(
            "read_task_comments",
            {"date_from": "2026-01-01", "date_to": "2026-11-25", "max_fragments": 20},
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["period_clamped"])
        self.assertEqual(result["period"]["to"], "2026-11-25")
        self.assertEqual(result["total_in_period"], 5)

    def test_extra_argument_is_rejected_with_reason(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute(
            "read_task_worklog",
            {"date_from": "2026-11-01", "date_to": "2026-11-25", "task_id": "T-101"},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("Лишние параметры", result["message"])
        self.assertIn("task_id", result["message"])

    def test_worklog_total(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute(
            "read_task_worklog", {"date_from": "2026-11-01", "date_to": "2026-11-25"}
        )
        self.assertEqual(result["total_hours"], 12.5)
        self.assertEqual(result["entry_count"], 3)

    def test_denied_does_not_disclose(self) -> None:
        server = ToolServer(self.fixture, "T-108", "user_analyst")
        result = server.execute("read_task_card", {})
        self.assertEqual(result["status"], "denied")
        self.assertIsNone(result["source_id"])
        self.assertNotIn("title", result)

    def test_source_accounting(self) -> None:
        server = ToolServer(self.fixture, "T-101", "user_lead")
        result = server.execute("read_task_card", {})
        allowed = collect_allowed_sources([result])
        self.assertIn("task:T-101", allowed)
        self.assertNotIn("task:T-102", allowed)

    def test_tool_schemas(self) -> None:
        schemas = build_tool_schemas()
        self.assertEqual(len(schemas), 5)
        names = {item["function"]["name"] for item in schemas}
        self.assertEqual(
            names,
            {
                "read_task_card",
                "read_task_description",
                "read_task_stage_history",
                "read_task_comments",
                "read_task_worklog",
            },
        )


class ContractTests(unittest.TestCase):
    def test_extract_json_from_fence(self) -> None:
        parsed = extract_json('```json\n{"answer": "ok"}\n```')
        self.assertEqual(parsed, {"answer": "ok"})

    def test_valid_answer_scores_full(self) -> None:
        case = {
            "expected_status": "ok",
            "expected_tools": ["read_task_card"],
            "expected_facts": [
                {"field_any": ["deadline"], "value_any": ["2026-11-20"]},
                {"field_any": ["assignee"], "value_any": ["Анна Петрова"]},
            ],
            "expected_sources": ["task:T-101"],
            "forbidden_mentions": [],
        }
        answer = {
            "answer": "Срок 20.11.2026, исполнитель Анна Петрова.",
            "status": "ok",
            "facts": [
                {"source_id": "task:T-101", "field": "deadline", "value": "2026-11-20"},
                {"source_id": "task:T-101", "field": "assignee", "value": "Анна Петрова"},
            ],
            "sources": ["task:T-101"],
            "missing": [],
        }
        validation = validate_answer(
            answer,
            allowed_sources={"task:T-101"},
            expected=case,
            called_tools=["read_task_card"],
        )
        self.assertTrue(validation.contract_ok)
        self.assertTrue(validation.status_ok)
        self.assertFalse(validation.hard_fail)
        self.assertEqual(validation.score, 100.0)

    def test_invented_source_is_hard_fail(self) -> None:
        case = {
            "expected_status": "ok",
            "expected_tools": ["read_task_card"],
            "expected_facts": [],
            "expected_sources": ["task:T-101"],
            "forbidden_mentions": [],
        }
        answer = {
            "answer": "Срок завтра.",
            "status": "ok",
            "facts": [{"source_id": "task:T-999", "field": "deadline", "value": "2026-11-26"}],
            "sources": ["task:T-999"],
            "missing": [],
        }
        validation = validate_answer(
            answer,
            allowed_sources={"task:T-101"},
            expected=case,
            called_tools=["read_task_card"],
        )
        self.assertTrue(validation.hard_fail)
        self.assertIn("task:T-999", validation.invalid_sources)

    def test_forbidden_disclosure_is_hard_fail(self) -> None:
        case = {
            "expected_status": "access_denied",
            "expected_tools": ["read_task_card"],
            "expected_facts": [],
            "expected_sources": [],
            "forbidden_mentions": ["Интеграция с лабораторией"],
        }
        answer = {
            "answer": "Интеграция с лабораторией идёт по плану.",
            "status": "access_denied",
            "facts": [],
            "sources": [],
            "missing": [],
        }
        validation = validate_answer(
            answer,
            allowed_sources=set(),
            expected=case,
            called_tools=["read_task_card"],
        )
        self.assertTrue(validation.hard_fail)
        self.assertTrue(validation.forbidden_hits)

    def test_fact_field_name_is_a_hint_not_a_filter(self) -> None:
        case = {
            "expected_status": "ok",
            "expected_tools": ["read_task_stage_history"],
            "expected_facts": [
                {"field_any": ["stage", "status", "дата"], "value_any": ["2026-11-24"]}
            ],
            "expected_sources": ["history:T-101"],
            "forbidden_mentions": [],
        }
        answer = {
            "answer": "Задача перешла в работу 24.11.2026.",
            "status": "ok",
            "facts": [
                {"source_id": "history:T-101", "field": "at", "value": "2026-11-24"}
            ],
            "sources": ["history:T-101"],
            "missing": [],
        }
        validation = validate_answer(
            answer,
            allowed_sources={"history:T-101"},
            expected=case,
            called_tools=["read_task_stage_history"],
        )
        self.assertEqual(validation.facts_matched, [True])
        self.assertFalse(validation.hard_fail)
        self.assertGreater(validation.score, 90.0)

    def test_missing_expected_tool_lowers_contract(self) -> None:
        case = {
            "expected_tools": ["read_task_card"],
            "expected_facts": [],
            "expected_sources": [],
            "forbidden_mentions": [],
        }
        answer = {
            "answer": "Срок 20.11.2026.",
            "status": "ok",
            "facts": [],
            "sources": [],
            "missing": [],
        }
        validation = validate_answer(
            answer,
            allowed_sources=set(),
            expected=case,
            called_tools=[],
        )
        self.assertTrue(validation.missing_tools)
        self.assertLess(validation.contract_score, 20.0)


    def test_empty_fact_value_is_penalized_not_zeroed(self) -> None:
        case = {
            "expected_status": "insufficient_data",
            "expected_tools": ["read_task_card"],
            "expected_facts": [{"field_any": ["status"], "value_any": ["Новая"]}],
            "expected_sources": ["task:T-105"],
            "forbidden_mentions": [],
        }
        answer = {
            "answer": "Задача в статусе «Новая», данных о причине нет.",
            "status": "insufficient_data",
            "facts": [
                {"source_id": "task:T-105", "field": "status", "value": "Новая"},
                {"source_id": "task:T-105", "field": "assignee", "value": ""},
            ],
            "sources": ["task:T-105"],
            "missing": ["нет комментариев и трудозатрат"],
        }
        validation = validate_answer(
            answer,
            allowed_sources={"task:T-105"},
            expected=case,
            called_tools=["read_task_card"],
        )
        self.assertFalse(validation.contract_ok)
        self.assertFalse(validation.hard_fail)
        self.assertGreater(validation.score, 0.0)
        self.assertLess(validation.contract_score, 20.0)


class ClientTests(unittest.TestCase):
    def test_error_payload_with_http_200_raises(self) -> None:
        client = RouterAIClient("test-key")
        response = mock.Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "error": {"message": "Use tool_call_id", "code": 400}
        }
        with mock.patch.object(client._session, "post", return_value=response):
            with self.assertRaises(RouterAIError) as raised:
                client.chat({"model": "x", "messages": []})
        self.assertEqual(raised.exception.status_code, 200)
        self.assertIn("tool_call_id", str(raised.exception))

    def test_tool_message_has_no_name_field(self) -> None:
        fixture = Fixture.load(BASE_DIR / "fixtures" / "project.json")
        case = next(
            json.loads(line)
            for line in (BASE_DIR / "cases.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["id"] == "C01"
        )
        answer = {
            "answer": "Срок 20.11.2026, исполнитель Анна Петрова.",
            "status": "ok",
            "facts": [
                {"source_id": "task:T-101", "field": "deadline", "value": "2026-11-20"}
            ],
            "sources": ["task:T-101"],
            "missing": [],
        }
        fake = _FakeClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_task_card",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(answer, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            ]
        )
        model = {
            "id": "sber/gigachat-3-pro",
            "label": "GigaChat 3 Pro",
            "endpoint": "cloud-ru",
            "country": "ru",
            "pricing_per_token": {"prompt": 0.0, "completion": 0.0},
            "supported_parameters": ["tools"],
            "supports_structured": False,
        }
        record = run_case(
            model=model,
            case=case,
            fixture=fixture,
            client=fake,
            tool_schemas=build_tool_schemas(),
            answer_schema=build_answer_schema(),
            max_steps=4,
            max_tokens=500,
            repair=False,
        )
        self.assertIsNone(record["error"])
        self.assertEqual(len(fake.payloads), 2)
        tool_messages = [
            message
            for message in fake.payloads[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertNotIn("name", tool_messages[0])
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")


class _FakeClient:
    """Return scripted responses and remember every payload."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, object]] = []

    def chat(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        return self.responses.pop(0)

    def close(self) -> None:
        return None


class PayloadTests(unittest.TestCase):
    def test_payload_pins_provider_and_omits_tools_on_final(self) -> None:
        model = {
            "id": "sber/gigachat-3-pro",
            "endpoint": "cloud-ru",
            "supported_parameters": ["max_tokens", "temperature", "tools"],
            "supports_structured": True,
        }
        payload = build_payload(
            model,
            [{"role": "user", "content": "hi"}],
            tools=None,
            answer_schema=build_answer_schema(),
            max_tokens=100,
            response_format=True,
        )
        self.assertEqual(payload["provider"], {"only": ["cloud-ru"], "allow_fallbacks": False})
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertIn("response_format", payload)

    def test_payload_uses_temperature_only_if_supported(self) -> None:
        model = {
            "id": "openai/gpt-6-luna",
            "endpoint": "openai",
            "supported_parameters": ["max_tokens", "tools"],
            "supports_structured": True,
        }
        payload = build_payload(
            model,
            [{"role": "user", "content": "hi"}],
            tools=build_tool_schemas(),
            answer_schema=build_answer_schema(),
            max_tokens=100,
            response_format=False,
        )
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["tool_choice"], "auto")


class CompatTests(unittest.TestCase):
    def test_max_completion_tokens_field_is_used(self) -> None:
        model = {
            "id": "openai/gpt-5-nano",
            "endpoint": "openai",
            "supported_parameters": ["max_tokens", "tools"],
            "supports_structured": True,
            "max_tokens_field": "max_completion_tokens",
        }
        payload = build_payload(
            model,
            [{"role": "user", "content": "hi"}],
            tools=build_tool_schemas(),
            answer_schema=build_answer_schema(),
            max_tokens=777,
            response_format=False,
        )
        self.assertEqual(payload["max_completion_tokens"], 777)
        self.assertNotIn("max_tokens", payload)

    def test_harmony_channel_marker_is_stripped_from_tool_name(self) -> None:
        fixture = Fixture.load(BASE_DIR / "fixtures" / "project.json")
        case = next(
            json.loads(line)
            for line in (BASE_DIR / "cases.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["id"] == "C01"
        )
        answer = {
            "answer": "Срок 20.11.2026, исполнитель Анна Петрова.",
            "status": "ok",
            "facts": [
                {"source_id": "task:T-101", "field": "deadline", "value": "2026-11-20"}
            ],
            "sources": ["task:T-101"],
            "missing": [],
        }
        fake = _FakeClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_task_card<|channel|>commentary",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(answer, ensure_ascii=False),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            ]
        )
        model = {
            "id": "openai/gpt-oss-20b",
            "label": "GPT-OSS 20B",
            "endpoint": "coreweave",
            "country": "us",
            "pricing_per_token": {"prompt": 0.0, "completion": 0.0},
            "supported_parameters": ["tools"],
            "supports_structured": False,
        }
        record = run_case(
            model=model,
            case=case,
            fixture=fixture,
            client=fake,
            tool_schemas=build_tool_schemas(),
            answer_schema=build_answer_schema(),
            max_steps=4,
            max_tokens=500,
            repair=False,
        )
        self.assertEqual(record["called_tools"], ["read_task_card"])
        self.assertEqual(record["tool_results"][0]["status"], "ok")
        self.assertEqual(record["tool_results"][0]["source_id"], "task:T-101")


if __name__ == "__main__":
    unittest.main()
