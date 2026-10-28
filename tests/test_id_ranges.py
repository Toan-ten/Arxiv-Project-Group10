import copy
import io
import json
import os
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import GeneratorType
from unittest.mock import patch

from src.id_ranges import (
    AssignedRange,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    arxiv_id_to_folder_key,
    format_arxiv_id,
    iter_assigned_ids,
    load_config,
    main,
    parse_config,
)


class FormattingTests(unittest.TestCase):
    def test_five_digits_and_months(self):
        cases = [
            ("2025-08", 7872, "2508.07872"),
            ("2025-02", 8235, "2502.08235"),
            ("2025-01", 2642, "2501.02642"),
            ("2025-01", 7641, "2501.07641"),
            ("2025-12", 1, "2512.00001"),
            ("2007-04", 1, "0704.00001"),
            ("2014-12", 1, "1412.00001"),
            ("2015-01", 99999, "1501.99999"),
            ("2099-12", 12, "9912.00012"),
        ]
        for month, serial, expected in cases:
            with self.subTest(month=month, serial=serial):
                self.assertEqual(format_arxiv_id(month, serial), expected)

    def test_invalid_months(self):
        for month in (
            None, 202508, True, "2025-8", "25-08", "2025-00", "2025-13",
            "2025/08", "2025-08-01", " 2025-08", "2025-08\n", "２０２５-０８",
            "0000-01", "2007-03", "2100-01",
        ):
            with self.subTest(month=month), self.assertRaises(ConfigError):
                format_arxiv_id(month, 7872)

    def test_invalid_serials(self):
        for serial in (0, -1, 100000, "7872", "07872", 7872.0, True, False, None):
            with self.subTest(serial=serial), self.assertRaises(ConfigError):
                format_arxiv_id("2025-08", serial)


