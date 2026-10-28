"""Offline tests for the arXiv ID range helpers.

Run from the repository root::

    python -m unittest src.test_ids -v
"""

from __future__ import annotations

import unittest

from src.ids import (
    IdRange,
    IdRangeError,
    arxiv_id_to_folder_key,
    count_total,
    expand_ids,
    iter_ids,
    parse_range_config,
    parse_ranges_config,
)

ACTUAL_RANGES = [
    ("24120010", "2025-08", 7872, 12871),
    ("24120035", "2025-07", 16659, 21658),
    ("24120232", "2025-02", 8235, 13234),
    ("24120260", "2025-01", 2642, 7641),
]


class TestIdFormatting(unittest.TestCase):
    def test_sequence_is_zero_padded_to_five_digits(self) -> None:
        self.assertEqual(
            IdRange("2025-08", 7872, "2025-08", 7872).first_id,
            "2508.07872",
        )
        self.assertEqual(
            IdRange("2025-01", 2642, "2025-01", 2642).first_id,
            "2501.02642",
        )
        self.assertEqual(
            IdRange("2025-01", 1, "2025-01", 1).first_id,
            "2501.00001",
        )
        self.assertEqual(
            IdRange("2025-01", 99999, "2025-01", 99999).first_id,
            "2501.99999",
        )

    def test_month_code_and_boundaries(self) -> None:
        item = IdRange("2025-08", 7872, "2025-08", 12871)
        self.assertEqual(item.month_code, "2508")
        self.assertEqual(item.first_id, "2508.07872")
        self.assertEqual(item.last_id, "2508.12871")
        self.assertEqual(item.count, 5000)

    def test_actual_ranges_are_inclusive_and_exactly_5000_ids(self) -> None:
        for student_id, month, start_id, end_id in ACTUAL_RANGES:
            with self.subTest(student_id=student_id):
                item = IdRange(month, start_id, month, end_id, student_id=student_id)
                self.assertEqual(item.count, 5000)
                self.assertEqual(item.end_id - item.start_id + 1, 5000)
                self.assertEqual(item.first_id, f"{item.month_code}.{start_id:05d}")
                self.assertEqual(item.last_id, f"{item.month_code}.{end_id:05d}")

    def test_full_assignment_has_20000_candidates(self) -> None:
        config = [
            {
                "id": student_id,
                "start_month": month,
                "start_id": start_id,
                "end_month": month,
                "end_id": end_id,
            }
            for student_id, month, start_id, end_id in ACTUAL_RANGES
        ]
        ranges = parse_ranges_config(config)

        self.assertEqual(count_total(ranges), 20000)
        ids = expand_ids(ranges)
        self.assertEqual(len(ids), 20000)
        self.assertEqual(len(set(ids)), 20000)

        expected_first = [
            "2508.07872",
            "2507.16659",
            "2502.08235",
            "2501.02642",
        ]
        expected_last = [
            "2508.12871",
            "2507.21658",
            "2502.13234",
            "2501.07641",
        ]
        for item, first, last in zip(ranges, expected_first, expected_last):
            with self.subTest(range=first):
                self.assertEqual(item.first_id, first)
                self.assertEqual(item.last_id, last)

    def test_folder_key_conversion(self) -> None:
        self.assertEqual(arxiv_id_to_folder_key("2508.07872"), "2508-07872")
        self.assertEqual(arxiv_id_to_folder_key("2501.02642"), "2501-02642")
        self.assertEqual(arxiv_id_to_folder_key("2507.16659"), "2507-16659")

    def test_folder_key_rejects_non_new_style_ids(self) -> None:
        for value in (
            "hep-th/9901001",
            "2501.2642",
            "2501.02642v2",
            "2501.026421",
            "2501-02642",
            "",
            250102642,
        ):
            with self.subTest(value=value):
                with self.assertRaises(IdRangeError):
                    arxiv_id_to_folder_key(value)  # type: ignore[arg-type]


