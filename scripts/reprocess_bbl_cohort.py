"""Reprocess a verified cohort archive under the BBL exception policy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile

from src.bbl_handling import BBL_POLICY_VERSION, apply_bbl_source_policy
from src.fast_production import (
    FastProductionRunner,
    audit_cohort_archive_payload,
    cohort_pairs,
    verify_cohort_handoff,
)
from src.storage import _atomic_json, arxiv_id_to_folder_key
from src.storage_integrity import audit_cohort_integrity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_verified_archive(archive_path: Path, corpus_root: Path, cohort: int) -> None:
    pairs = cohort_pairs(cohort)
    expected = {arxiv_id_to_folder_key(arxiv_id) for _, arxiv_id in pairs}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (path.is_absolute() or "\\" in member.name
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or path.parts[0] not in expected
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"Unsafe cohort archive member: {member.name}")
            if member.isdir():
                (corpus_root.joinpath(*path.parts)).mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Cannot read cohort archive member: {member.name}")
            destination = corpus_root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(source.read())


def _file_hashes(version_dir: Path, files: list[str]) -> tuple[dict[str, str], int]:
    hashes, size = {}, 0
    for relative in files:
        path = version_dir / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Filtered source file is missing or unsafe: {relative}")
        hashes[relative] = _sha256(path)
        size += path.stat().st_size
    return hashes, size


def _hashes_by_paper(corpus_root: Path, pairs: list[tuple[str, str]]) -> dict:
    snapshot = {}
    for _mssv, arxiv_id in pairs:
        paper_dir = corpus_root / arxiv_id_to_folder_key(arxiv_id)
        snapshot[arxiv_id] = {
            name: _sha256(paper_dir / name)
            for name in ("metadata.json", "references.json")
        }
    return snapshot


def _reprocess(
    archive_path: Path,
    output_dir: Path,
    *,
    cohort: int,
    expected_sha256: str,
) -> dict:
    archive_path = archive_path.expanduser().resolve()
    output_dir = output_dir.expanduser().absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output_dir}")
    actual_sha = _sha256(archive_path)
    if actual_sha != expected_sha256:
        raise ValueError(
            f"Source archive checksum mismatch: expected {expected_sha256}, got {actual_sha}"
        )
    source_audit = audit_cohort_archive_payload(
        archive_path, cohort, require_full=True,
    )
    pairs = cohort_pairs(cohort)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        local_root = stage / "local"
        drive_root = stage / "drive"
        corpus_root = local_root / "corpus"
        artifact_root = drive_root / "reports" / "derived_bibliography" / BBL_POLICY_VERSION
        archives_root = drive_root / "archives"
        reports_root = drive_root / "reports"
        corpus_root.mkdir(parents=True)
        artifact_root.mkdir(parents=True)
        archives_root.mkdir(parents=True)
        reports_root.mkdir(parents=True, exist_ok=True)
        _extract_verified_archive(archive_path, corpus_root, cohort)

        original_references = _hashes_by_paper(corpus_root, pairs)
        original_versions = {}
        original_tex_bib_hashes = {}
        final_tex_bib_hashes = {}
        source_bbl_count = 0
        decisions = []
        status_counts: dict[str, int] = {}
        for mssv, arxiv_id in pairs:
            paper_dir = corpus_root / arxiv_id_to_folder_key(arxiv_id)
            manifest_path = paper_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("arxiv_id") != arxiv_id or manifest.get("mssv") != mssv:
                raise ValueError(f"Manifest identity mismatch for {arxiv_id}")
            original_versions[arxiv_id] = sorted(manifest.get("versions", {}))
            for version, record in sorted(manifest.get("versions", {}).items()):
                files = record.get("files", [])
                for name, digest in (record.get("file_checksums") or {}).items():
                    if PurePosixPath(name).suffix.lower() in {".tex", ".bib"}:
                        original_tex_bib_hashes[(arxiv_id, version, name)] = digest
                source_bbl_count += sum(
                    PurePosixPath(name).suffix.lower() == ".bbl" for name in files
                )
                version_dir = paper_dir / version
                remaining, policy = apply_bbl_source_policy(
                    version_dir,
                    files,
                    artifact_root=artifact_root,
                    mssv=mssv,
                    arxiv_id=arxiv_id,
                    version=version,
                )
                hashes, kept_bytes = _file_hashes(version_dir, remaining)
                record["files"] = sorted(remaining)
                record["file_checksums"] = hashes
                record["kept_bytes"] = kept_bytes
                record["bbl_policy"] = policy
                for name, digest in hashes.items():
                    if PurePosixPath(name).suffix.lower() in {".tex", ".bib"}:
                        final_tex_bib_hashes[(arxiv_id, version, name)] = digest
                for item in policy["items"]:
                    decision = {
                        "mssv": mssv,
                        "arxiv_id": arxiv_id,
                        "version": version,
                        **item,
                    }
                    decisions.append(decision)
                    status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _atomic_json(manifest_path, manifest)

        expected_versions = sum(len(versions) for versions in original_versions.values())
        if len(original_versions) != len(pairs):
            raise ValueError("Original archive did not contain all assigned IDs")
        if expected_versions != 172:
            raise ValueError(f"Expected 172 source versions, found {expected_versions}")
        if final_tex_bib_hashes != original_tex_bib_hashes:
            raise ValueError("A source .tex or .bib checksum changed during BBL reprocessing")
        if any(
            _hashes_by_paper(corpus_root, [pair])[pair[1]] != original_references[pair[1]]
            for pair in pairs
        ):
            raise ValueError("metadata.json or references.json changed during BBL reprocessing")

        runner = FastProductionRunner(local_root, drive_root)
        archive_output = archives_root / f"cohort_{cohort:04d}_corpus.tar.gz"
        runner._archive_cohort(cohort, archive_output)
        new_archive_audit = audit_cohort_archive_payload(
            archive_output, cohort, require_full=True,
        )
        integrity = audit_cohort_integrity(runner.store, pairs, cohort_number=cohort)
        handoff = verify_cohort_handoff(runner, cohort, inspect_members=True)

        expected_counts_match = (
            source_bbl_count == 98
            and status_counts.get("removed_bib_present", 0) == 73
            and status_counts.get("retained_conversion_failed", 0) == 25
            and sum(status_counts.values()) == source_bbl_count
        )
        report = {
            "schema_version": 1,
            "cohort_number": cohort,
            "policy_version": BBL_POLICY_VERSION,
            "source_archive": str(archive_path),
            "source_archive_sha256": actual_sha,
            "source_archive_audit": source_audit,
            "output_corpus_archive": str(archive_output),
            "output_corpus_archive_sha256": _sha256(archive_output),
            "output_bbl_artifact_archive": str(
                archives_root / f"cohort_{cohort:04d}_bbl_artifacts.tar.gz"
            ),
            "paper_count": len(original_versions),
            "version_count": expected_versions,
            "source_bbl_file_count": source_bbl_count,
            "bbl_decision_counts": status_counts,
            "expected_25_failures_and_73_bib_present_match": expected_counts_match,
            "metadata_and_references_unchanged": True,
            "tex_and_bib_checksums_unchanged": True,
            "output_archive_audit": new_archive_audit,
            "integrity_audit": integrity.to_dict(),
            "handoff_audit": handoff,
            "items": decisions,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(reports_root / f"bbl_policy_reprocess_cohort_{cohort:04d}.json", report)

        if integrity.invalid_count or integrity.incomplete_count or integrity.corrupted_count:
            raise ValueError("Filtered cohort failed the independent corpus integrity audit")
        if handoff.get("bbl_policy_state") != "complete":
            raise ValueError("Filtered cohort does not pass the BBL policy audit")

        os.replace(stage, output_dir)
        final_archive = output_dir / "drive" / "archives" / archive_output.name
        final_artifact_archive = (
            output_dir / "drive" / "archives" / f"cohort_{cohort:04d}_bbl_artifacts.tar.gz"
        )
        final_report = (
            output_dir / "drive" / "reports" / f"bbl_policy_reprocess_cohort_{cohort:04d}.json"
        )
        report["output_dir"] = str(output_dir)
        report["output_corpus_archive"] = str(final_archive)
        report["output_bbl_artifact_archive"] = str(final_artifact_archive)
        report["report_path"] = str(final_report)
        report["output_archive_audit"]["archive_path"] = str(final_archive)
        report["handoff_audit"]["archive_path"] = str(final_archive)
        _atomic_json(final_report, report)
        return report
    except BaseException:
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cohort", type=int, default=151)
    args = parser.parse_args()
    report = _reprocess(
        args.archive,
        args.output_dir,
        cohort=args.cohort,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