class FolderKeyTests(unittest.TestCase):
    def test_valid_ids_are_converted(self):
        cases = {
            "2508.07872": "2508-07872",
            "2501.02642": "2501-02642",
            "0704.00001": "0704-00001",
            "9912.99999": "9912-99999",
        }
        for arxiv_id, expected in cases.items():
            with self.subTest(arxiv_id=arxiv_id):
                self.assertEqual(arxiv_id_to_folder_key(arxiv_id), expected)

    def test_invalid_ids_are_rejected(self):
        for arxiv_id in (
            "hep-th/9901001",
            "2501.2642",
            "2501.02642v2",
            "2501.00000",
            "2501.026421",
            "2501-02642",
            "0000.00001",
            "0703.00001",
            "2007-03.00001",
            "",
            250102642,
        ):
            with self.subTest(arxiv_id=arxiv_id), self.assertRaises(ConfigError):
                arxiv_id_to_folder_key(arxiv_id)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig"))

    def test_real_assignments_preserve_sheet_values_and_ownership(self):
        config = load_config()
        expected = {
            "24120010": ("2025-08", 7872, "2025-08", 12871,
                         "2508.07872", "2508.12871"),
            "24120035": ("2025-07", 16659, "2025-07", 21658,
                         "2507.16659", "2507.21658"),
            "24120232": ("2025-02", 8235, "2025-02", 13234,
                         "2502.08235", "2502.13234"),
            "24120260": ("2025-01", 2642, "2025-01", 7641,
                         "2501.02642", "2501.07641"),
        }
        actual = {
            mssv: (m.start_month, m.start_id, m.end_month, m.end_id,
                   m.start_arxiv_id, m.end_arxiv_id)
            for mssv, m in config.items()
        }
        self.assertEqual(actual, expected)
        self.assertEqual(set(self.data), {"members"})
        for row in self.data["members"]:
            self.assertEqual(
                set(row), {"mssv", "start_month", "start_id", "end_month", "end_id"}
            )
            self.assertEqual(config[row["mssv"]].mssv, row["mssv"])

    def test_member_order_does_not_change_ownership(self):
        self.data["members"].reverse()
        mapping = parse_config(self.data)
        self.assertEqual(mapping, load_config())

    def test_mssv_keeps_leading_zeros(self):
        self.data["members"][0]["mssv"] = "00120010"
        mapping = parse_config(self.data)
        self.assertEqual(mapping["00120010"].mssv, "00120010")

    def test_missing_and_unknown_fields(self):
        for location in ("config", "member"):
            original = self.data if location == "config" else self.data["members"][0]
            for field in original:
                data = copy.deepcopy(self.data)
                target = data if location == "config" else data["members"][0]
                del target[field]
                with self.subTest(location=location, missing=field):
                    with self.assertRaises(ConfigError):
                        parse_config(data)
            data = copy.deepcopy(self.data)
            target = data if location == "config" else data["members"][0]
            target["typo"] = "unexpected"
            with self.subTest(location=location, extra="typo"):
                with self.assertRaises(ConfigError):
                    parse_config(data)

    def test_invalid_root_and_members_structure(self):
        for data in (None, [], "config", 10):
            with self.subTest(root=data), self.assertRaises(ConfigError):
                parse_config(data)
        for members in (None, {}, "members", [], self.data["members"][:3],
                        self.data["members"] + [self.data["members"][0]],
                        [None] * 4):
            data = {**self.data, "members": members}
            with self.subTest(members=members), self.assertRaises(ConfigError):
                parse_config(data)

    def test_removed_fields_are_rejected(self):
        for field in ("group_id", "boundary_policy", "source"):
            data = {**self.data, field: "unused"}
            with self.subTest(field=field), self.assertRaises(ConfigError):
                parse_config(data)
        for field in ("member_id", "member_name", "sheet_row"):
            data = copy.deepcopy(self.data)
            data["members"][0][field] = "unused"
            with self.subTest(field=field), self.assertRaises(ConfigError):
                parse_config(data)

    def test_invalid_member_fields_and_boundaries(self):
        cases = {
            "mssv": (None, [], 24120010, True, 24120010.0, "", " ", "A",
                     " 24120010", "24120010\n", "2412 0010", "２４１２００１０"),
            "start_month": (None, "2025-00", "2025-8", "2025-09"),
            "end_month": (None, "2025-13", "2025-07"),
            "start_id": (None, 0, -1, 100000, True, "7872", 7872.0, 12872),
            "end_id": (None, 0, -1, 100000, True, "12871", 12871.0, 7871),
        }
        for field, values in cases.items():
            for value in values:
                data = copy.deepcopy(self.data)
                data["members"][0][field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ConfigError):
                        parse_config(data)

    def test_duplicate_mssv_is_rejected(self):
        self.data["members"][1]["mssv"] = self.data["members"][0]["mssv"]
        with self.assertRaisesRegex(ConfigError, "duplicate mssv: 24120010"):
            parse_config(self.data)

    def test_cross_month_and_year_only_compare_boundaries(self):
        original = load_config()["24120010"]
        changed = replace(original, start_month="2025-12", start_id=99999,
                          end_month="2026-01", end_id=1)
        self.assertEqual(changed.start_arxiv_id, "2512.99999")
        self.assertEqual(changed.end_arxiv_id, "2601.00001")
        with self.assertRaises(ConfigError):
            replace(changed, end_month="2025-11")

    def test_cross_month_config_is_rejected_at_parse_time(self):
        data = copy.deepcopy(self.data)
        data["members"][0]["end_month"] = "2025-09"
        data["members"][0]["end_id"] = 1
        with self.assertRaisesRegex(ConfigError, "same-month"):
            parse_config(data)

    def test_overlapping_member_ranges_are_rejected(self):
        data = copy.deepcopy(self.data)
        data["members"][1].update(
            {
                "start_month": "2025-08",
                "start_id": 8000,
                "end_month": "2025-08",
                "end_id": 8001,
            }
        )
        with self.assertRaisesRegex(ConfigError, "overlapping ranges"):
            parse_config(data)

    def test_equal_boundaries_are_valid(self):
        original = load_config()["24120010"]
        changed = replace(original, end_id=original.start_id)
        self.assertEqual(changed.start_arxiv_id, changed.end_arxiv_id)

    def test_assignments_cannot_be_mutated_accidentally(self):
        assignment = load_config()["24120010"]
        with self.assertRaises(FrozenInstanceError):
            assignment.start_id = 1

    def test_default_path_is_independent_of_working_directory(self):
        self.assertTrue(DEFAULT_CONFIG_PATH.is_absolute())
        previous_directory = Path.cwd()
        try:
            os.chdir(DEFAULT_CONFIG_PATH.parent)
            self.assertEqual(Path.cwd(), DEFAULT_CONFIG_PATH.parent)
            self.assertEqual(load_config()["24120035"].end_id, 21658)
        finally:
            os.chdir(previous_directory)


