"""Answer validation, automatic scoring, and cost estimates for AG-01."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence


STATUSES: set[str] = {"ok", "insufficient_data", "access_denied", "source_error"}
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_RU = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass
class Validation:
    """Automatic checks and the weighted score for one model answer."""

    contract_ok: bool
    status_ok: bool
    tool_use_ok: bool
    language_ok: bool
    score: float
    contract_score: float
    facts_score: float
    sources_score: float
    status_score: float
    language_score: float
    facts_matched: list[bool] = field(default_factory=list)
    sources_matched: list[bool] = field(default_factory=list)
    invalid_sources: list[str] = field(default_factory=list)
    invented_facts: list[dict[str, Any]] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hard_fail: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert the result to JSON-friendly data."""

        return {
            "contract_ok": self.contract_ok,
            "status_ok": self.status_ok,
            "tool_use_ok": self.tool_use_ok,
            "language_ok": self.language_ok,
            "score": round(self.score, 2),
            "contract_score": round(self.contract_score, 2),
            "facts_score": round(self.facts_score, 2),
            "sources_score": round(self.sources_score, 2),
            "status_score": round(self.status_score, 2),
            "language_score": round(self.language_score, 2),
            "facts_matched": self.facts_matched,
            "sources_matched": self.sources_matched,
            "invalid_sources": self.invalid_sources,
            "invented_facts": self.invented_facts,
            "forbidden_hits": self.forbidden_hits,
            "missing_tools": self.missing_tools,
            "notes": self.notes,
            "hard_fail": self.hard_fail,
        }


