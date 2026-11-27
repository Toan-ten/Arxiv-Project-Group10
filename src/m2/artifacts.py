"""Read verified, publication-bound M1 BBL evidence without modifying the corpus."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..bbl_handling import BblConversionError, validate_bbl_source_policy
from ..id_ranges import arxiv_id_to_folder_key
from .latex import BblIntegrityError, ParseError, safe_path


def verified_bbl_artifacts(version_root, arxiv_id, version):
    """Resolve the existing Safe-Plus staging root using M1's per-file index.

    ``<paper>/bbl`` is the explicit staging root. Keep all paths from M1's
    index below it; a tar/zip download hash cannot replace ``bbl_sha256``.
    The index may retain M1's <paper-key>/vN prefix or be staged as vN.
    Neither placement permits a different publication/version's evidence.
    """
    paper = version_root.parent
    try:
        artifact_root = safe_path(paper, "bbl")
        if not artifact_root.exists():
            return {}
        if not artifact_root.is_dir():
            raise BblIntegrityError("BBL artifact root is not a directory")
        if not isinstance(arxiv_id, str) or type(version) is not int or version < 1:
            raise BblIntegrityError("External BBL requires expected arXiv ID/version")
        name = f"v{version}"
        if version_root.name != name:
            raise BblIntegrityError("External BBL version directory mismatch")
        manifest = json.loads(safe_path(paper, "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads(safe_path(paper, "metadata.json").read_text(encoding="utf-8"))
        owner = manifest.get("mssv")
        if (not isinstance(owner, str) or not owner
                or any(obj.get("contract_version") != 2 or obj.get("arxiv_id") != arxiv_id
                       or obj.get("mssv") != owner for obj in (manifest, metadata))):
            raise BblIntegrityError("External BBL metadata/manifest identity mismatch")
        record = manifest["versions"][name]
        files, hashes = record["files"], record["file_checksums"]
        if (record.get("status") != "success" or not isinstance(files, list)
                or not isinstance(hashes, dict) or not hashes or set(files) != set(hashes)
                or {p.relative_to(version_root).as_posix() for p in version_root.rglob("*")
                    if p.is_file()} != set(files)):
            raise BblIntegrityError("External BBL source manifest mismatch")
        for file in files:
            source = safe_path(version_root, file)
            if hashlib.sha256(source.read_bytes()).hexdigest() != hashes[file]:
                raise BblIntegrityError("External BBL source checksum mismatch: " + file)
        policy = record.get("bbl_policy")
        if not isinstance(policy, dict) or not policy.get("index_path"):
            # M1 recorded no bibliography for this version at all. That is an absence
            # of evidence, not invalid evidence: the version must keep its own failure
            # (for example missing citations) instead of an integrity error, otherwise
            # carrying artefacts would make results worse than carrying none.
            return {}
        # Validate exact paths before the M1 validator; include Windows drive/ADS
        # syntax and symlink ancestors, not just POSIX traversal.
        prefix = {name, arxiv_id_to_folder_key(arxiv_id) + "/" + name}
        index_path = policy.get("index_path")
        if not isinstance(index_path, str) or index_path not in {p + "/index.json" for p in prefix}:
            raise BblIntegrityError("External BBL index path is not bound to this version")
        safe_path(artifact_root, index_path)
        entries = policy.get("items")
        if not isinstance(entries, list):
            raise BblIntegrityError("Invalid external BBL policy items")
        candidates = {}
        for item in entries:
            bbl_path = item["bbl_path"]
            safe_path(version_root, bbl_path)
            if item.get("status") == "retained_conversion_failed":
                continue  # The original is already inside the version, not external.
            evidence = item.get("source_evidence_path")
            if not isinstance(evidence, str) or evidence not in {
                    p + "/source_bbl/" + bbl_path for p in prefix}:
                raise BblIntegrityError("External BBL evidence path is not bound to this paper/version")
            source = safe_path(artifact_root, evidence)
            derived = item.get("derived_bib_path")
            if derived is not None:
                if derived not in {p + "/derived_bib/" + filename
                                   for p in prefix for filename in
                                   (bbl_path + ".bib", Path(bbl_path).with_suffix(".bib").as_posix())}:
                    raise BblIntegrityError("External BBL derived path is not bound to this paper/version")
                safe_path(artifact_root, derived)
            candidates[source] = dict(bbl_path=bbl_path, sha256=item.get("bbl_sha256"))
        validation = validate_bbl_source_policy(
            version_root, files, policy, artifact_root=artifact_root,
            mssv=owner, arxiv_id=arxiv_id, version=name)
        if validation["status"] != "verified":
            raise BblIntegrityError("External BBL evidence is not verified")
        return candidates
    except BblIntegrityError:
        raise
    except (BblConversionError, ParseError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BblIntegrityError("External BBL verification failed: " + str(exc)) from exc
