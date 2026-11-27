"""Push the assembled dataset tree to the Hub and print the public link to submit.

Kept separate from the exporter on purpose: building the tree is reproducible offline, while
pushing depends on credentials and on an account name that should not be baked into the
files. The card is rewritten with the account that actually authenticated, so the loading
snippets in it are copy-pasteable by whoever opens the dataset.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def rewrite_card(card_path, repo_id):
    """Put the real repository id in the card's title and loading snippets."""
    text = Path(card_path).read_text(encoding="utf-8")
    text = re.sub(r"^# .+$", f"# {repo_id}", text, count=1, flags=re.MULTILINE)
    text = re.sub(r'load_dataset\("[^"]+"', f'load_dataset("{repo_id}"', text)
    Path(card_path).write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Push the M3 dataset tree to Hugging Face")
    ap.add_argument("--folder", required=True, help="Dataset repository root")
    ap.add_argument("--name", default="arxiv-project-group10", help="Repository name")
    ap.add_argument("--private", action="store_true",
                    help="Create private; the submission needs a public link")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    api = HfApi()
    try:
        who = api.whoami()
    except Exception as exc:                      # no credentials yet
        print(f"not authenticated: {type(exc).__name__}: {exc}")
        return 2
    owner = who.get("name") or who.get("user")
    repo_id = f"{owner}/{args.name}"
    print(f"account : {owner}")
    print(f"repo    : {repo_id}")

    folder = Path(args.folder)
    files = sorted(folder.rglob("*.parquet")) + [folder / "README.md"]
    total = sum(path.stat().st_size for path in files if path.exists())
    print(f"payload : {len(files)} file(s), {total / 1e6:.0f} MB")

    rewrite_card(folder / "README.md", repo_id)
    if args.dry_run:
        print("dry run: nothing uploaded")
        return 0

    api.create_repo(repo_id, repo_type="dataset", exist_ok=True, private=args.private)
    api.upload_folder(folder_path=str(folder), repo_id=repo_id, repo_type="dataset",
                      commit_message="Six sharded Parquet tables for M3 reference matching")
    url = f"https://huggingface.co/datasets/{repo_id}"
    print(f"public  : {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
