"""Record bounded replay arguments and hashes, not copies of Odoo projections."""

from datetime import date
from hashlib import sha256
import json
import re

from .task_read_map import ReadTool
from .task_read_plan import ReadStep, ReadingContext

type Evidence = tuple[ReadStep, str]

MAX_EVIDENCE: int = 16
_DIGEST: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


def collect_evidence(context: ReadingContext, previous: tuple[Evidence, ...] = ()) -> tuple[Evidence, ...]:
    """Bind a reply to current and inherited reads; collect_evidence(context) keeps no source text."""
    current: tuple[Evidence, ...] = tuple((item.step, sha256(item.encoded).hexdigest()) for item in context.observations)
    combined: tuple[Evidence, ...] = tuple(dict.fromkeys((*current, *previous)))
    if not current or len(combined) > MAX_EVIDENCE:
        raise ValueError("Invalid dialogue evidence size")
    return combined


def encode_evidence(evidence: tuple[Evidence, ...]) -> str:
    """Encode checked replay metadata; decode_evidence(encode_evidence(evidence)) restores it."""
    if not 1 <= len(evidence) <= MAX_EVIDENCE:
        raise ValueError("Invalid dialogue evidence size")
    return json.dumps([1, [[step.tool.value, step.start.isoformat() if step.start else None,
                           step.end.isoformat() if step.end else None, step.limit, digest]
                          for step, digest in evidence]], separators=(",", ":"))


def decode_evidence(payload: str) -> tuple[Evidence, ...]:
    """Validate persisted replay metadata; decode_evidence('[]') raises ValueError."""
    if type(payload) is not str or len(payload) > 4096:
        raise ValueError("Invalid dialogue evidence")
    raw: object = json.loads(payload)
    if (type(raw) is not list or len(raw) != 2 or type(raw[0]) is not int or raw[0] != 1
            or type(raw[1]) is not list or not 1 <= len(raw[1]) <= MAX_EVIDENCE):
        raise ValueError("Invalid dialogue evidence")
    result: list[Evidence] = []
    for item in raw[1]:
        if type(item) is not list or len(item) != 5:
            raise ValueError("Invalid dialogue evidence row")
        tool, start, end, limit, digest = item
        if (type(tool) is not str or (start is not None and type(start) is not str)
                or (end is not None and type(end) is not str) or (limit is not None and type(limit) is not int)
                or type(digest) is not str or not _DIGEST.fullmatch(digest)):
            raise ValueError("Invalid dialogue evidence row")
        step: ReadStep = ReadStep(ReadTool(tool), date.fromisoformat(start) if start is not None else None,
                                  date.fromisoformat(end) if end is not None else None, limit).checked()
        result.append((step, digest))
    return tuple(result)
