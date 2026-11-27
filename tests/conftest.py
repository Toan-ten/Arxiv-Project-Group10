"""Lazy, offline fixtures shared by M2 bibliography integrity tests."""
import hashlib
import json

import pytest

from src.bbl_handling import apply_bbl_source_policy, validate_bbl_source_policy
from src.id_ranges import arxiv_id_to_folder_key


@pytest.fixture
def m2_external_bbl(tmp_path):
    def create(*, aid="2508.92001", version=1, key="k", names=("main.bbl",), bibliography_request=True):
        paper = tmp_path / "corpus" / arxiv_id_to_folder_key(aid)
        source = paper / f"v{version}"
        source.mkdir(parents=True)
        text = (r"\documentclass{article}\begin{document}Evidence $x=y$ "
                + r"\cite{" + key + r"}.\section{Results}After.\bibliography{refs}\end{document}")
        if not bibliography_request:
            text = text.replace(r"\bibliography{refs}", "")
        (source / "main.tex").write_text(text, encoding="utf8")
        raw = (r"\refsection{0}\datalist[entry]{none}\entry{" + key
               + r"}{article}{}\field{title}{Verified evidence}\field{year}{2020}"
               + r"\endentry\enddatalist\endrefsection").encode()
        for name in names:
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(raw)
        files, policy = apply_bbl_source_policy(
            source, ["main.tex", *names], artifact_root=paper / "bbl",
            mssv="24120010", arxiv_id=aid, version=f"v{version}")
        identity = dict(contract_version=2, arxiv_id=aid, mssv="24120010")
        manifest = dict(identity, versions={f"v{version}": dict(
            status="success", files=files,
            file_checksums={f: hashlib.sha256((source / f).read_bytes()).hexdigest() for f in files},
            bbl_policy=policy)})
        (paper / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
        (paper / "metadata.json").write_text(json.dumps(identity), encoding="utf8")
        assert validate_bbl_source_policy(
            source, files, policy, artifact_root=paper / "bbl", mssv=identity["mssv"],
            arxiv_id=aid, version=f"v{version}")["status"] == "verified"
        return dict(paper=paper, source=source, manifest=manifest, policy=policy, aid=aid,
                    artifact=paper / "bbl" / policy["items"][0]["source_evidence_path"])
    return create
