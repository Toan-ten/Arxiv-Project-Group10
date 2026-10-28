"""Deterministic pilot selections for M1 crawling.

The pilot sets are derived only from the assigned ID ranges. They are not
network samples: ``pilot_10`` is for smoke tests and ``pilot_500`` is for the
controlled benchmark run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .id_ranges import AssignedRange, format_arxiv_id

DEFAULT_SMOKE_PER_RANGE = 2
DEFAULT_PILOT_PER_RANGE = 125


@dataclass(frozen=True)
class PilotEntry:
    arxiv_id: str
    mssv: str
    range_start: str
    range_end: str
    reason: str


def _entry(member: AssignedRange, serial: int, reason: str) -> PilotEntry:
    if member.start_month != member.end_month:
        raise ValueError(f"{member.mssv}: pilot sets require a same-month range")
    return PilotEntry(
        arxiv_id=format_arxiv_id(member.start_month, serial),
        mssv=member.mssv,
        range_start=member.start_arxiv_id,
        range_end=member.end_arxiv_id,
        reason=reason,
    )


def _dedupe(entries: list[PilotEntry]) -> list[PilotEntry]:
    seen: set[str] = set()
    unique: list[PilotEntry] = []
    for entry in entries:
        if entry.arxiv_id in seen:
            continue
        seen.add(entry.arxiv_id)
        unique.append(entry)
    return unique


def build_smoke_set(members: Sequence[AssignedRange]) -> list[PilotEntry]:
    """Return two boundary IDs per range, plus midpoints for the first two ranges."""

    if not members:
        raise ValueError("at least one assigned range is required")

    entries: list[PilotEntry] = []
    for member in members:
        entries.append(_entry(member, member.start_id, "range_start"))
        entries.append(_entry(member, member.end_id, "range_end"))

    for member in members[:2]:
        midpoint = (member.start_id + member.end_id) // 2
        if midpoint not in (member.start_id, member.end_id):
            entries.append(_entry(member, midpoint, "mid"))

    result = _dedupe(entries)
    if len(result) != len(members) * DEFAULT_SMOKE_PER_RANGE + min(len(members), 2):
        raise ValueError("smoke set has duplicate IDs; check assigned ranges")
    return result


def _spread_offsets(count: int, wanted: int) -> list[int]:
    if wanted < 2:
        raise ValueError("per_range must be at least 2")
    if count < wanted:
        raise ValueError(f"range has only {count} IDs, need at least {wanted}")

    offsets = [round(index * (count - 1) / (wanted - 1)) for index in range(wanted)]
    if len(set(offsets)) != wanted or offsets[0] != 0 or offsets[-1] != count - 1:
        raise ValueError("could not build a unique spread across the range")
    return offsets


def build_pilot_set(
    members: Sequence[AssignedRange],
    per_range: int = DEFAULT_PILOT_PER_RANGE,
) -> list[PilotEntry]:
    """Return a deterministic, evenly spread set including both boundaries."""

    if not members:
        raise ValueError("at least one assigned range is required")

    entries: list[PilotEntry] = []
    for member in members:
        count = member.end_id - member.start_id + 1
        offsets = _spread_offsets(count, per_range)
        for index, offset in enumerate(offsets):
            if index == 0:
                reason = "pilot_start"
            elif index == len(offsets) - 1:
                reason = "pilot_end"
            else:
                reason = "pilot_stride"
            entries.append(_entry(member, member.start_id + offset, reason))

    if len({entry.arxiv_id for entry in entries}) != len(entries):
        raise ValueError("pilot set contains duplicate IDs; check range overlaps")
    return entries


def write_pilot_file(path: str | Path, entries: Sequence[PilotEntry]) -> None:
    """Write a pilot set as UTF-8 JSON with a trailing newline."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