class AssignedIdIterationTests(unittest.TestCase):
    def test_group10_counts_mapping_and_uniqueness(self):
        expected = {
            "24120232": ("2502.08235", "2502.13234"),
            "24120010": ("2508.07872", "2508.12871"),
            "24120035": ("2507.16659", "2507.21658"),
            "24120260": ("2501.02642", "2501.07641"),
        }
        counts = Counter()
        first = {}
        last = {}
        seen = set()
        for mssv, arxiv_id in iter_assigned_ids():
            self.assertIsInstance(mssv, str)
            self.assertIn(mssv, expected)
            start, end = expected[mssv]
            self.assertEqual(arxiv_id[:5], start[:5])
            self.assertTrue(start <= arxiv_id <= end)
            self.assertRegex(arxiv_id, r"^[0-9]{4}\.[0-9]{5}$")
            self.assertNotIn(arxiv_id, seen)
            seen.add(arxiv_id)
            counts[mssv] += 1
            first.setdefault(mssv, arxiv_id)
            last[mssv] = arxiv_id
        self.assertEqual(counts, {mssv: 5000 for mssv in expected})
        self.assertEqual(sum(counts.values()), 20000)
        self.assertEqual(len(seen), 20000)
        self.assertEqual(first, {mssv: bounds[0] for mssv, bounds in expected.items()})
        self.assertEqual(last, {mssv: bounds[1] for mssv, bounds in expected.items()})

    def test_small_range_includes_both_boundaries_and_padding(self):
        member = AssignedRange("24120232", "2025-02", 9, "2025-02", 11)
        self.assertEqual(
            list(iter_assigned_ids({member.mssv: member})),
            [("24120232", "2502.00009"), ("24120232", "2502.00010"),
             ("24120232", "2502.00011")],
        )

    def test_equal_boundaries_yield_one_id(self):
        member = AssignedRange("00120010", "2025-08", 7872, "2025-08", 7872)
        self.assertEqual(
            list(iter_assigned_ids({member.mssv: member})),
            [("00120010", "2508.07872")],
        )

    def test_cross_month_ranges_are_rejected_before_any_id(self):
        for start_month, end_month in (("2025-01", "2025-02"),
                                       ("2025-12", "2026-01")):
            config = load_config()
            config["24120260"] = replace(
                config["24120260"], start_month=start_month, end_month=end_month
            )
            with self.subTest(start=start_month, end=end_month):
                with self.assertRaisesRegex(ConfigError, "24120260.*same-month"):
                    next(iter_assigned_ids(config))

    def test_overlapping_direct_config_is_rejected_before_any_id(self):
        config = load_config()
        config["24120035"] = replace(
            config["24120035"],
            start_month="2025-08",
            start_id=8000,
            end_month="2025-08",
            end_id=8001,
        )
        with self.assertRaisesRegex(ConfigError, "overlapping ranges"):
            next(iter_assigned_ids(config))

    def test_ids_are_formatted_only_as_requested(self):
        config = load_config()
        with patch("src.id_ranges.format_arxiv_id", wraps=format_arxiv_id) as formatter:
            ids = iter_assigned_ids(config)
            self.assertIsInstance(ids, GeneratorType)
            formatter.assert_not_called()
            self.assertEqual(next(ids), ("24120010", "2508.07872"))
            self.assertEqual(formatter.call_count, 1)
            self.assertEqual(next(ids), ("24120010", "2508.07873"))
            self.assertEqual(formatter.call_count, 2)

    def test_loading_errors_are_not_silenced(self):
        with patch("src.id_ranges.load_config", side_effect=ConfigError("bad config")):
            with self.assertRaisesRegex(ConfigError, "bad config"):
                next(iter_assigned_ids())

    def test_empty_assignments_do_not_load_default_config(self):
        with patch("src.id_ranges.load_config") as loader:
            self.assertEqual(list(iter_assigned_ids({})), [])
            loader.assert_not_called()


class LoaderAndCommandTests(unittest.TestCase):
    def test_load_explicit_path(self):
        self.assertEqual(load_config(str(DEFAULT_CONFIG_PATH)), load_config())

    def test_read_errors_are_config_errors(self):
        for error in (FileNotFoundError("missing"), PermissionError("denied"),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")):
            with self.subTest(error=type(error).__name__):
                with patch.object(Path, "read_text", side_effect=error):
                    with self.assertRaises(ConfigError):
                        load_config("unused.json")

    def test_malformed_json_is_rejected(self):
        with patch.object(Path, "read_text", return_value="{not valid JSON}"):
            with self.assertRaises(ConfigError):
                load_config("unused.json")

    def test_duplicate_json_keys_are_rejected(self):
        texts = ('{"members": [], "members": []}',
                 '{"members": [{"start_id": 1, "start_id": 2}]}')
        for text in texts:
            with self.subTest(text=text):
                with patch.object(Path, "read_text", return_value=text):
                    with self.assertRaisesRegex(ConfigError, "duplicate JSON key"):
                        load_config("unused.json")

    def test_command_shows_mssv_and_formatted_boundaries(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"members"})
        members = {row["mssv"]: row for row in result["members"]}
        self.assertEqual(set(members), {"24120010", "24120035", "24120232", "24120260"})
        self.assertEqual(members["24120010"]["start_id"], 7872)
        self.assertEqual(members["24120010"]["start_arxiv_id"], "2508.07872")
        self.assertEqual(members["24120010"]["end_arxiv_id"], "2508.12871")
        for row in members.values():
            self.assertEqual(
                set(row),
                {"mssv", "start_month", "start_id", "end_month", "end_id",
                 "start_arxiv_id", "end_arxiv_id"},
            )

    def test_command_reports_bad_config_with_nonzero_exit(self):
        output = io.StringIO()
        with patch("src.id_ranges.load_config", side_effect=ConfigError("bad config")):
            with redirect_stderr(output), self.assertRaises(SystemExit) as context:
                main(["invalid.json"])
        self.assertEqual(context.exception.code, 2)
        self.assertIn("bad config", output.getvalue())


if __name__ == "__main__":
    unittest.main()