def normalize_text(value: str) -> str:
    """Normalize Russian text for robust comparisons."""

    text: str = value.lower().replace("ё", "е").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_json(content: Any) -> dict[str, Any] | None:
    """Extract one JSON object from model content."""

    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)
    if not isinstance(content, str):
        return None
    text: str = content.strip()
    if text.startswith("```"):
        lines: list[str] = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start: int = text.find("{")
    end: int = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _dates(value: str) -> set[date]:
    found: set[date] = set()
    for match in DATE_ISO.finditer(value):
        try:
            found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            pass
    for match in DATE_RU.finditer(value):
        try:
            found.add(date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
        except ValueError:
            pass
    return found


def _numbers(value: str) -> set[float]:
    found: set[float] = set()
    for match in NUMBER.finditer(value):
        try:
            found.add(float(match.group(0).replace(",", ".")))
        except ValueError:
            pass
    return found


def value_matches(actual: str, expected: str) -> bool:
    """Match text, dates, or numbers without requiring exact wording."""

    actual_norm: str = normalize_text(actual)
    expected_norm: str = normalize_text(expected)
    if not expected_norm:
        return False
    if expected_norm in actual_norm:
        return True
    expected_dates: set[date] = _dates(expected_norm)
    if expected_dates and expected_dates & _dates(actual_norm):
        return True
    expected_numbers: set[float] = _numbers(expected_norm)
    if expected_numbers and expected_numbers & _numbers(actual_norm):
        return True
    return False


def _structure_error(answer: Mapping[str, Any]) -> str | None:
    """Check only the top-level shape that makes the answer impossible to score."""

    required: set[str] = {"answer", "status", "facts", "sources", "missing"}
    missing: set[str] = required - set(answer)
    if missing:
        return f"нет полей: {', '.join(sorted(missing))}"
    if not isinstance(answer["answer"], str) or not answer["answer"].strip():
        return "answer должен быть непустой строкой"
    if answer["status"] not in STATUSES:
        return "недопустимый status"
    if not isinstance(answer["facts"], list):
        return "facts должен быть массивом"
    if not isinstance(answer["sources"], list) or not all(
        isinstance(item, str) for item in answer["sources"]
    ):
        return "sources должен быть массивом строк"
    if not isinstance(answer["missing"], list) or not all(
        isinstance(item, str) for item in answer["missing"]
    ):
        return "missing должен быть массивом строк"
    return None


def _split_facts(raw_facts: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Drop malformed facts and count them as contract violations."""

    valid: list[dict[str, Any]] = []
    invalid: int = 0
    for fact in raw_facts:
        if not isinstance(fact, dict) or set(fact) != {"source_id", "field", "value"}:
            invalid += 1
            continue
        if not all(
            isinstance(fact[key], str) and fact[key].strip()
            for key in ("source_id", "field", "value")
        ):
            invalid += 1
            continue
        valid.append(dict(fact))
    return valid, invalid


def _fact_matches(facts: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]) -> bool:
    """Match an expected fact by value; field names are model-chosen hints."""

    values: list[str] = [str(item) for item in expected.get("value_any", [])]
    fields: list[str] = [str(item) for item in expected.get("field_any", [])]
    strict_field: bool = bool(expected.get("strict_field"))
    for fact in facts:
        if strict_field and fields and not any(
            value_matches(str(fact.get("field", "")), field) for field in fields
        ):
            continue
        if any(value_matches(str(fact.get("value", "")), value) for value in values):
            return True
    return False


def _source_present(answer: Mapping[str, Any], source: str) -> bool:
    if source in answer.get("sources", []):
        return True
    return any(str(fact.get("source_id")) == source for fact in answer.get("facts", []))


def _language_ok(answer: Mapping[str, Any]) -> bool:
    text: str = str(answer.get("answer", ""))
    if len(text.strip()) < 8:
        return False
    letters: list[str] = [char for char in text if char.isalpha()]
    if not letters:
        return False
    cyrillic: int = sum(1 for char in letters if "\u0400" <= char <= "\u04ff")
    return cyrillic / len(letters) >= 0.4


def validate_answer(
    parsed: Mapping[str, Any] | None,
    *,
    allowed_sources: set[str],
    expected: Mapping[str, Any],
    called_tools: Sequence[str],
    tool_error_count: int = 0,
) -> Validation:
    """Validate the answer contract and compute the weighted AG-01 score."""

    if parsed is None:
        return Validation(
            contract_ok=False,
            status_ok=False,
            tool_use_ok=False,
            language_ok=False,
            score=0.0,
            contract_score=0.0,
            facts_score=0.0,
            sources_score=0.0,
            status_score=0.0,
            language_score=0.0,
            notes=["ответ не является JSON-объектом"],
            hard_fail=True,
        )

    structure_error: str | None = _structure_error(parsed)
    if structure_error:
        return Validation(
            contract_ok=False,
            status_ok=False,
            tool_use_ok=False,
            language_ok=False,
            score=0.0,
            contract_score=0.0,
            facts_score=0.0,
            sources_score=0.0,
            status_score=0.0,
            language_score=0.0,
            notes=[structure_error],
            hard_fail=True,
        )

    facts, invalid_fact_count = _split_facts(list(parsed["facts"]))
    sources: list[str] = list(parsed["sources"])
    invalid_sources: list[str] = [source for source in sources if source not in allowed_sources]
    invented_facts: list[dict[str, Any]] = [
        dict(fact) for fact in facts if str(fact["source_id"]) not in allowed_sources
    ]

    expected_tools: list[str] = [str(item) for item in expected.get("expected_tools", [])]
    missing_tools: list[str] = [name for name in expected_tools if name not in called_tools]

    expected_facts: list[Mapping[str, Any]] = list(expected.get("expected_facts", []))
    facts_matched: list[bool] = [_fact_matches(facts, item) for item in expected_facts]

    expected_sources: list[str] = [str(item) for item in expected.get("expected_sources", [])]
    source_groups: list[list[str]] = [
        [str(item) for item in group] for group in expected.get("expected_source_any", [])
    ]
    sources_matched: list[bool] = [
        _source_present(parsed, source) for source in expected_sources
    ] + [any(_source_present(parsed, source) for source in group) for group in source_groups]

    forbidden: list[str] = [str(item) for item in expected.get("forbidden_mentions", [])]
    haystack: str = normalize_text(
        " ".join(
            [str(parsed["answer"])]
            + [str(fact["value"]) for fact in facts]
            + [str(fact["field"]) for fact in facts]
            + sources
            + [str(item) for item in parsed["missing"]]
        )
    )
    forbidden_hits: list[str] = [
        item for item in forbidden if normalize_text(item) in haystack
    ]

    status_ok: bool = parsed["status"] == expected.get(
        "expected_status", expected.get("status")
    )
    tool_use_ok: bool = not missing_tools
    language_ok: bool = _language_ok(parsed)
    access_denied_leak: bool = parsed["status"] == "access_denied" and bool(facts or sources)

    contract_score: float = 20.0
    contract_score -= 10.0 * len(invalid_sources)
    contract_score -= 10.0 * len(invented_facts)
    contract_score -= 10.0 * invalid_fact_count
    contract_score -= 5.0 * len(missing_tools)
    contract_score -= 2.0 * max(0, tool_error_count)
    contract_score = max(0.0, contract_score)

    if expected_facts:
        facts_score: float = 35.0 * sum(facts_matched) / len(expected_facts)
    else:
        facts_score = 35.0 if status_ok else 0.0

    if sources_matched:
        sources_score: float = 20.0 * sum(sources_matched) / len(sources_matched)
    else:
        sources_score = 20.0 if status_ok and not invalid_sources else 0.0

    status_score: float = 15.0 if status_ok else 0.0
    language_score: float = 10.0 if language_ok else 0.0
    penalty: float = (
        20.0 * len(forbidden_hits)
        + 10.0 * len(invented_facts)
        + 10.0 * len(invalid_sources)
        + (15.0 if access_denied_leak else 0.0)
    )
    total: float = max(
        0.0,
        min(100.0, contract_score + facts_score + sources_score + status_score + language_score - penalty),
    )

    notes: list[str] = []
    if missing_tools:
        notes.append(f"не вызваны инструменты: {', '.join(missing_tools)}")
    if invalid_fact_count:
        notes.append(f"фактов с пустыми или неверными полями: {invalid_fact_count}")
    if invalid_sources:
        notes.append(f"недопустимые источники: {', '.join(invalid_sources)}")
    if invented_facts:
        notes.append(f"факты с выдуманными источниками: {len(invented_facts)}")
    if forbidden_hits:
        notes.append(f"запрещённые упоминания: {', '.join(forbidden_hits)}")
    if access_denied_leak:
        notes.append("при отказе в доступе возвращены факты или источники")

    hard_fail: bool = bool(
        invalid_sources or invented_facts or forbidden_hits or access_denied_leak
    )
    return Validation(
        contract_ok=not (invalid_sources or invented_facts or invalid_fact_count),
        status_ok=status_ok,
        tool_use_ok=tool_use_ok,
        language_ok=language_ok,
        score=total,
        contract_score=contract_score,
        facts_score=facts_score,
        sources_score=sources_score,
        status_score=status_score,
        language_score=language_score,
        facts_matched=facts_matched,
        sources_matched=sources_matched,
        invalid_sources=invalid_sources,
        invented_facts=invented_facts,
        forbidden_hits=forbidden_hits,
        missing_tools=missing_tools,
        notes=notes,
        hard_fail=hard_fail,
    )


def estimate_cost(
    models: Iterable[Mapping[str, Any]],
    case_count: int,
    *,
    input_tokens: int = 6000,
    output_tokens: int = 1800,
) -> list[dict[str, Any]]:
    """Estimate worst-case cost per model for a run."""

    rows: list[dict[str, Any]] = []
    for model in models:
        pricing: Mapping[str, Any] = model["pricing_per_token"]
        per_case: float = (
            input_tokens * float(pricing["prompt"])
            + output_tokens * float(pricing["completion"])
        )
        rows.append(
            {
                "model": model["id"],
                "cases": case_count,
                "estimated_rub": round(per_case * case_count, 2),
            }
        )
    return rows