class TestRangeValidation(unittest.TestCase):
    def test_cross_month_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(IdRangeError, "cross-month"):
            IdRange("2025-07", 1, "2025-08", 1)

    def test_invalid_month_formats_are_rejected(self) -> None:
        for month in ("2025-7", "25-07", "2025-13", "2025-00", "2025/07", "2025-07 ", ""):
            with self.subTest(month=month):
                with self.assertRaises(IdRangeError):
                    IdRange(month, 1, month, 1)

    def test_month_before_new_style_is_rejected(self) -> None:
        with self.assertRaises(IdRangeError):
            IdRange("2007-03", 1, "2007-03", 1)
        self.assertEqual(IdRange("2007-04", 1, "2007-04", 1).first_id, "0704.00001")

    def test_non_string_month_is_rejected(self) -> None:
        with self.assertRaises(IdRangeError):
            IdRange(202507, 1, "2025-07", 1)  # type: ignore[arg-type]

    def test_start_must_not_exceed_end(self) -> None:
        with self.assertRaisesRegex(IdRangeError, "start_id"):
            IdRange("2025-07", 10, "2025-07", 9)

    def test_invalid_sequences_are_rejected(self) -> None:
        for sequence in (0, -1, 100000, True, "abc", 1.5, None):
            with self.subTest(sequence=sequence):
                with self.assertRaises(IdRangeError):
                    IdRange("2025-07", sequence, "2025-07", 10)  # type: ignore[arg-type]

    def test_student_id_is_normalized(self) -> None:
        self.assertEqual(
            IdRange("2025-07", 1, "2025-07", 1, student_id=24120035).student_id,
            "24120035",
        )
        self.assertIsNone(IdRange("2025-07", 1, "2025-07", 1).student_id)

    def test_empty_student_id_is_rejected(self) -> None:
        with self.assertRaises(IdRangeError):
            IdRange("2025-07", 1, "2025-07", 1, student_id="  ")


class TestConfigParsing(unittest.TestCase):
    def test_parse_one_mapping_with_integer_values(self) -> None:
        item = parse_range_config(
            {
                "student_id": "24120035",
                "start_month": "2025-07",
                "start_id": 16659,
                "end_month": "2025-07",
                "end_id": 16661,
            }
        )
        self.assertEqual(item.student_id, "24120035")
        self.assertEqual(list(item.iter_ids()), ["2507.16659", "2507.16660", "2507.16661"])

    def test_parse_accepts_digit_strings_and_id_alias(self) -> None:
        item = parse_range_config(
            {
                "id": 24120010,
                "start_month": "2025-08",
                "start_id": "7872",
                "end_month": "2025-08",
                "end_id": "7874",
            }
        )
        self.assertEqual(item.student_id, "24120010")
        self.assertEqual(item.count, 3)

    def test_parse_ignores_extra_keys(self) -> None:
        item = parse_range_config(
            {
                "student_id": "24120035",
                "start_month": "2025-07",
                "start_id": 1,
                "end_month": "2025-07",
                "end_id": 1,
                "note": "extra metadata is allowed",
            }
        )
        self.assertEqual(item.count, 1)

    def test_missing_required_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(IdRangeError, "missing required"):
            parse_range_config(
                {
                    "start_month": "2025-07",
                    "start_id": 1,
                    "end_id": 2,
                }
            )

    def test_non_mapping_config_is_rejected(self) -> None:
        with self.assertRaises(IdRangeError):
            parse_range_config(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_empty_config_is_rejected(self) -> None:
        with self.assertRaisesRegex(IdRangeError, "at least one"):
            parse_ranges_config([])

    def test_mapping_instead_of_sequence_is_rejected(self) -> None:
        with self.assertRaises(IdRangeError):
            parse_ranges_config(  # type: ignore[arg-type]
                {"start_month": "2025-07", "start_id": 1, "end_month": "2025-07", "end_id": 2}
            )

    def test_overlapping_ranges_are_rejected(self) -> None:
        with self.assertRaisesRegex(IdRangeError, "overlapping"):
            parse_ranges_config(
                [
                    {
                        "start_month": "2025-07",
                        "start_id": 1,
                        "end_month": "2025-07",
                        "end_id": 10,
                    },
                    {
                        "start_month": "2025-07",
                        "start_id": 10,
                        "end_month": "2025-07",
                        "end_id": 20,
                    },
                ]
            )

    def test_adjacent_and_different_month_ranges_are_allowed(self) -> None:
        ranges = parse_ranges_config(
            [
                {
                    "start_month": "2025-07",
                    "start_id": 1,
                    "end_month": "2025-07",
                    "end_id": 10,
                },
                {
                    "start_month": "2025-07",
                    "start_id": 11,
                    "end_month": "2025-07",
                    "end_id": 20,
                },
                {
                    "start_month": "2025-08",
                    "start_id": 1,
                    "end_month": "2025-08",
                    "end_id": 10,
                },
            ]
        )
        self.assertEqual(count_total(ranges), 30)


class TestIdIteration(unittest.TestCase):
    def test_iter_ids_matches_expand_ids(self) -> None:
        ranges = [
            IdRange("2025-07", 1, "2025-07", 3),
            IdRange("2025-08", 1, "2025-08", 2),
        ]
        self.assertEqual(list(iter_ids(ranges)), expand_ids(ranges))

    def test_empty_range_list_returns_empty(self) -> None:
        self.assertEqual(expand_ids([]), [])
        self.assertEqual(count_total([]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
