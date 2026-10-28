"""arXiv identifier helpers for the Milestone 1 crawler.

Only new-style arXiv IDs (``YYMM.NNNNN``, introduced in April 2007) are
supported. Assigned ranges are inclusive on both ends and must stay inside a
single calendar month; cross-month ranges are rejected instead of being
silently expanded with the wrong month prefix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "IdRange",
    "IdRangeError",
    "arxiv_id_to_folder_key",
    "count_total",
    "expand_ids",
    "iter_ids",
    "parse_range_config",
    "parse_ranges_config",
]

_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_ARXIV_ID_RE = re.compile(r"^(?P<yymm>\d{4})\.(?P<sequence>\d{5})$")

# New-style identifiers started in April 2007.
_MIN_MONTH = (2007, 4)
_MIN_SEQUENCE = 1
_MAX_SEQUENCE = 99_999

_REQUIRED_KEYS = ("start_month", "start_id", "end_month", "end_id")


class IdRangeError(ValueError):
    """Raised when an arXiv ID range or identifier is invalid."""


def _parse_month(value: object, field: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise IdRangeError(f"{field} must be a string in YYYY-MM format")
    match = _MONTH_RE.fullmatch(value)
    if match is None:
        raise IdRangeError(f"{field} must be in YYYY-MM format: {value!r}")

    year = int(match.group("year"))
    month = int(match.group("month"))
    if (year, month) < _MIN_MONTH:
        raise IdRangeError(
            f"{field} must be on or after 2007-04 for new-style arXiv IDs"
        )
    return year, month


def _parse_sequence(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise IdRangeError(f"{field} must be an integer, not bool")

    if isinstance(value, int):
        sequence = value
    elif isinstance(value, str) and value.strip().isdigit():
        sequence = int(value.strip())
    else:
        raise IdRangeError(f"{field} must be an integer: {value!r}")

    if not _MIN_SEQUENCE <= sequence <= _MAX_SEQUENCE:
        raise IdRangeError(
            f"{field} must be between {_MIN_SEQUENCE} and {_MAX_SEQUENCE}: {sequence}"
        )
    return sequence


@dataclass(frozen=True)
class IdRange:
    """An inclusive, single-month range of new-style arXiv IDs."""

    start_month: str
    start_id: int
    end_month: str
    end_id: int
    student_id: str | None = None

    def __post_init__(self) -> None:
        _parse_month(self.start_month, "start_month")
        _parse_month(self.end_month, "end_month")

        if self.start_month != self.end_month:
            raise IdRangeError(
                "cross-month ranges are not supported in M1: "
                f"{self.start_month} -> {self.end_month}"
            )

        object.__setattr__(self, "start_id", _parse_sequence(self.start_id, "start_id"))
        object.__setattr__(self, "end_id", _parse_sequence(self.end_id, "end_id"))

        if self.start_id > self.end_id:
            raise IdRangeError(
                f"start_id must be <= end_id: {self.start_id} > {self.end_id}"
            )

        if self.student_id is not None:
            if isinstance(self.student_id, bool) or not isinstance(
                self.student_id, (str, int)
            ):
                raise IdRangeError("student_id must be a string or integer")
            student_id = str(self.student_id).strip()
            if not student_id:
                raise IdRangeError("student_id must not be empty")
            object.__setattr__(self, "student_id", student_id)

    @property
    def month_code(self) -> str:
        """Return the YYMM prefix, e.g. ``2025-07`` -> ``2507``."""

        year, month = _parse_month(self.start_month, "start_month")
        return f"{year % 100:02d}{month:02d}"

    @property
    def count(self) -> int:
        """Number of candidate IDs in the inclusive range."""

        return self.end_id - self.start_id + 1

    @property
    def first_id(self) -> str:
        return f"{self.month_code}.{self.start_id:05d}"

    @property
    def last_id(self) -> str:
        return f"{self.month_code}.{self.end_id:05d}"

    def iter_ids(self) -> Iterator[str]:
        """Yield every ID in the inclusive range, in ascending order."""

        month_code = self.month_code
        for sequence in range(self.start_id, self.end_id + 1):
            yield f"{month_code}.{sequence:05d}"


def parse_range_config(raw: Mapping[str, object]) -> IdRange:
    """Build an :class:`IdRange` from one config mapping.

    ``student_id`` is optional; ``id`` is accepted as an alias because the
    shared range table uses that column name.
    """

    if not isinstance(raw, Mapping):
        raise IdRangeError(f"range config must be a mapping, got {type(raw).__name__}")

    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise IdRangeError(f"range config is missing required key(s): {', '.join(missing)}")

    student_id = raw.get("student_id", raw.get("id"))
    return IdRange(
        start_month=raw["start_month"],
        start_id=raw["start_id"],
        end_month=raw["end_month"],
        end_id=raw["end_id"],
        student_id=student_id,
    )


def parse_ranges_config(config: Iterable[Mapping[str, object]]) -> list[IdRange]:
    """Parse and validate a non-empty sequence of range mappings."""

    if isinstance(config, (str, bytes, Mapping)):
        raise IdRangeError("config must be a sequence of range mappings")

    ranges = [parse_range_config(item) for item in config]
    if not ranges:
        raise IdRangeError("config must contain at least one range")

    ensure_disjoint(ranges)
    return ranges


def ensure_disjoint(ranges: Sequence[IdRange]) -> None:
    """Raise if two ranges in the same month overlap."""

    by_month: dict[str, list[IdRange]] = {}
    for item in ranges:
        by_month.setdefault(item.start_month, []).append(item)

    for month, group in by_month.items():
        ordered = sorted(group, key=lambda item: item.start_id)
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_id <= previous.end_id:
                raise IdRangeError(
                    f"overlapping ranges in {month}: "
                    f"{previous.first_id}..{previous.last_id} and "
                    f"{current.first_id}..{current.last_id}"
                )


def iter_ids(ranges: Iterable[IdRange]) -> Iterator[str]:
    """Yield all IDs from disjoint ranges, preserving range order."""

    ordered_ranges = list(ranges)
    ensure_disjoint(ordered_ranges)
    for item in ordered_ranges:
        yield from item.iter_ids()


def expand_ids(ranges: Iterable[IdRange]) -> list[str]:
    """Return all IDs from disjoint ranges as a list."""

    return list(iter_ids(ranges))


def count_total(ranges: Iterable[IdRange]) -> int:
    """Return the total number of candidate IDs across ranges."""

    return sum(item.count for item in ranges)


def arxiv_id_to_folder_key(arxiv_id: str) -> str:
    """Convert ``YYMM.NNNNN`` to the corpus folder key ``YYMM-NNNNN``."""

    if not isinstance(arxiv_id, str):
        raise IdRangeError(f"arXiv ID must be a string: {arxiv_id!r}")

    match = _ARXIV_ID_RE.fullmatch(arxiv_id)
    if match is None:
        raise IdRangeError(
            f"expected a new-style arXiv ID such as 2507.16659: {arxiv_id!r}"
        )
    return f"{match.group('yymm')}-{match.group('sequence')}"
