import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Tuple

from scripts.aggregate_m1_group import (
    EXPECTED_MEMBERS,
    audit_group_campaign,
    generate_markdown_report,
    main,
    parse_member_roots,
)
from src.fast_production import cohort_pairs, member_cohort_range
from src.storage import arxiv_id_to_folder_key


def _create_valid_cohort_archive(
    archives_dir: Path,
    cohort_num: int,
    num_papers: int = 100,
) -> Tuple[Path, str]:
    """Helper to create an actual gzipped tarball with valid paper folders, manifests, and files."""
    pairs = cohort_pairs(cohort_num)[:num_papers]
    arc_name = f"cohort_{cohort_num:04d}_corpus.tar.gz"
    arc_path = archives_dir / arc_name

    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        for mssv, aid in pairs:
            fkey = arxiv_id_to_folder_key(aid)
            pdir = temp_root / fkey
            pdir.mkdir(parents=True, exist_ok=True)
            vdir = pdir / "v1"
            vdir.mkdir(parents=True, exist_ok=True)
            tex_content = b"\\documentclass{article}\\begin{document}Hello\\end{document}"
            (vdir / "main.tex").write_bytes(tex_content)
            fhash = hashlib.sha256(tex_content).hexdigest()

            (pdir / "metadata.json").write_text(
                json.dumps({
                    "status": "complete", "arxiv_id": aid, "mssv": mssv, "title": "Paper",
                }),
                encoding="utf-8",
            )
            (pdir / "references.json").write_text(
                json.dumps({
                    "status": "complete", "arxiv_id": aid, "items": [],
                }),
                encoding="utf-8",
            )
            (pdir / "manifest.json").write_text(
                json.dumps({
                    "contract_version": 2, "arxiv_id": aid, "mssv": mssv,
                    "discovery_status": "found", "version_status": "resolved",
                    "metadata_status": "complete", "references_status": "complete",
                    "status": "complete",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid}v1", "status": "complete", "source_status": "complete",
                            "files": ["main.tex"], "file_checksums": {"main.tex": fhash},
                        }
                    },
                }),
                encoding="utf-8",
            )

        with tarfile.open(arc_path, "w:gz") as tar:
            for item in sorted(temp_root.iterdir()):
                tar.add(item, arcname=item.name)

    sha = hashlib.sha256(arc_path.read_bytes()).hexdigest()
    (archives_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
    manifest = {
        "cohort_number": cohort_num,
        "cohort_id": cohort_num,
        "archive_state": "m1_handoff_verified",
        "archive_sha256": sha,
        "included_count": len(pairs),
        "omitted_count": 0,
    }
    (archives_dir / f"cohort_{cohort_num:04d}_corpus.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return arc_path, sha


def _create_valid_benchmark_report(
    reports_dir: Path,
    cohort_num: int,
    attempt_id: str,
    elapsed_seconds: float = 120.0,
    average_ram_mb: float = 250.0,
    peak_ram_mb: float = 300.0,
    sample_count: int = 50,
) -> Path:
    """Helper to create a realistic exp-fast-... benchmark directory."""
    exp_dir = reports_dir / f"exp-fast-{cohort_num:04d}-1762734000"
    exp_dir.mkdir(parents=True, exist_ok=True)

    res_data = {
        "status": "success",
        "elapsed_seconds": elapsed_seconds,
        "average_ram_mb": average_ram_mb,
        "peak_ram_mb": peak_ram_mb,
        "disk_corpus_size_mb": 15.0,
        "metrics_v2": {
            "ram": {
                "sample_count": sample_count,
                "average_bytes": int(average_ram_mb * 1024 * 1024),
                "sampled_peak_bytes": int(peak_ram_mb * 1024 * 1024),
            },
            "disk": {
                "sampled_peak_bytes": int(15.0 * 1024 * 1024),
            },
        },
    }
    rep_data = {
        "attempt_ids": [attempt_id],
        "configuration": {
            "s2": {"timeout": 30.0},
        },
        "timing_metrics": {
            "total_wall_seconds": elapsed_seconds,
            "references_wall_seconds": 25.0,
        },
        "resource_metrics": res_data,
    }

    (exp_dir / "resources.json").write_text(json.dumps(res_data), encoding="utf-8")
    (exp_dir / "report.json").write_text(json.dumps(rep_data), encoding="utf-8")
    return exp_dir


class TestGroupAggregator(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_parse_member_roots(self):
        for mssv in EXPECTED_MEMBERS:
            (self.base_dir / mssv).mkdir(parents=True, exist_ok=True)

        roots = parse_member_roots(self.base_dir, None)
        self.assertEqual(len(roots), 4)
        for mssv in EXPECTED_MEMBERS:
            self.assertIn(mssv, roots)
            self.assertEqual(roots[mssv], self.base_dir / mssv)

        custom_p = self.base_dir / "custom_24120010"
        custom_p.mkdir(parents=True, exist_ok=True)
        roots_custom = parse_member_roots(self.base_dir, [f"24120010={custom_p}"])
        self.assertEqual(roots_custom["24120010"], custom_p)

    def test_audit_missing_member_returns_incomplete(self):
        partial_roots = {}
        for mssv in EXPECTED_MEMBERS[:3]:
            m_dir = self.base_dir / mssv
            m_dir.mkdir(parents=True, exist_ok=True)
            partial_roots[mssv] = m_dir

        summary = audit_group_campaign(partial_roots)
        self.assertEqual(summary["status"], "INCOMPLETE")
        self.assertIn(EXPECTED_MEMBERS[3], summary["missing_members"])
        self.assertTrue(any(EXPECTED_MEMBERS[3] in d for d in summary["discrepancies"]))

    def test_audit_200_cohorts_full_pass(self):
        """Full 200 cohorts / 20,000 IDs integration test with real tarballs and benchmark artifacts."""
        import io
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            arc_dir = m_dir / "archives"
            rep_dir = m_dir / "reports"
            arc_dir.mkdir(parents=True, exist_ok=True)
            rep_dir.mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

            cohorts = member_cohort_range(mssv)
            for c in cohorts:
                pairs = cohort_pairs(c)
                arc_name = f"cohort_{c:04d}_corpus.tar.gz"
                arc_path = arc_dir / arc_name

                bio = io.BytesIO()
                with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                    for p_mssv, aid in pairs:
                        fkey = arxiv_id_to_folder_key(aid)
                        man_bytes = json.dumps({
                            "contract_version": 2, "arxiv_id": aid, "mssv": p_mssv,
                            "discovery_status": "found", "version_status": "resolved",
                            "metadata_status": "complete", "references_status": "complete",
                            "status": "complete", "versions": {},
                        }).encode("utf-8")
                        ti = tarfile.TarInfo(name=f"{fkey}/manifest.json")
                        ti.size = len(man_bytes)
                        tar.addfile(ti, io.BytesIO(man_bytes))
                        m_b = b'{"status": "complete"}'
                        ti_m = tarfile.TarInfo(name=f"{fkey}/metadata.json")
                        ti_m.size = len(m_b)
                        tar.addfile(ti_m, io.BytesIO(m_b))
                        r_b = b'{"status": "complete"}'
                        ti_r = tarfile.TarInfo(name=f"{fkey}/references.json")
                        ti_r.size = len(r_b)
                        tar.addfile(ti_r, io.BytesIO(r_b))

                tar_bytes = bio.getvalue()
                arc_path.write_bytes(tar_bytes)
                sha = hashlib.sha256(tar_bytes).hexdigest()
                (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
                (arc_dir / f"cohort_{c:04d}_corpus.manifest.json").write_text(json.dumps({
                    "cohort_number": c, "cohort_id": c, "archive_state": "m1_handoff_verified",
                    "archive_sha256": sha, "included_count": len(pairs),
                }))

            # Write real benchmark report
            exp_dir = rep_dir / f"exp-fast-{cohorts[0]:04d}-1762734000"
            exp_dir.mkdir(parents=True, exist_ok=True)
            res = {
                "status": "success", "elapsed_seconds": 1200.0, "average_ram_mb": 250.0, "peak_ram_mb": 310.0,
                "metrics_v2": {"ram": {"sample_count": 100, "average_bytes": 250 * 1024 * 1024, "sampled_peak_bytes": 310 * 1024 * 1024}}
            }
            (exp_dir / "resources.json").write_text(json.dumps(res))
            (exp_dir / "report.json").write_text(json.dumps({
                "attempt_ids": [f"att-{mssv}"], "configuration": {"s2": {}}, "timing_metrics": {"total_wall_seconds": 1200.0}, "resource_metrics": res
            }))

        summary = audit_group_campaign(member_roots, verify_archives=True)
        self.assertEqual(summary["status"], "PASS")
        self.assertTrue(summary["data_integrity_pass"])
        self.assertTrue(summary["coverage_20k_pass"])
        self.assertTrue(summary["group_handoff_pass"])
        self.assertEqual(summary["cohorts_evaluated"], 200)
        self.assertEqual(summary["found_m1_complete"], 20000)
        self.assertEqual(summary["unprocessed"], 0)
        self.assertEqual(summary["integrity_failures"], 0)
        self.assertEqual(summary["benchmark"]["status"], "PARTIAL_STAGE_MEASUREMENTS")
        self.assertEqual(summary["benchmark"]["weighted_ram_mb"], 250.0)
        self.assertEqual(summary["benchmark"]["sampled_peak_ram_mb"], 310.0)
        self.assertIsNone(summary["benchmark"]["total_active_seconds"])
        self.assertEqual(summary["benchmark"]["known_stage_active_seconds"], 4800.0)

        # Markdown report generation
        md = generate_markdown_report(summary)
        self.assertIn("# Milestone 1 Group Campaign Audit", md)
        self.assertIn("20,000", md)
        self.assertIn("**`PASS`**", md)

    def test_real_valid_tar_fixture_passes(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            (m_dir / "reports").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        # Build 1 real valid cohort for member 24120010 (cohort 1)
        arc_dir = member_roots["24120010"] / "archives"
        rep_dir = member_roots["24120010"] / "reports"
        _create_valid_cohort_archive(arc_dir, cohort_num=1, num_papers=100)
        _create_valid_benchmark_report(rep_dir, cohort_num=1, attempt_id="att-001", elapsed_seconds=60.0)

        summary = audit_group_campaign(member_roots, verify_archives=True)
        # Since other 199 cohorts are missing, status is INCOMPLETE, but cohort 1 itself is verified
        m10 = summary["members"]["24120010"]
        self.assertEqual(m10["archives_verified"], 1)
        self.assertEqual(m10["found_m1_complete"], 100)
        self.assertEqual(m10["archive_issues"], [])
        self.assertEqual(m10["benchmark"]["status"], "PARTIAL_STAGE_MEASUREMENTS")
        self.assertIsNone(m10["benchmark"]["total_active_seconds"])
        self.assertEqual(m10["benchmark"]["known_stage_active_seconds"], 60.0)

    def test_non_tar_dummy_archive_fails(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        arc_path = arc_dir / "cohort_0001_corpus.tar.gz"
        arc_path.write_bytes(b"not a tarball content at all")
        sha = hashlib.sha256(b"not a tarball content at all").hexdigest()
        (arc_dir / "cohort_0001_corpus.tar.gz.sha256").write_text(f"{sha}  cohort_0001_corpus.tar.gz\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(
            json.dumps({"archive_state": "m1_handoff_verified", "archive_sha256": sha, "included_count": 100})
        )

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("Cannot open or read archive" in iss for iss in m10_issues))
        self.assertGreater(summary["integrity_failures"], 0)
        self.assertNotEqual(summary["status"], "PASS")

    def test_modified_archive_with_old_sidecar_fails(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        arc_path, _ = _create_valid_cohort_archive(arc_dir, cohort_num=1, num_papers=5)

        # Tamper archive bytes by appending 1 byte, but leave .sha256 and manifest intact
        with open(arc_path, "ab") as f:
            f.write(b"\x00")

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("does not match sidecar" in iss for iss in m10_issues))
        self.assertGreater(summary["integrity_failures"], 0)
        self.assertNotEqual(summary["status"], "PASS")

    def test_missing_archive_with_manifest_fails(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(
            json.dumps({"archive_state": "m1_handoff_verified", "archive_sha256": "fake", "included_count": 100})
        )
        # Note: cohort_0001_corpus.tar.gz does NOT exist

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("Missing archive tarball" in iss for iss in m10_issues))
        self.assertGreater(summary["integrity_failures"], 0)
        self.assertNotEqual(summary["status"], "PASS")

    def test_foreign_folder_in_archive_fails(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        arc_path = arc_dir / "cohort_0001_corpus.tar.gz"

        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            foreign_dir = temp_root / "foreign-99999"
            foreign_dir.mkdir()
            (foreign_dir / "manifest.json").write_text("{}", encoding="utf-8")
            with tarfile.open(arc_path, "w:gz") as tar:
                tar.add(foreign_dir, arcname="foreign-99999")

        sha = hashlib.sha256(arc_path.read_bytes()).hexdigest()
        (arc_dir / "cohort_0001_corpus.tar.gz.sha256").write_text(f"{sha}  cohort_0001_corpus.tar.gz\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(
            json.dumps({"archive_state": "m1_handoff_verified", "archive_sha256": sha, "included_count": 100})
        )

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("Foreign folder" in iss for iss in m10_issues))
        self.assertGreater(summary["integrity_failures"], 0)

    def test_checksum_mismatch_in_archived_file_fails(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        # Create cohort 1 with tampered file checksum in manifest
        pairs = cohort_pairs(1)[:2]
        arc_dir = member_roots["24120010"] / "archives"
        arc_name = "cohort_0001_corpus.tar.gz"
        arc_path = arc_dir / arc_name

        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            for mssv, aid in pairs:
                fkey = arxiv_id_to_folder_key(aid)
                pdir = temp_root / fkey
                vdir = pdir / "v1"
                vdir.mkdir(parents=True, exist_ok=True)
                (vdir / "main.tex").write_bytes(b"content")

                (pdir / "metadata.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
                (pdir / "references.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
                (pdir / "manifest.json").write_text(
                    json.dumps({
                        "arxiv_id": aid, "mssv": mssv, "discovery_status": "found",
                        "version_status": "resolved", "metadata_status": "complete",
                        "references_status": "complete", "status": "complete",
                        "versions": {
                            "v1": {
                                "files": ["main.tex"],
                                "file_checksums": {"main.tex": "incorrect_hash"},
                            }
                        },
                    }),
                    encoding="utf-8",
                )
            with tarfile.open(arc_path, "w:gz") as tar:
                for item in sorted(temp_root.iterdir()):
                    tar.add(item, arcname=item.name)

        sha = hashlib.sha256(arc_path.read_bytes()).hexdigest()
        (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(
            json.dumps({"archive_state": "m1_handoff_verified", "archive_sha256": sha, "included_count": len(pairs)})
        )

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("Checksum mismatch for" in iss for iss in m10_issues))
        self.assertGreater(summary["members"]["24120010"]["integrity_failures"], 0)

    def test_incomplete_benchmark_evidence_not_reported_as_zero_or_pass(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            (m_dir / "reports").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        summary = audit_group_campaign(member_roots, verify_archives=False)
        self.assertEqual(summary["benchmark"]["status"], "NOT_COLLECTED")
        self.assertIsNone(summary["benchmark"]["total_active_seconds"])
        self.assertIsNone(summary["benchmark"]["weighted_ram_mb"])
        self.assertIsNone(summary["benchmark"]["sampled_peak_ram_mb"])
        self.assertNotEqual(summary["status"], "PASS")

    def test_benchmark_aggregation_deduplicates_attempt_intervals(self):
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            (m_dir / "reports").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        rep_dir = member_roots["24120010"] / "reports"
        # Two directories reporting the SAME attempt_id (e.g. duplicate or resume rerun)
        _create_valid_benchmark_report(rep_dir, cohort_num=1, attempt_id="att-shared", elapsed_seconds=50.0, average_ram_mb=200.0, sample_count=100)
        # Create second dir with same attempt_id
        exp2 = rep_dir / "exp-fast-0001-1762734999"
        exp2.mkdir()
        (exp2 / "resources.json").write_text(json.dumps({
            "elapsed_seconds": 50.0, "average_ram_mb": 200.0, "metrics_v2": {"ram": {"sample_count": 100}}
        }))
        (exp2 / "report.json").write_text(json.dumps({
            "attempt_ids": ["att-shared"], "timing_metrics": {"total_wall_seconds": 50.0}
        }))

        # Member 2 has unique attempt
        rep2_dir = member_roots["24120035"] / "reports"
        _create_valid_benchmark_report(rep2_dir, cohort_num=51, attempt_id="att-51", elapsed_seconds=70.0, average_ram_mb=400.0, sample_count=300)

        summary = audit_group_campaign(member_roots, verify_archives=False)
        bench = summary["benchmark"]
        self.assertEqual(bench["status"], "PARTIAL_STAGE_MEASUREMENTS")
        # Att-shared counted once (50.0s) + att-51 (70.0s) = 120.0s
        self.assertIsNone(bench["total_active_seconds"])
        self.assertEqual(bench["known_stage_active_seconds"], 120.0)
        # Total samples: 100 + 300 = 400. Weighted RAM: (100*200 + 300*400) / 400 = (20000 + 120000) / 400 = 350.0 MB
        self.assertEqual(bench["weighted_ram_mb"], 350.0)

    def test_cli_invocation_matches_member_guide(self):
        """Task 4: Test the exact CLI command from docs/M1_COLAB_MEMBER_GUIDE.md."""
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            (m_dir / "reports").mkdir(parents=True, exist_ok=True)

        out_json = self.base_dir / "test_out.json"
        out_md = self.base_dir / "test_out.md"

        cmd_args = [
            "--base-dir", str(self.base_dir),
            "--output", str(out_json),
            "--report", str(out_md),
            "--verify-archives",
        ]
        exit_code = main(cmd_args)
        # Since cohorts are not complete, exit_code is 1 (fail-closed)
        self.assertEqual(exit_code, 1)
        self.assertTrue(out_json.is_file(), "JSON report must be generated")
        self.assertTrue(out_md.is_file(), "Markdown report must be generated")
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["expected_members"], EXPECTED_MEMBERS)

    def test_cohort_with_unverified_archive_state_fails(self):
        """BUG-003: Cohort with archive_state != 'm1_handoff_verified' must not pass."""
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        _create_valid_cohort_archive(arc_dir, cohort_num=1, num_papers=5)

        # Alter manifest archive_state to unverified checkpoint_partial
        m_path = arc_dir / "cohort_0001_corpus.manifest.json"
        data = json.loads(m_path.read_text(encoding="utf-8"))
        data["archive_state"] = "checkpoint_partial"
        m_path.write_text(json.dumps(data), encoding="utf-8")

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("archive_state" in iss for iss in m10_issues), f"Expected archive_state error, got {m10_issues}")
        self.assertGreater(summary["integrity_failures"], 0)

    def test_manifest_cohort_number_mismatch_fails(self):
        """BUG-003: Manifest with mismatched cohort_number / cohort_id must fail."""
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        _create_valid_cohort_archive(arc_dir, cohort_num=1, num_papers=5)

        m_path = arc_dir / "cohort_0001_corpus.manifest.json"
        data = json.loads(m_path.read_text(encoding="utf-8"))
        data["cohort_id"] = 999
        data["cohort_number"] = 999
        m_path.write_text(json.dumps(data), encoding="utf-8")

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("mismatch" in iss.lower() and "cohort" in iss.lower() for iss in m10_issues), f"Expected cohort mismatch error, got {m10_issues}")

    def test_missing_metadata_json_inside_tar_fails(self):
        """BUG-003: Paper claiming complete metadata but missing metadata.json in tar must fail."""
        import io
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        pairs = cohort_pairs(1)[:2]
        arc_name = "cohort_0001_corpus.tar.gz"
        arc_path = arc_dir / arc_name

        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            for p_mssv, aid in pairs:
                fkey = arxiv_id_to_folder_key(aid)
                # Omit metadata.json on purpose!
                man_bytes = json.dumps({
                    "contract_version": 2, "arxiv_id": aid, "mssv": p_mssv,
                    "discovery_status": "found", "version_status": "resolved",
                    "metadata_status": "complete", "references_status": "complete",
                    "status": "complete", "versions": {},
                }).encode("utf-8")
                ti = tarfile.TarInfo(name=f"{fkey}/manifest.json")
                ti.size = len(man_bytes)
                tar.addfile(ti, io.BytesIO(man_bytes))
                # Add references.json
                ref_bytes = b'{"status": "complete"}'
                ti_ref = tarfile.TarInfo(name=f"{fkey}/references.json")
                ti_ref.size = len(ref_bytes)
                tar.addfile(ti_ref, io.BytesIO(ref_bytes))

        tar_bytes = bio.getvalue()
        arc_path.write_bytes(tar_bytes)
        sha = hashlib.sha256(tar_bytes).hexdigest()
        (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(json.dumps({
            "cohort_number": 1, "cohort_id": 1, "archive_state": "m1_handoff_verified",
            "archive_sha256": sha, "included_count": len(pairs),
        }))

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("missing metadata.json" in iss.lower() for iss in m10_issues), f"Expected missing metadata.json error, got {m10_issues}")
        self.assertGreater(summary["integrity_failures"], 0)

    def test_missing_references_json_inside_tar_fails(self):
        """BUG-003: Paper claiming complete references but missing references.json in tar must fail."""
        import io
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        pairs = cohort_pairs(1)[:2]
        arc_name = "cohort_0001_corpus.tar.gz"
        arc_path = arc_dir / arc_name

        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            for p_mssv, aid in pairs:
                fkey = arxiv_id_to_folder_key(aid)
                # Omit references.json on purpose!
                man_bytes = json.dumps({
                    "contract_version": 2, "arxiv_id": aid, "mssv": p_mssv,
                    "discovery_status": "found", "version_status": "resolved",
                    "metadata_status": "complete", "references_status": "complete",
                    "status": "complete", "versions": {},
                }).encode("utf-8")
                ti = tarfile.TarInfo(name=f"{fkey}/manifest.json")
                ti.size = len(man_bytes)
                tar.addfile(ti, io.BytesIO(man_bytes))
                # Add metadata.json
                meta_bytes = b'{"status": "complete"}'
                ti_meta = tarfile.TarInfo(name=f"{fkey}/metadata.json")
                ti_meta.size = len(meta_bytes)
                tar.addfile(ti_meta, io.BytesIO(meta_bytes))

        tar_bytes = bio.getvalue()
        arc_path.write_bytes(tar_bytes)
        sha = hashlib.sha256(tar_bytes).hexdigest()
        (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(json.dumps({
            "cohort_number": 1, "cohort_id": 1, "archive_state": "m1_handoff_verified",
            "archive_sha256": sha, "included_count": len(pairs),
        }))

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("missing references.json" in iss.lower() for iss in m10_issues), f"Expected missing references.json error, got {m10_issues}")
        self.assertGreater(summary["integrity_failures"], 0)

    def test_unmanaged_file_inside_paper_tar_fails(self):
        """BUG-003: Tarball containing extraneous unmanaged file inside paper directory must fail."""
        import io
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        pairs = cohort_pairs(1)[:2]
        arc_name = "cohort_0001_corpus.tar.gz"
        arc_path = arc_dir / arc_name

        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            for p_mssv, aid in pairs:
                fkey = arxiv_id_to_folder_key(aid)
                man_bytes = json.dumps({
                    "contract_version": 2, "arxiv_id": aid, "mssv": p_mssv,
                    "discovery_status": "found", "version_status": "resolved",
                    "metadata_status": "complete", "references_status": "complete",
                    "status": "complete", "versions": {},
                }).encode("utf-8")
                ti = tarfile.TarInfo(name=f"{fkey}/manifest.json")
                ti.size = len(man_bytes)
                tar.addfile(ti, io.BytesIO(man_bytes))
                m_b = b'{"status": "complete"}'
                ti_meta = tarfile.TarInfo(name=f"{fkey}/metadata.json")
                ti_meta.size = len(m_b)
                tar.addfile(ti_meta, io.BytesIO(m_b))
                r_b = b'{"status": "complete"}'
                ti_ref = tarfile.TarInfo(name=f"{fkey}/references.json")
                ti_ref.size = len(r_b)
                tar.addfile(ti_ref, io.BytesIO(r_b))

                # Add unmanaged foreign file!
                unmanaged_bytes = b"echo hack"
                ti_bad = tarfile.TarInfo(name=f"{fkey}/unmanaged.sh")
                ti_bad.size = len(unmanaged_bytes)
                tar.addfile(ti_bad, io.BytesIO(unmanaged_bytes))

        tar_bytes = bio.getvalue()
        arc_path.write_bytes(tar_bytes)
        sha = hashlib.sha256(tar_bytes).hexdigest()
        (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(json.dumps({
            "cohort_number": 1, "cohort_id": 1, "archive_state": "m1_handoff_verified",
            "archive_sha256": sha, "included_count": len(pairs),
        }))

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("unmanaged" in iss.lower() for iss in m10_issues), f"Expected unmanaged file error, got {m10_issues}")
        self.assertGreater(summary["integrity_failures"], 0)

    def test_duplicate_paper_id_across_cohorts_fails(self):
        """BUG-003: Aggregator detects duplicate paper IDs appearing across different cohorts."""
        import io
        from unittest.mock import patch
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        # Cohort 1 and Cohort 2 both package paper from cohort 1!
        shared_pair = cohort_pairs(1)[0]  # ('24120010', '2508.07872')

        for c in (1, 2):
            arc_name = f"cohort_{c:04d}_corpus.tar.gz"
            arc_path = arc_dir / arc_name
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w:gz") as tar:
                # include shared_pair in both cohorts!
                fkey = arxiv_id_to_folder_key(shared_pair[1])
                man_bytes = json.dumps({
                    "contract_version": 2, "arxiv_id": shared_pair[1], "mssv": shared_pair[0],
                    "discovery_status": "found", "version_status": "resolved",
                    "metadata_status": "complete", "references_status": "complete",
                    "status": "complete", "versions": {},
                }).encode("utf-8")
                ti = tarfile.TarInfo(name=f"{fkey}/manifest.json")
                ti.size = len(man_bytes)
                tar.addfile(ti, io.BytesIO(man_bytes))
                m_b = b'{"status": "complete"}'
                ti_m = tarfile.TarInfo(name=f"{fkey}/metadata.json")
                ti_m.size = len(m_b)
                tar.addfile(ti_m, io.BytesIO(m_b))
                r_b = b'{"status": "complete"}'
                ti_r = tarfile.TarInfo(name=f"{fkey}/references.json")
                ti_r.size = len(r_b)
                tar.addfile(ti_r, io.BytesIO(r_b))

            tar_bytes = bio.getvalue()
            arc_path.write_bytes(tar_bytes)
            sha = hashlib.sha256(tar_bytes).hexdigest()
            (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
            (arc_dir / f"cohort_{c:04d}_corpus.manifest.json").write_text(json.dumps({
                "cohort_number": c, "cohort_id": c, "archive_state": "m1_handoff_verified",
                "archive_sha256": sha, "included_count": 1,
            }))

        from scripts import aggregate_m1_group
        orig_pairs = aggregate_m1_group.cohort_pairs
        def patched_pairs(c):
            if c in (1, 2):
                return [shared_pair]
            return orig_pairs(c)

        with patch("scripts.aggregate_m1_group.cohort_pairs", side_effect=patched_pairs):
            summary = audit_group_campaign(member_roots, verify_archives=True)
            disc = summary["discrepancies"]
            self.assertTrue(any("duplicate" in d.lower() and shared_pair[1] in d for d in disc), f"Expected duplicate ID error, got {disc}")
            self.assertGreater(summary["integrity_failures"], 0)

    def test_foreign_cohort_archive_in_member_folder_fails(self):
        """BUG-003: Member directory containing cohort archive outside assigned range fails."""
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        # Member 24120010 is assigned 1..50. Inject cohort 99!
        (arc_dir / "cohort_0099_corpus.tar.gz").write_bytes(b"dummy")

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("foreign cohort" in iss.lower() and "99" in iss for iss in m10_issues), f"Expected foreign cohort error, got {m10_issues}")

    def test_symlink_inside_tar_fails(self):
        """BUG-003: Tarball containing symlinks must be rejected as UNSAFE_MEMBER."""
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        arc_dir = member_roots["24120010"] / "archives"
        arc_name = "cohort_0001_corpus.tar.gz"
        arc_path = arc_dir / arc_name

        import io
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as tar:
            fkey = arxiv_id_to_folder_key("2508.07872")
            ti = tarfile.TarInfo(name=f"{fkey}/symlink_file")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)

        tar_bytes = bio.getvalue()
        arc_path.write_bytes(tar_bytes)
        sha = hashlib.sha256(tar_bytes).hexdigest()
        (arc_dir / f"{arc_name}.sha256").write_text(f"{sha}  {arc_name}\n")
        (arc_dir / "cohort_0001_corpus.manifest.json").write_text(json.dumps({
            "cohort_number": 1, "cohort_id": 1, "archive_state": "m1_handoff_verified",
            "archive_sha256": sha, "included_count": 1,
        }))

        summary = audit_group_campaign(member_roots, verify_archives=True)
        m10_issues = summary["members"]["24120010"]["archive_issues"]
        self.assertTrue(any("unsafe" in iss.lower() or "disallowed" in iss.lower() for iss in m10_issues), f"Expected unsafe member error, got {m10_issues}")

    def test_distinct_three_pass_flags_semantics(self):
        """BUG-003: Summary provides 3 separate pass flags: data_integrity, coverage_20k, group_handoff."""
        member_roots = {}
        for mssv in EXPECTED_MEMBERS:
            m_dir = self.base_dir / mssv
            (m_dir / "archives").mkdir(parents=True, exist_ok=True)
            (m_dir / "reports").mkdir(parents=True, exist_ok=True)
            member_roots[mssv] = m_dir

        # Partial campaign (missing cohorts)
        summary = audit_group_campaign(member_roots, verify_archives=True)
        self.assertIn("data_integrity_pass", summary)
        self.assertIn("coverage_20k_pass", summary)
        self.assertIn("group_handoff_pass", summary)
        self.assertFalse(summary["coverage_20k_pass"])
        self.assertFalse(summary["group_handoff_pass"])


if __name__ == "__main__":
    unittest.main()
