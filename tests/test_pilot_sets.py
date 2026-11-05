import json
import unittest
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from src.id_ranges import AssignedRange, load_config
from src.pilots import build_pilot_set, build_smoke_set

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


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
        smoke_path = CONFIG_DIR / "pilot_10.json"
        pilot_path = CONFIG_DIR / "pilot_500.json"

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
                    (CONFIG_DIR / filename).read_text(encoding="utf-8")
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
        with self.assertRaisesRegex(ValueError, "duplicate IDs"):
            build_pilot_set([first, second], per_range=2)

    def test_empty_member_list_is_rejected(self):
        with self.assertRaises(ValueError):
            build_smoke_set([])
        with self.assertRaises(ValueError):
            build_pilot_set([])


if __name__ == "__main__":
    unittest.main()
