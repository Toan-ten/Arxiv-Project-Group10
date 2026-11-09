"""Generate M1 Milestone 1 execution statistics and markdown report tables.

Computes comprehensive crawling, storage, extraction, and reference coverage metrics
for insertion into the group's Jupyter Book technical report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from src.storage import CorpusStorage, arxiv_id_to_folder_key


def compute_m1_statistics(store: CorpusStorage, pairs: Iterable[Tuple[str, str]]) -> Dict:
    """Compute all 9 canonical Milestone 1 crawling and extraction metrics."""
    pairs_list = list(pairs)
    total_candidates = len(pairs_list)
    found_papers = 0
    not_found_papers = 0
    completed_papers = 0
    total_versions = 0
    completed_versions = 0
    no_tex_versions = 0
    total_references = 0
    papers_with_references = 0

    for mssv, arxiv_id in pairs_list:
        folder_key = arxiv_id_to_folder_key(arxiv_id)
        manifest_file = store.root / folder_key / "manifest.json"
        if not manifest_file.exists():
            continue

        try:
            m = json.loads(manifest_file.read_text(encoding="utf-8"))
            disc = m.get("discovery_status")
            if disc == "not_found":
                not_found_papers += 1
                continue
            if disc == "found":
                found_papers += 1
                versions = m.get("versions") or {}
                total_versions += len(versions)
                paper_complete = True
                for v_name, v_info in versions.items():
                    v_stat = v_info.get("status")
                    if v_stat == "complete":
                        completed_versions += 1
                    elif v_stat == "no_tex_source":
                        completed_versions += 1
                        no_tex_versions += 1
                    else:
                        paper_complete = False

                if paper_complete and versions:
                    completed_papers += 1

            # Read references if present
            ref_file = store.root / folder_key / "references.json"
            if ref_file.exists():
                try:
                    ref_data = json.loads(ref_file.read_text(encoding="utf-8"))
                    items = ref_data.get("items") or []
                    if items:
                        total_references += len(items)
                        papers_with_references += 1
                except Exception:
                    pass
        except Exception:
            continue

    success_rate = (completed_papers / found_papers * 100.0) if found_papers > 0 else 0.0

    return {
        "total_candidates": total_candidates,
        "found_papers": found_papers,
        "not_found_papers": not_found_papers,
        "completed_papers": completed_papers,
        "total_versions": total_versions,
        "completed_versions": completed_versions,
        "no_tex_versions": no_tex_versions,
        "success_rate": round(success_rate, 2),
        "total_references": total_references,
        "papers_with_references": papers_with_references,
        "average_references_per_paper": round(total_references / max(1, found_papers), 2),
    }


def format_markdown_summary(stats: Dict) -> str:
    """Format computed statistics into a standard Markdown table for Jupyter Book."""
    lines = [
        "| Chỉ số | Giá trị |",
        "|---|---:|",
        f"| Tổng số ứng viên phân công | {stats['total_candidates']:,} |",
        f"| Bài báo tồn tại trên arXiv (found) | {stats['found_papers']:,} |",
        f"| Bài báo không tồn tại (not_found) | {stats['not_found_papers']:,} |",
        f"| Bài báo thu thập hoàn tất (completed) | {stats['completed_papers']:,} |",
        f"| Tỷ lệ thành công (Success Rate) | {stats['success_rate']}% |",
        f"| Tổng số phiên bản (versions) | {stats['total_versions']:,} |",
        f"| Phiên bản hoàn tất trích xuất | {stats['completed_versions']:,} |",
        f"| Phiên bản không chứa TeX (no_tex_source) | {stats['no_tex_versions']:,} |",
        f"| Tổng số trích dẫn Semantic Scholar | {stats['total_references']:,} |",
        f"| Số trích dẫn trung bình / bài báo | {stats['average_references_per_paper']} |",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate Milestone 1 summary statistics report.")
    parser.add_argument("--corpus", required=True, help="Path to corpus root directory")
    parser.add_argument("--output", default="m1_report_summary.md", help="Output Markdown report path")
    args = parser.parse_args()

    from src.id_ranges import iter_assigned_ids
    store = CorpusStorage(Path(args.corpus))
    stats = compute_m1_statistics(store, iter_assigned_ids())
    md = format_markdown_summary(stats)

    out_path = Path(args.output)
    out_path.write_text(md, encoding="utf-8")
    print(f"Đã xuất báo cáo thống kê M1 ra tệp: {out_path}")
    print(md)


if __name__ == "__main__":
    main()
