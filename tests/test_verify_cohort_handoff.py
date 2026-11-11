import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from src.fast_production import FastProductionRunner, cohort_pairs, verify_cohort_handoff
from src.storage import arxiv_id_to_folder_key


class TestVerifyCohortHandoffEdgeCases(unittest.TestCase):
    """BUG-004: Regression tests for verify_cohort_handoff edge cases, security, and contracts."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local_root = Path(self.temp_dir.name) / "local"
        self.drive_root = Path(self.temp_dir.name) / "drive"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.drive_root.mkdir(parents=True, exist_ok=True)
        self.runner = FastProductionRunner(self.local_root, self.drive_root)
        self.runner.archives_dir.mkdir(parents=True, exist_ok=True)
        self.pairs = cohort_pairs(1)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_archive(
        self,
        cohort_num=1,
        paper_overrides=None,
        archive_state="handoff_verified",
        manifest_cohort_num=1,
        manifest_included_count=None,
        extra_tar_members=None,
        truncate_bytes=None,
        corrupt_paper_manifest=None,
    ):
        """Helper to create a cohort archive with customizable papers and sidecars."""
        paper_overrides = paper_overrides or {}
        extra_tar_members = extra_tar_members or []
        archive_path = self.runner.archives_dir / f"cohort_{cohort_num:04d}_corpus.tar.gz"
        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        manifest_file = archive_path.with_name(f"cohort_{cohort_num:04d}_corpus.manifest.json")

        tex_bytes = b"\\documentclass{article}\\begin{document}Content\\end{document}"
        tex_hash = hashlib.sha256(tex_bytes).hexdigest()

        with tarfile.open(archive_path, "w:gz") as tar:
            for mssv, aid in self.pairs:
                f_key = arxiv_id_to_folder_key(aid)
                override = paper_overrides.get(aid)

                if override == "OMIT_FOLDER":
                    continue

                if override and "raw_manifest" in override:
                    p_man_bytes = override["raw_manifest"]
                else:
                    man_dict = {
                        "contract_version": 2,
                        "arxiv_id": aid,
                        "mssv": mssv,
                        "status": "complete",
                        "discovery_status": "found",
                        "version_status": "resolved",
                        "metadata_status": "complete",
                        "references_status": "complete",
                        "versions": {
                            "v1": {
                                "versioned_id": f"{aid}v1",
                                "status": "success",
                                "files": ["main.tex"],
                                "file_checksums": {"main.tex": tex_hash},
                            }
                        },
                    }
                    if override and "manifest_updates" in override:
                        man_dict.update(override["manifest_updates"])
                    p_man_bytes = json.dumps(man_dict).encode("utf-8")

                if corrupt_paper_manifest == aid:
                    p_man_bytes = b"{{{corrupted json"

                ti = tarfile.TarInfo(f"{f_key}/manifest.json")
                ti.size = len(p_man_bytes)
                tar.addfile(ti, io.BytesIO(p_man_bytes))

                # Handle not_found papers: only manifest.json in tar
                if override and override.get("discovery_status") == "not_found":
                    continue

                # Add metadata.json unless omitted
                if not (override and override.get("omit_metadata")):
                    meta_b = b'{"status": "complete"}'
                    ti_m = tarfile.TarInfo(f"{f_key}/metadata.json")
                    ti_m.size = len(meta_b)
                    tar.addfile(ti_m, io.BytesIO(meta_b))

                # Add references.json unless omitted
                if not (override and override.get("omit_references")):
                    ref_b = b'{"status": "complete"}'
                    ti_r = tarfile.TarInfo(f"{f_key}/references.json")
                    ti_r.size = len(ref_b)
                    tar.addfile(ti_r, io.BytesIO(ref_b))

                # Add version files unless custom versions provided
                if override and "custom_files" in override:
                    for rel_p, content in override["custom_files"].items():
                        ti_f = tarfile.TarInfo(f"{f_key}/{rel_p}")
                        ti_f.size = len(content)
                        tar.addfile(ti_f, io.BytesIO(content))
                elif not (override and override.get("omit_files")):
                    ti_tex = tarfile.TarInfo(f"{f_key}/v1/main.tex")
                    ti_tex.size = len(tex_bytes)
                    tar.addfile(ti_tex, io.BytesIO(tex_bytes))

            for ti_extra, extra_bytes in extra_tar_members:
                tar.addfile(ti_extra, io.BytesIO(extra_bytes) if extra_bytes else None)

        if truncate_bytes is not None:
            raw = archive_path.read_bytes()
            archive_path.write_bytes(raw[:truncate_bytes])

        actual_sha = self.runner._compute_sha256(archive_path)
        sha_file.write_text(f"{actual_sha}  {archive_path.name}\n", encoding="utf-8")

        included_count = (
            manifest_included_count
            if manifest_included_count is not None
            else len(self.pairs)
        )
        manifest_payload = {
            "cohort_number": manifest_cohort_num,
            "archive_filename": archive_path.name,
            "archive_sha256": actual_sha,
            "included_count": included_count,
            "included_ids": [aid for _, aid in self.pairs],
            "archive_state": archive_state,
        }
        manifest_file.write_text(json.dumps(manifest_payload), encoding="utf-8")
        return archive_path, sha_file, manifest_file, actual_sha

    def test_nominal_archive_passes_verification(self):
        """Nominal valid cohort passes all checks."""
        self._build_archive()
        res = verify_cohort_handoff(self.runner, 1)
        self.assertTrue(res["verified"])
        self.assertEqual(res["cohort_number"], 1)
        self.assertEqual(res["inspected_papers"], 100)

    def test_valid_no_tex_source_bib_only_passes(self):
        """Paper with status complete_with_no_tex and bib-only files passes."""
        aid_target = self.pairs[0][1]
        bib_bytes = b"@article{key, title={Paper}, author={Author}}"
        bib_hash = hashlib.sha256(bib_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_no_tex",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "no_tex_source",
                            "files": ["ref.bib"],
                            "file_checksums": {"ref.bib": bib_hash},
                            "kept_bytes": len(bib_bytes),
                        }
                    },
                },
                "custom_files": {"v1/ref.bib": bib_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        res = verify_cohort_handoff(self.runner, 1)
        self.assertTrue(res["verified"])

    def test_valid_no_tex_source_empty_passes(self):
        """Paper with status complete_with_no_tex and empty files passes."""
        aid_target = self.pairs[0][1]
        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_no_tex",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "no_tex_source",
                            "files": [],
                            "file_checksums": {},
                            "kept_bytes": 0,
                        }
                    },
                },
                "omit_files": True,
            }
        }
        self._build_archive(paper_overrides=overrides)
        res = verify_cohort_handoff(self.runner, 1)
        self.assertTrue(res["verified"])

    def test_invalid_no_tex_source_containing_tex_fails(self):
        """no_tex_source version claiming .tex file must fail."""
        aid_target = self.pairs[0][1]
        tex_bytes = b"\\documentclass{article}"
        tex_hash = hashlib.sha256(tex_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_no_tex",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "no_tex_source",
                            "files": ["main.tex"],
                            "file_checksums": {"main.tex": tex_hash},
                        }
                    },
                },
                "custom_files": {"v1/main.tex": tex_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("contains .tex file", str(ctx.exception))

    def test_invalid_no_tex_source_containing_non_bib_fails(self):
        """no_tex_source version containing arbitrary non-.bib file must fail."""
        aid_target = self.pairs[0][1]
        py_bytes = b"print('hello')"
        py_hash = hashlib.sha256(py_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_no_tex",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "no_tex_source",
                            "files": ["script.py"],
                            "file_checksums": {"script.py": py_hash},
                        }
                    },
                },
                "custom_files": {"v1/script.py": py_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("contains non-bib file", str(ctx.exception))

    def test_empty_no_tex_source_with_nonzero_kept_bytes_fails(self):
        """empty no_tex_source with kept_bytes != 0 must fail."""
        aid_target = self.pairs[0][1]
        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_no_tex",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "no_tex_source",
                            "files": [],
                            "file_checksums": {},
                            "kept_bytes": 100,
                        }
                    },
                },
                "omit_files": True,
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("kept_bytes must be 0", str(ctx.exception))

    def test_complete_status_without_any_tex_files_fails(self):
        """status 'complete' requires at least one .tex file across versions."""
        aid_target = self.pairs[0][1]
        bbl_bytes = b"bbl data"
        bbl_hash = hashlib.sha256(bbl_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "success",
                            "files": ["main.bbl"],
                            "file_checksums": {"main.bbl": bbl_hash},
                        }
                    },
                },
                "custom_files": {"v1/main.bbl": bbl_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("has no .tex files", str(ctx.exception))

    def test_missing_metadata_json_in_tar_fails(self):
        """Missing metadata.json inside archive raises ValueError."""
        aid_target = self.pairs[0][1]
        overrides = {aid_target: {"omit_metadata": True}}
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("missing metadata.json inside archive", str(ctx.exception))

    def test_missing_references_json_in_tar_fails(self):
        """Missing references.json inside archive raises ValueError."""
        aid_target = self.pairs[0][1]
        overrides = {aid_target: {"omit_references": True}}
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("missing references.json inside archive", str(ctx.exception))

    def test_missing_sha256_sidecar_fails(self):
        """Missing .sha256 file raises ValueError."""
        _, sha_file, _, _ = self._build_archive()
        sha_file.unlink()
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Missing checksum sidecar file", str(ctx.exception))

    def test_mismatched_sha256_sidecar_fails(self):
        """Mismatched .sha256 content raises ValueError."""
        _, sha_file, _, _ = self._build_archive()
        sha_file.write_text("0000000000000000000000000000000000000000000000000000000000000000  bad.tar.gz\n")
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_mismatched_manifest_sha256_fails(self):
        """Accounting manifest with wrong archive_sha256 raises ValueError."""
        _, _, mf_file, _ = self._build_archive()
        mf = json.loads(mf_file.read_text(encoding="utf-8"))
        mf["archive_sha256"] = "deadbeef" * 8
        mf_file.write_text(json.dumps(mf), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Accounting manifest sha256 mismatch", str(ctx.exception))

    def test_mismatched_file_checksum_inside_archive_fails(self):
        """File in tar with bytes differing from manifest checksum raises ValueError."""
        aid_target = self.pairs[0][1]
        real_bytes = b"real content"
        wrong_hash = hashlib.sha256(b"tampered content").hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "success",
                            "files": ["main.tex"],
                            "file_checksums": {"main.tex": wrong_hash},
                        }
                    }
                },
                "custom_files": {"v1/main.tex": real_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Checksum mismatch for", str(ctx.exception))

    def test_corrupted_or_truncated_tar_fails(self):
        """Truncated tar.gz raises ValueError."""
        self._build_archive(truncate_bytes=64)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Corrupted or unreadable archive", str(ctx.exception))

    def test_corrupted_manifest_json_in_tar_fails(self):
        """Corrupted manifest.json syntax inside tar raises ValueError."""
        aid_target = self.pairs[0][1]
        self._build_archive(corrupt_paper_manifest=aid_target)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Corrupted manifest.json in archive", str(ctx.exception))

    def test_unmanaged_file_in_paper_dir_fails(self):
        """Unmanaged file inside a paper folder raises ValueError."""
        aid_target = self.pairs[0][1]
        f_key = arxiv_id_to_folder_key(aid_target)
        ti_extra = tarfile.TarInfo(f"{f_key}/unmanaged_exploit.sh")
        extra_bytes = b"echo hack"
        ti_extra.size = len(extra_bytes)

        self._build_archive(extra_tar_members=[(ti_extra, extra_bytes)])
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("contains unmanaged file outside manifest", str(ctx.exception))

    def test_foreign_folder_in_archive_fails(self):
        """Archive member belonging to another paper or arbitrary folder raises ValueError."""
        ti_extra = tarfile.TarInfo("foreign_paper_folder/manifest.json")
        extra_bytes = b'{"status": "complete"}'
        ti_extra.size = len(extra_bytes)

        self._build_archive(extra_tar_members=[(ti_extra, extra_bytes)])
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Foreign folder in cohort archive", str(ctx.exception))

    def test_symlink_member_in_archive_fails(self):
        """Symlink member in archive raises ValueError."""
        ti_sym = tarfile.TarInfo("symlink_member")
        ti_sym.type = tarfile.SYMTYPE
        ti_sym.linkname = "/etc/passwd"

        self._build_archive(extra_tar_members=[(ti_sym, None)])
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Special files not allowed in backup", str(ctx.exception))

    def test_path_traversal_member_in_archive_fails(self):
        """Path traversal member (..) raises ValueError."""
        ti_trav = tarfile.TarInfo("../../traversal.txt")
        b = b"traversal"
        ti_trav.size = len(b)

        self._build_archive(extra_tar_members=[(ti_trav, b)])
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Unsafe archive member", str(ctx.exception))

    def test_cohort_number_mismatch_fails(self):
        """Manifest with cohort_number != 1 for cohort 1 archive raises ValueError."""
        self._build_archive(manifest_cohort_num=2)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("Cohort number mismatch in manifest", str(ctx.exception))

    def test_invalid_archive_state_fails(self):
        """Accounting manifest with state != handoff_verified raises ValueError."""
        self._build_archive(archive_state="checkpoint_partial")
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("archive_state is 'checkpoint_partial'", str(ctx.exception))

    def test_included_count_mismatch_fails(self):
        """Accounting manifest with included_count != 100 raises ValueError."""
        self._build_archive(manifest_included_count=99)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("included_count is 99, expected 100", str(ctx.exception))

    def test_confirmed_not_found_paper_passes(self):
        """Confirmed not_found paper with clean contract passes."""
        aid_target = self.pairs[0][1]
        overrides = {
            aid_target: {
                "discovery_status": "not_found",
                "manifest_updates": {
                    "status": "not_found",
                    "discovery_status": "not_found",
                    "version_status": "not_found",
                    "metadata_status": "not_found",
                    "references_status": "not_found",
                    "versions": {},
                },
                "omit_metadata": True,
                "omit_references": True,
                "omit_files": True,
            }
        }
        self._build_archive(paper_overrides=overrides)
        res = verify_cohort_handoff(self.runner, 1)
        self.assertTrue(res["verified"])

    def test_confirmed_not_found_with_extraneous_file_fails(self):
        """not_found paper containing extraneous file raises ValueError."""
        aid_target = self.pairs[0][1]
        f_key = arxiv_id_to_folder_key(aid_target)
        overrides = {
            aid_target: {
                "discovery_status": "not_found",
                "manifest_updates": {
                    "status": "not_found",
                    "discovery_status": "not_found",
                    "version_status": "not_found",
                    "metadata_status": "not_found",
                    "references_status": "not_found",
                    "versions": {},
                },
                "omit_metadata": True,
                "omit_references": True,
                "omit_files": True,
            }
        }
        ti_extra = tarfile.TarInfo(f"{f_key}/extraneous.txt")
        b = b"junk"
        ti_extra.size = len(b)
        self._build_archive(paper_overrides=overrides, extra_tar_members=[(ti_extra, b)])
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("not_found Paper", str(ctx.exception))
        self.assertIn("contains unmanaged file", str(ctx.exception))

    def test_idempotency_returns_consistent_result_without_side_effects(self):
        """Sequential invocations return identical results and do not mutate filesystem."""
        arc_p, sha_p, man_p, _ = self._build_archive()
        arc_stat_before = arc_p.stat().st_mtime_ns
        sha_stat_before = sha_p.stat().st_mtime_ns
        man_stat_before = man_p.stat().st_mtime_ns

        res1 = verify_cohort_handoff(self.runner, 1)
        res2 = verify_cohort_handoff(self.runner, 1)

        self.assertEqual(res1, res2)
        self.assertTrue(res1["verified"])
        self.assertEqual(arc_p.stat().st_mtime_ns, arc_stat_before)
        self.assertEqual(sha_p.stat().st_mtime_ns, sha_stat_before)
        self.assertEqual(man_p.stat().st_mtime_ns, man_stat_before)

    def test_valid_withdrawn_version_multi_version_passes(self):
        """Multi-version paper with v1 success and v2 confirmed withdrawn passes with complete_with_withdrawn."""
        aid_target = self.pairs[0][1]
        tex_bytes = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
        tex_hash = hashlib.sha256(tex_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_withdrawn",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "success",
                            "files": ["main.tex"],
                            "file_checksums": {"main.tex": tex_hash},
                            "kept_bytes": len(tex_bytes),
                        },
                        "v2": {
                            "versioned_id": f"{aid_target}v2",
                            "status": "withdrawn_version",
                            "files": [],
                            "file_checksums": {},
                            "kept_bytes": 0,
                            "withdrawal_evidence": {
                                "classification": "withdrawn_version",
                                "original_http_status": 404,
                                "evidence_source": "arxiv_metadata",
                                "details": {
                                    "arxiv_id": aid_target,
                                    "versioned_id": f"{aid_target}v2",
                                    "official_url": f"https://arxiv.org/abs/{aid_target}v2",
                                    "withdrawal_reason": "This paper has been withdrawn by the author.",
                                },
                            },
                        },
                    },
                },
                "custom_files": {"v1/main.tex": tex_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        res = verify_cohort_handoff(self.runner, 1)
        self.assertTrue(res["verified"])

    def test_withdrawn_version_with_nonzero_kept_bytes_fails(self):
        """withdrawn_version with kept_bytes != 0 must fail."""
        aid_target = self.pairs[0][1]
        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete_with_withdrawn",
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "withdrawn_version",
                            "files": [],
                            "file_checksums": {},
                            "kept_bytes": 100,
                        }
                    },
                },
                "omit_files": True,
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("kept_bytes must be 0", str(ctx.exception))

    def test_complete_status_with_withdrawn_version_fails(self):
        """status 'complete' is rejected when a version is withdrawn_version (must be complete_with_withdrawn)."""
        aid_target = self.pairs[0][1]
        tex_bytes = b"\\documentclass{article}\\begin{document}Hi\\end{document}"
        tex_hash = hashlib.sha256(tex_bytes).hexdigest()

        overrides = {
            aid_target: {
                "manifest_updates": {
                    "status": "complete",  # Contradicts v2 withdrawn
                    "versions": {
                        "v1": {
                            "versioned_id": f"{aid_target}v1",
                            "status": "success",
                            "files": ["main.tex"],
                            "file_checksums": {"main.tex": tex_hash},
                            "kept_bytes": len(tex_bytes),
                        },
                        "v2": {
                            "versioned_id": f"{aid_target}v2",
                            "status": "withdrawn_version",
                            "files": [],
                            "file_checksums": {},
                            "kept_bytes": 0,
                        },
                    },
                },
                "custom_files": {"v1/main.tex": tex_bytes},
            }
        }
        self._build_archive(paper_overrides=overrides)
        with self.assertRaises(ValueError) as ctx:
            verify_cohort_handoff(self.runner, 1)
        self.assertIn("has withdrawn_version versions", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
