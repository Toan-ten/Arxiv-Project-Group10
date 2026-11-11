import json
import unittest
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from src import pilots

from src.id_ranges import AssignedRange, ConfigError, load_config
from src.pilots import build_pilot_set, build_smoke_set

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
PILOTS_DIR = CONFIG_DIR / "pilots"


def _pilot_file_path(filename: str) -> Path:
    nested = PILOTS_DIR / filename
    if nested.exists():
        return nested
    return CONFIG_DIR / filename


class PilotSetTests(unittest.TestCase):
    def setUp(self):
        self.members = list(load_config().values())
        self.by_mssv = {member.mssv: member for member in self.members}

    def assert_entry_belongs_to_member(self, entry: dict) -> None:
        member = self.by_mssv[entry["mssv"]]
        self.assertEqual(entry["range_start"], member.start_arxiv_id)
        self.assertEqual(entry["range_end"], member.end_arxiv_id)
        self.assertEqual(entry["arxiv_id"][:5], member.start_arxiv_id[:5])
        serial = int(entry["arxiv_id"][5:])
        self.assertGreaterEqual(serial, member.start_id)
        self.assertLessEqual(serial, member.end_id)

    def test_smoke_set_has_ten_unique_ids(self):
        entries = build_smoke_set(self.members)

        self.assertEqual(len(entries), 10)
        self.assertEqual(len({entry.arxiv_id for entry in entries}), 10)
        expected_counts = {member.mssv: 2 for member in self.members}
        for member in self.members[:2]:
            expected_counts[member.mssv] += 1
        self.assertEqual(Counter(entry.mssv for entry in entries), expected_counts)
        self.assertEqual(
            Counter(entry.reason for entry in entries),
            {"range_start": 4, "range_end": 4, "mid": 2},
        )

    def test_smoke_set_includes_every_boundary(self):
        entries = build_smoke_set(self.members)

        for member in self.members:
            ids = {
                entry.arxiv_id for entry in entries if entry.mssv == member.mssv
            }
            self.assertIn(member.start_arxiv_id, ids)
            self.assertIn(member.end_arxiv_id, ids)

    def test_smoke_midpoints_come_from_first_two_ranges(self):
        entries = build_smoke_set(self.members)
        mids = [entry for entry in entries if entry.reason == "mid"]

        self.assertEqual([entry.mssv for entry in mids], [
            member.mssv for member in self.members[:2]
        ])
        for entry in mids:
            member = self.by_mssv[entry.mssv]
            self.assertEqual(
                int(entry.arxiv_id[5:]),
                (member.start_id + member.end_id) // 2,
            )

    def test_pilot_set_has_500_unique_ids_and_125_per_range(self):
        entries = build_pilot_set(self.members)

        self.assertEqual(len(entries), 500)
        self.assertEqual(len({entry.arxiv_id for entry in entries}), 500)
        self.assertEqual(Counter(entry.mssv for entry in entries), {
            member.mssv: 125 for member in self.members
        })

    def test_pilot_set_includes_boundaries_and_even_spacing(self):
        entries = build_pilot_set(self.members)
        by_mssv: dict[str, list] = {}
        for entry in entries:
            by_mssv.setdefault(entry.mssv, []).append(entry)

        for member in self.members:
            group = by_mssv[member.mssv]
            self.assertEqual(len(group), 125)
            self.assertEqual(group[0].arxiv_id, member.start_arxiv_id)
            self.assertEqual(group[-1].arxiv_id, member.end_arxiv_id)
            self.assertEqual(group[0].reason, "pilot_start")
            self.assertEqual(group[-1].reason, "pilot_end")
            serials = [int(entry.arxiv_id[5:]) for entry in group]
            self.assertEqual(serials, sorted(serials))
            self.assertEqual(len(set(serials)), 125)
            gaps = [
                later - earlier
                for earlier, later in zip(serials, serials[1:])
            ]
            self.assertLessEqual(max(gaps) - min(gaps), 1)

    def test_pilot_set_is_deterministic(self):
        self.assertEqual(
            [asdict(entry) for entry in build_pilot_set(self.members)],
            [asdict(entry) for entry in build_pilot_set(self.members)],
        )

    def test_committed_pilot_files_match_builder(self):
        smoke_path = _pilot_file_path("pilot_10.json")
        pilot_path = _pilot_file_path("pilot_500.json")

        smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))

        self.assertEqual(
            smoke,
            [asdict(entry) for entry in build_smoke_set(self.members)],
        )
        self.assertEqual(
            pilot,
            [asdict(entry) for entry in build_pilot_set(self.members)],
        )

    def test_committed_pilot_entries_have_valid_schema_and_ranges(self):
        expected_fields = {
            "arxiv_id", "mssv", "range_start", "range_end", "reason"
        }
        for filename in ("pilot_10.json", "pilot_500.json"):
            with self.subTest(filename=filename):
                entries = json.loads(
                    _pilot_file_path(filename).read_text(encoding="utf-8")
                )
                for entry in entries:
                    self.assertEqual(set(entry), expected_fields)
                    self.assertRegex(entry["arxiv_id"], r"^[0-9]{4}\.[0-9]{5}$")
                    self.assert_entry_belongs_to_member(entry)

    def test_pilot_requires_enough_ids(self):
        member = AssignedRange("24120035", "2025-07", 16659, "2025-07", 16660)
        with self.assertRaisesRegex(ValueError, "only 2 IDs"):
            build_pilot_set([member], per_range=3)

    def test_cross_month_range_is_rejected(self):
        member = AssignedRange("24120035", "2025-07", 16659, "2025-08", 16659)
        with self.assertRaisesRegex(ValueError, "same-month"):
            build_smoke_set([member])

    def test_overlapping_ranges_are_rejected(self):
        first = AssignedRange("24120035", "2025-07", 16659, "2025-07", 16660)
        second = AssignedRange("24120010", "2025-07", 16660, "2025-07", 16661)
        with self.assertRaisesRegex(ValueError, "overlapping ranges"):
            build_pilot_set([first, second], per_range=2)

    def test_empty_member_list_is_rejected(self):
        with self.assertRaises(ValueError):
            build_smoke_set([])
        with self.assertRaises(ValueError):
            build_pilot_set([])

    def test_overlap_without_duplicate_samples_is_rejected(self):
        # Both 5,000-ID ranges have disjoint sampled IDs, but overlap as ranges.
        first = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        second = AssignedRange("24120010", "2025-07", 120, "2025-07", 5119)
        for builder in (build_smoke_set, build_pilot_set):
            for members in ((first, second), (second, first)):
                with self.subTest(builder=builder.__name__, members=members):
                    with self.assertRaisesRegex(ConfigError, "overlapping ranges"):
                        builder(members)

    def test_identical_ranges_are_rejected_even_with_same_owner(self):
        first = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        other_owner = replace(first, mssv="24120010")
        for builder in (build_smoke_set, build_pilot_set):
            for members in ((first, other_owner), (first, first)):
                with self.subTest(builder=builder.__name__, members=members):
                    with self.assertRaisesRegex(ConfigError, "overlapping ranges"):
                        builder(members)

    def test_contained_range_is_rejected_in_either_input_order(self):
        outer = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        inner = AssignedRange("24120010", "2025-07", 120, "2025-07", 2500)
        for builder in (build_smoke_set, build_pilot_set):
            for members in ((outer, inner), (inner, outer)):
                with self.subTest(builder=builder.__name__, members=members):
                    with self.assertRaisesRegex(ConfigError, "overlapping ranges"):
                        builder(members)

    def test_adjacent_ranges_are_allowed_and_preserve_order(self):
        first = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        second = AssignedRange("24120010", "2025-07", 5100, "2025-07", 10099)
        smoke = build_smoke_set([first, second])
        self.assertEqual([entry.arxiv_id for entry in smoke], [
            "2507.00100", "2507.05099", "2507.05100", "2507.10099",
            "2507.02599", "2507.07599",
        ])
        self.assertEqual([entry.mssv for entry in smoke], [
            first.mssv, first.mssv, second.mssv, second.mssv,
            first.mssv, second.mssv,
        ])
        pilot = build_pilot_set([first, second])
        self.assertEqual(len(pilot), 250)
        self.assertEqual(len({entry.arxiv_id for entry in pilot}), 250)
        self.assertEqual([entry.mssv for entry in pilot],
                         [first.mssv] * 125 + [second.mssv] * 125)
        self.assertEqual(pilot[0].arxiv_id, first.start_arxiv_id)
        self.assertEqual(pilot[124].arxiv_id, first.end_arxiv_id)
        self.assertEqual(pilot[125].arxiv_id, second.start_arxiv_id)
        self.assertEqual(pilot[-1].arxiv_id, second.end_arxiv_id)

    def test_same_serial_ranges_in_different_months_are_allowed(self):
        first = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        second = AssignedRange("24120010", "2025-08", 100, "2025-08", 5099)
        for builder, count in ((build_smoke_set, 6), (build_pilot_set, 250)):
            with self.subTest(builder=builder.__name__):
                entries = builder([first, second])
                self.assertEqual(len(entries), count)
                self.assertEqual(len({entry.arxiv_id for entry in entries}), count)
                for entry in entries:
                    expected_prefix = "2507." if entry.mssv == first.mssv else "2508."
                    self.assertTrue(entry.arxiv_id.startswith(expected_prefix))

    def test_all_ranges_are_validated_before_any_sampling(self):
        valid = AssignedRange("24120232", "2025-02", 8235, "2025-02", 13234)
        first = AssignedRange("24120035", "2025-07", 100, "2025-07", 5099)
        second = AssignedRange("24120010", "2025-07", 120, "2025-07", 5119)
        cross_month = AssignedRange("24120260", "2025-12", 100, "2026-01", 5099)
        for builder in (build_smoke_set, build_pilot_set):
            for members, message in (
                ([valid, first, second], "overlapping ranges"),
                ([valid, cross_month], "same-month"),
            ):
                with self.subTest(builder=builder.__name__, members=members):
                    with patch("src.pilots._entry", wraps=pilots._entry) as entry:
                        with patch("src.pilots._spread_offsets",
                                   wraps=pilots._spread_offsets) as spread:
                            with self.assertRaisesRegex(ValueError, message):
                                builder(members)
                            entry.assert_not_called()
                            spread.assert_not_called()

    def test_group10_order_ownership_and_determinism_are_unchanged(self):
        before = list(self.members)
        for filename, builder in (
            ("pilot_10.json", build_smoke_set),
            ("pilot_500.json", build_pilot_set),
        ):
            with self.subTest(filename=filename):
                expected = json.loads(
                    _pilot_file_path(filename).read_text(encoding="utf-8")
                )
                for members in (self.members, tuple(self.members), self.members):
                    self.assertEqual(
                        [asdict(entry) for entry in builder(members)], expected
                    )
                self.assertEqual(self.members, before)


if __name__ == "__main__":
    unittest.main()
