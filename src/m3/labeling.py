"""Manual labeling helpers and MRR evaluation for M3."""
from __future__ import annotations

import json
import os

import pyarrow.parquet as pq

from .corpus import join_publications, load_bibtex, load_references
from .matching import build_candidates, mrr_at_k


def load_labels(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_labels(path, labels):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(labels, f, indent=2, ensure_ascii=False)


def interactive_label(refs_path, bibtex_path, labels_path, limit_pubs=5, top_k=5):
    refs_rows = load_references(refs_path)
    bibtex_rows = load_bibtex(bibtex_path)
    pubs = join_publications(refs_rows, bibtex_rows)
    labels = load_labels(labels_path)

    done = 0
    for aid in sorted(pubs):
        if done >= limit_pubs:
            break
        bib_list, ref_list = pubs[aid]
        if not bib_list or not ref_list:
            continue
        if aid not in labels:
            labels[aid] = {}
        cands = build_candidates(bib_list, ref_list)
        print("\n=== Publication", aid, "(", len(bib_list), "entries,", len(ref_list), "refs ) ===")
        for be, scored in cands:
            key = be.get("citation_key")
            if key in labels[aid]:
                continue
            print("\nEntry:", key, "|", be.get("title"))
            for i, (ref, score) in enumerate(scored[:top_k], start=1):
                print(f"  {i}. [{score:.3f}] {ref.get('ref_title')} | {ref.get('ref_authors')} | {ref.get('ref_submission_date')}")
            ans = input("correct rank (1-5, 0=none, s=skip pub): ").strip().lower()
            if ans == "s":
                break
            try:
                r = int(ans)
            except ValueError:
                r = 0
            if r == 0:
                labels[aid][key] = None
            elif 1 <= r <= len(scored):
                labels[aid][key] = scored[r - 1][0].get("ref_arxiv_id")
            else:
                labels[aid][key] = None
        done += 1
        save_labels(labels_path, labels)
    save_labels(labels_path, labels)
    return labels


def evaluate_mrr(labels_path, predictions_path, k=5):
    labels = load_labels(labels_path)
    table = pq.read_table(predictions_path)
    # group predictions by (arxiv_id, citation_key) -> ordered ref ids
    preds = {}
    for i in range(table.num_rows):
        aid = table.column("arxiv_id")[i].as_py()
        key = table.column("citation_key")[i].as_py()
        rank = table.column("rank")[i].as_py()
        ref = table.column("ref_arxiv_id")[i].as_py()
        if rank <= k:
            preds.setdefault((aid, key), []).append((rank, ref))
    # build per-publication prediction id lists + ground truth
    pub_preds = {}
    pub_gts = {}
    for (aid, key), lst in preds.items():
        lst.sort(key=lambda x: x[0])
        pub_preds.setdefault(aid, []).append([r for _, r in lst])
        gt = labels.get(aid, {}).get(key) if isinstance(labels.get(aid), dict) else None
        if gt is not None:
            pub_gts.setdefault(aid, []).append(gt)
        else:
            pub_gts.setdefault(aid, []).append(None)
    # only publications with at least one non-None ground truth
    eval_preds = []
    eval_gts = []
    for aid in pub_preds:
        gts = pub_gts.get(aid, [])
        if not any(g is not None for g in gts):
            continue
        # align: only entries that have ground truth
        p_list = pub_preds[aid]
        filtered_p = []
        filtered_g = []
        for idx, g in enumerate(gts):
            if g is None:
                continue
            filtered_p.append(p_list[idx])
            filtered_g.append(g)
        if filtered_p:
            eval_preds.extend(filtered_p)
            eval_gts.extend(filtered_g)
    # mrr_at_k expects list of prediction lists and list of ground truths
    mrr = mrr_at_k(eval_preds, eval_gts, k=k)
    return {"evaluated_entries": len(eval_preds), "mrr_at_k": mrr}
