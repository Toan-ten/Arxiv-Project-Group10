import argparse
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "group10_id_ranges.json"
)

# New-style arXiv IDs started in April 2007.
_MIN_MONTH = "2007-04"
_MAX_MONTH = "2099-12"
_ARXIV_ID_RE = re.compile(r"^(?P<yymm>[0-9]{4})\.(?P<serial>[0-9]{5})$")


class ConfigError(ValueError):
    pass


def _positive_integer(value: object, field: str) -> None:
    # bool is also an int, so check the exact type.
    if type(value) is not int or value < 1:
        raise ConfigError(f"{field} must be a positive integer (not bool)")


def format_arxiv_id(month: str, serial: int) -> str:
    if not isinstance(month, str) or not re.fullmatch(
        r"[0-9]{4}-(0[1-9]|1[0-2])", month
    ):
        raise ConfigError("month must use YYYY-MM with a month from 01 to 12")
    if not _MIN_MONTH <= month <= _MAX_MONTH:
        raise ConfigError(f"five-digit ID scope is {_MIN_MONTH} through {_MAX_MONTH}")
    _positive_integer(serial, "ID")
    if serial > 99999:
        raise ConfigError("ID must be at most 99999 (five digits)")
    return f"{month[2:4]}{month[5:7]}.{serial:05d}"


def arxiv_id_to_folder_key(arxiv_id: str) -> str:
    """Convert a new-style ID such as ``2508.07872`` to ``2508-07872``."""

    if not isinstance(arxiv_id, str):
        raise ConfigError("arXiv ID must be a string")
    match = _ARXIV_ID_RE.fullmatch(arxiv_id)
    if match is None:
        raise ConfigError(
            f"expected a new-style arXiv ID such as 2507.16659: {arxiv_id!r}"
        )

    yymm = match.group("yymm")
    serial = int(match.group("serial"))
    if serial < 1:
        raise ConfigError("ID must be between 1 and 99999 (five digits)")

    month_number = int(yymm[2:])
    if not 1 <= month_number <= 12:
        raise ConfigError(f"invalid month in arXiv ID: {arxiv_id!r}")

    year = 2000 + int(yymm[:2])
    month = f"{year:04d}-{month_number:02d}"
    if not _MIN_MONTH <= month <= _MAX_MONTH:
        raise ConfigError(f"arXiv ID is outside {_MIN_MONTH} through {_MAX_MONTH}")
    return f"{yymm}-{serial:05d}"


@dataclass(frozen=True)
class AssignedRange:
    mssv: str
    start_month: str
    start_id: int
    end_month: str
    end_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.mssv, str) or not re.fullmatch(r"[0-9]+", self.mssv):
            raise ConfigError("mssv must be a non-empty string of digits")
        for label, month, serial in (
            ("start", self.start_month, self.start_id),
            ("end", self.end_month, self.end_id),
        ):
            try:
                format_arxiv_id(month, serial)
            except ConfigError as exc:
                raise ConfigError(f"{self.mssv}.{label}: {exc}") from exc
        if (self.start_month, self.start_id) > (self.end_month, self.end_id):
            raise ConfigError(f"{self.mssv}: start must not be after end")

    @property
    def start_arxiv_id(self) -> str:
        return format_arxiv_id(self.start_month, self.start_id)

    @property
    def end_arxiv_id(self) -> str:
        return format_arxiv_id(self.end_month, self.end_id)


def _require_fields(value: object, expected: set[str], context: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ConfigError(
            f"{context} must be an object with exactly: {', '.join(sorted(expected))}"
        )


def _ensure_no_overlaps(members: dict[str, AssignedRange]) -> None:
    by_month: dict[str, list[AssignedRange]] = {}
    for member in members.values():
        by_month.setdefault(member.start_month, []).append(member)

    for month, group in by_month.items():
        ordered = sorted(group, key=lambda member: member.start_id)
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_id <= previous.end_id:
                raise ConfigError(
                    f"overlapping ranges in {month}: "
                    f"{previous.mssv} and {current.mssv}"
                )


def parse_config(data: object) -> dict[str, AssignedRange]:
    _require_fields(data, {"members"}, "config")
    if not isinstance(data["members"], list) or len(data["members"]) != 4:
        raise ConfigError("members must be a list of four assignments")

    members = {}
    for index, row in enumerate(data["members"]):
        _require_fields(
            row,
            {
                "mssv", "start_month", "start_id", "end_month", "end_id",
            },
            f"members[{index}]",
        )
        member = AssignedRange(**row)
        if member.mssv in members:
            raise ConfigError(f"duplicate mssv: {member.mssv}")
        if member.start_month != member.end_month:
            raise ConfigError(f"{member.mssv}: only same-month ranges are supported")
        members[member.mssv] = member

    _ensure_no_overlaps(members)
    return members


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, AssignedRange]:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(text, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    return parse_config(data)


def iter_assigned_ids(
    config: dict[str, AssignedRange] | None = None,
) -> Iterator[tuple[str, str]]:
    if config is None:
        config = load_config()
    for member in config.values():
        if member.start_month != member.end_month:
            raise ConfigError(f"{member.mssv}: only same-month ranges are supported")
    _ensure_no_overlaps(config)
    for member in config.values():
        for serial in range(member.start_id, member.end_id + 1):
            yield member.mssv, format_arxiv_id(member.start_month, serial)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    result = {
        "members": [
            {
                **asdict(member),
                "start_arxiv_id": member.start_arxiv_id,
                "end_arxiv_id": member.end_arxiv_id,
            }
            for member in config.values()
        ]
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
