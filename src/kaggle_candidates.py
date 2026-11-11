"""Build a checksum-bound Kaggle candidate index without writing corpus state.

Candidates are never evidence that an arXiv paper or version exists.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timezone
from typing import Iterable

from src.kaggle_discovery import _parse_iso_date
from src.storage import _atomic_json


def file_sha256(path: str | Path) -> str:
    """Return SHA-256 of a file using bounded memory."""
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def build_candidate_index(snapshot: str | Path, pairs: Iterable[tuple[str, str]],
                          output: str | Path) -> dict:
    """Scan one snapshot for exact assigned IDs; write an atomic candidate index.

    Duplicate/malformed records are never accepted as candidates. An interrupted
    scan leaves no completed index and existing corpus files are untouched.
    """
    snapshot, output = Path(snapshot), Path(output)
    selected = list(pairs)
    ids = [a for _, a in selected]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate target ID')
    started = time.monotonic()
    checksum = file_sha256(snapshot)
    checksum_seconds = time.monotonic() - started
    target = set(ids)
    candidates, invalid, duplicate, seen = {}, set(), set(), set()
    errors = 0
    started = time.monotonic()
    opener = gzip.open if snapshot.suffix == '.gz' else open
    with opener(snapshot, 'rb') as stream:
        for raw in stream:
            try:
                record = json.loads(raw.decode('utf-8'))
                if not isinstance(record, dict):
                    raise ValueError('record is not an object')
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                errors += 1
                continue
            aid = record.get('id')
            if not isinstance(aid, str) or aid not in target:
                continue
            if aid in seen:
                duplicate.add(aid)
                candidates.pop(aid, None)
                continue
            seen.add(aid)
            versions = record.get('versions')
            if not isinstance(versions, list) or not versions or not all(isinstance(v, dict) for v in versions):
                invalid.add(aid)
                continue
            names = [v.get('version') for v in versions]
            dates = [_parse_iso_date(v.get('created')) for v in versions]
            if (names != [f'v{i}' for i in range(1, len(versions) + 1)]
                    or not all(dates) or dates != sorted(dates)):
                invalid.add(aid)
                continue
            parsed = record.get('authors_parsed')
            authors = None
            if isinstance(parsed, list) and parsed and all(isinstance(x, list) and len(x) >= 2 and all(isinstance(y, str) for y in x) for x in parsed):
                authors = [' '.join(part.strip() for part in [x[1], x[0], *x[2:]] if part.strip()) for x in parsed]
            candidates[aid] = dict(version_ids=[aid + name for name in names], dates=dates,
                                   authors=authors, authors_raw=record.get('authors'),
                                   title=record.get('title'), withdrawal_hint=record.get('comments'))
    if file_sha256(snapshot) != checksum:
        raise ValueError('snapshot changed while scanning')
    result = dict(schema_version=1, status='complete', snapshot_sha256=checksum,
                  snapshot_size_bytes=snapshot.stat().st_size, snapshot_time='unknown',
                  dataset='Cornell-University/arxiv', created_at=datetime.now(timezone.utc).isoformat(),
                  targets=sorted(ids), candidates=candidates,
                  diagnostics=dict(invalid_ids=sorted(invalid), duplicate_ids=sorted(duplicate),
                                   missing_ids=sorted(target-seen), malformed_lines=errors),
                  checksum_seconds=checksum_seconds, scan_seconds=time.monotonic()-started)
    _atomic_json(output, result)
    return result


def apply_verified_history(store, mssv: str, arxiv_id: str, candidate: dict,
                           provenance: dict) -> bool:
    """Apply candidate dates only to a freshly arXiv-confirmed, matching paper.

    The caller must supply freshly validated arXiv discovery; this function
    checks existing metadata for consistency before changing anything.
    """
    from src import arxiv_history
    meta = store.read_metadata(mssv, arxiv_id)
    if meta.get('status') == 'complete' or meta['provenance'].get('version_history'):
        return False
    versions = meta.get('versions') or []
    ids = [v.get('versioned_id') for v in versions]
    dates = candidate.get('dates') or []
    if ids != candidate.get('version_ids') or len(ids) < 2 or len(dates) != len(ids):
        return False
    if (provenance.get('status') != 'complete'
            or not isinstance(provenance.get('snapshot_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', provenance['snapshot_sha256']) is None
            or provenance.get('dataset') != 'Cornell-University/arxiv'
            or not isinstance(provenance.get('snapshot_time'), str)
            or not provenance['snapshot_time'].strip()):
        return False
    if not all(_parse_iso_date(d) == d for d in dates) or dates != sorted(dates):
        return False
    discovery = (meta['provenance'].get('arxiv') or {}).get('snapshot') or {}
    if (discovery.get('status') != 'found' or discovery.get('http_status') != 200
            or discovery.get('version_status') != 'resolved'
            or discovery.get('version_ids') != ids
            or _parse_iso_date(discovery.get('updated')) != dates[-1]
            or _parse_iso_date(meta.get('submission_date')) != dates[0]):
        return False
    if any(v.get('date') and _parse_iso_date(v['date']) != dates[i] for i, v in enumerate(versions)):
        return False
    # Keep v1 spelling consistent with the arXiv value and use candidate dates
    # only for revisions. The provider's evidence remains separate from arXiv.
    for i, item in enumerate(versions):
        item['date'] = meta['submission_date'] if i == 0 else dates[i]
        item['date_status'] = 'verified'
    meta['revised_dates'] = dates[1:]
    meta['provenance']['version_history'] = dict(
        provider='kaggle_snapshot', query_identifier=arxiv_id,
        retrieved_at=datetime.now(timezone.utc).isoformat(), snapshot=dates[1:],
        snapshot_sha256=provenance['snapshot_sha256'], dataset=provenance['dataset'],
        snapshot_time=provenance['snapshot_time'], source_record=candidate)
    meta['missing_fields'] = [f for f in meta['missing_fields'] if f != 'version_history']
    store.save_metadata(mssv, arxiv_id, meta)
    return arxiv_history._cached(store.read_metadata(mssv, arxiv_id))


def prepare_snapshot(cache_dir: str | Path, *, token: str | None = None) -> dict:
    """Reuse a verified snapshot or download it atomically with the Kaggle CLI.

    A failed or unverifiable cache is never overwritten. Credentials stay in
    the child environment and errors contain no command output or secrets.
    """
    import shutil
    import subprocess
    import sys
    import zipfile

    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'arxiv-metadata-oai-snapshot.json'
    manifest = root / 'snapshot.manifest.json'
    started = time.monotonic()
    if path.exists() or manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            if not path.is_file() or saved['sha256'] != file_sha256(path):
                raise ValueError('snapshot checksum mismatch')
            return dict(status='ready', source='cached', snapshot_path=str(path),
                        snapshot_sha256=saved['sha256'], snapshot_time=saved.get('snapshot_time', 'unknown'),
                        validation_seconds=time.monotonic()-started, download_seconds=0.0)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return dict(status='fallback', reason='invalid_existing_cache', download_seconds=None,
                        validation_seconds=time.monotonic()-started)
    if not token or not token.strip():
        return dict(status='fallback', reason='missing_kaggle_token', download_seconds=None,
                    validation_seconds=None)
    try:
        with tempfile.TemporaryDirectory(prefix='.kaggle-', dir=root) as tmp:
            temporary = Path(tmp)
            env = os.environ.copy()
            env['KAGGLE_API_TOKEN'] = token.strip()
            started = time.monotonic()
            proc = subprocess.run([sys.executable, '-c', 'from kaggle.cli import main; main()', 'datasets', 'download',
                                   '-d', 'Cornell-University/arxiv', '-f', path.name,
                                   '-p', str(temporary)], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, check=False, timeout=3600)
            download_seconds = time.monotonic()-started
            if proc.returncode:
                return dict(status='fallback', reason='kaggle_download_failed',
                            error_type='KaggleDownloadError', download_seconds=download_seconds,
                            validation_seconds=None)
            raw = temporary / path.name
            archive = temporary / (path.name + '.zip')
            if not raw.is_file() and archive.is_file():
                with zipfile.ZipFile(archive) as zf:
                    members = [m for m in zf.infolist() if m.filename == path.name and not m.is_dir()]
                    if len(members) != 1:
                        raise ValueError('snapshot archive has unexpected members')
                    with zf.open(members[0]) as source, raw.open('xb') as output:
                        shutil.copyfileobj(source, output, length=1024*1024)
            started = time.monotonic()
            if not raw.is_file():
                raise ValueError('snapshot file missing')
            with raw.open('rb') as stream:
                first = json.loads(stream.readline().decode('utf-8'))
                if not isinstance(first, dict) or 'id' not in first:
                    raise ValueError('invalid snapshot')
            digest = file_sha256(raw)
            metadata = dict(dataset='Cornell-University/arxiv', snapshot_time='unknown',
                            downloaded_at=datetime.now(timezone.utc).isoformat(),
                            size_bytes=raw.stat().st_size, sha256=digest)
            # The pipeline requires one writer for a cache. Both files must be
            # absent; a crash between atomic replacements is detected on resume.
            if path.exists() or manifest.exists():
                raise FileExistsError('snapshot cache appeared during download')
            os.replace(raw, path)
            _atomic_json(manifest, metadata)
            return dict(status='ready', source='downloaded', snapshot_path=str(path),
                        snapshot_sha256=digest, snapshot_time='unknown',
                        download_seconds=download_seconds, validation_seconds=time.monotonic()-started)
    except subprocess.TimeoutExpired:
        return dict(status='fallback', reason='download_timeout', error_type='TimeoutExpired',
                    download_seconds=None, validation_seconds=None)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        return dict(status='fallback', reason='download_or_validation_failed',
                    error_type=type(exc).__name__, download_seconds=None, validation_seconds=None)


def reuse_or_build_index(snapshot: str | Path, pairs: Iterable[tuple[str, str]],
                         cache_dir: str | Path, expected_sha256: str) -> dict:
    """Reuse a completed index only for the exact snapshot and target IDs."""
    selected = list(pairs)
    ids = sorted(a for _, a in selected)
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate target ID')
    if file_sha256(snapshot) != expected_sha256:
        raise ValueError('snapshot checksum mismatch')
    key = hashlib.sha256(json.dumps([expected_sha256, ids], separators=(',', ':')).encode()).hexdigest()
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'candidates_{key}.json'
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data['schema_version'] == 1 and data['status'] == 'complete'
                    and data['snapshot_sha256'] == expected_sha256 and data['targets'] == ids
                    and isinstance(data['candidates'], dict) and isinstance(data['diagnostics'], dict)):
                return dict(index_path=str(path), reused=True, index=data)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Keep the damaged cache as evidence and refuse to overwrite it.
        raise ValueError('candidate index invalid')
    data = build_candidate_index(snapshot, selected, path)
    if data['snapshot_sha256'] != expected_sha256:
        raise ValueError('snapshot changed while scanning')
    return dict(index_path=str(path), reused=False, index=data)
