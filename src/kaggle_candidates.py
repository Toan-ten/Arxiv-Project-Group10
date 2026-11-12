"""Build a checksum-bound Kaggle candidate index without writing corpus state.

Candidates are never evidence that an arXiv paper or version exists.
"""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import date, datetime, timezone
from typing import Iterable

from src.kaggle_discovery import _parse_iso_date
from src.storage import _atomic_json

SNAPSHOT_DOWNLOAD_TIMEOUT_SECONDS = 3600
SNAPSHOT_PROGRESS_INTERVAL_SECONDS = 30
_VERIFIED_FILE_HASHES = {}
_VERIFIED_INDEXES = {}


def _file_signature(info) -> tuple[int, int, int, int]:
    """Identify a file instance and its observed content generation."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def file_sha256(path: str | Path) -> str:
    """Return SHA-256 of a file using bounded memory."""
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _format_bytes(value: int) -> str:
    """Format a byte count for progress messages."""
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def _progress_message(label: str, processed: int, total: int | None,
                      started: float) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    rate = processed / elapsed
    if total and total > 0:
        percent = min(processed / total * 100.0, 100.0)
        eta = max(total - processed, 0) / rate if rate > 0 else 0.0
        return (f"Kaggle: {label}: {_format_bytes(processed)} / {_format_bytes(total)} "
                f"({percent:.1f}%); {_format_bytes(int(rate))}/s; "
                f"ETA ~{eta:.0f}s.")
    return (f"Kaggle: {label}: {_format_bytes(processed)} đã xử lý; "
            f"{_format_bytes(int(rate))}/s; elapsed {elapsed:.0f}s.")


def _directory_file_bytes(root: Path) -> int:
    """Return current regular-file bytes under a temporary download directory."""
    total = 0
    for target in root.rglob("*"):
        try:
            if target.is_file() and not target.is_symlink():
                total += target.stat().st_size
        except OSError:
            continue
    return total


def _sha256_with_progress(path: str | Path, label: str) -> tuple[str, float]:
    """Hash a file and emit periodic byte progress without buffering the file."""
    target = Path(path)
    total = target.stat().st_size
    started = time.monotonic()
    last_report = started
    processed = 0
    digest = hashlib.sha256()
    print(f"Kaggle: {label}: bắt đầu ({_format_bytes(total)}).", flush=True)
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
            processed += len(block)
            now = time.monotonic()
            if now - last_report >= SNAPSHOT_PROGRESS_INTERVAL_SECONDS:
                print(_progress_message(label, processed, total, started), flush=True)
                last_report = now
    elapsed = time.monotonic() - started
    print(f"Kaggle: {label}: hoàn tất ({_format_bytes(processed)} trong {elapsed:.1f}s).",
          flush=True)
    return digest.hexdigest(), elapsed


def _copy_with_progress(source, destination, total: int, label: str) -> tuple[int, float]:
    """Copy a stream in bounded chunks and report extraction progress."""
    started = time.monotonic()
    last_report = started
    copied = 0
    while True:
        block = source.read(4 * 1024 * 1024)
        if not block:
            break
        destination.write(block)
        copied += len(block)
        now = time.monotonic()
        if now - last_report >= SNAPSHOT_PROGRESS_INTERVAL_SECONDS:
            print(_progress_message(label, copied, total, started), flush=True)
            last_report = now
    elapsed = time.monotonic() - started
    print(f"Kaggle: {label}: hoàn tất ({_format_bytes(copied)} trong {elapsed:.1f}s).",
          flush=True)
    return copied, elapsed


def _verified_file_sha256(path: str | Path, expected: str) -> str:
    """Reuse a verified hash while device, inode, size and mtime remain unchanged."""
    resolved = Path(path).expanduser().resolve()
    info = resolved.stat()
    signature = _file_signature(info)
    cached = _VERIFIED_FILE_HASHES.get(str(resolved))
    if cached and cached[0] == signature:
        digest = cached[1]
    else:
        if info.st_size >= 64 * 1024 * 1024:
            digest, _ = _sha256_with_progress(resolved, f'xác minh SHA-256 {resolved.name}')
        else:
            digest = file_sha256(resolved)
        if _file_signature(resolved.stat()) != signature:
            raise ValueError(f'Kaggle evidence input changed while hashing: {resolved.name}')
        _VERIFIED_FILE_HASHES[str(resolved)] = (signature, digest)
    if digest != expected:
        raise ValueError(f'Kaggle evidence input checksum mismatch: {resolved.name}')
    return digest


def _remember_verified_file_sha256(
    path: str | Path, digest: str, *, verified_signature: tuple[int, int, int, int] | None = None,
) -> bool:
    """Remember a digest just computed from this exact file instance."""
    resolved = Path(path).expanduser().resolve()
    signature = _file_signature(resolved.stat())
    if verified_signature is not None and signature != verified_signature:
        return False
    _VERIFIED_FILE_HASHES[str(resolved)] = (signature, digest)
    return True


def _calendar_day_utc(value: str | None) -> str | None:
    """Normalize an ISO date/timestamp to the common UTC calendar-day precision."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    # Bản chụp Cornell/Kaggle thực tế lưu giá trị ``created`` của phiên bản dưới dạng
    # dấu thời gian RFC 2822 (ví dụ ``Mon, 2 Apr 2007 19:18:42 GMT``).
    # Giữ khả năng đọc các bản ghi này và chuẩn hóa về độ chính xác ngày UTC.
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def build_candidate_index(snapshot: str | Path, pairs: Iterable[tuple[str, str]],
                          output: str | Path, *, expected_sha256: str | None = None) -> dict:
    """Scan one snapshot for exact assigned IDs; write an atomic candidate index.

    Duplicate/malformed records are never accepted as candidates. An interrupted
    scan leaves no completed index and existing corpus files are untouched.
    """
    snapshot, output = Path(snapshot), Path(output)
    selected = list(pairs)
    ids = [a for _, a in selected]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate target ID')
    snapshot_manifest_path = snapshot.with_name('snapshot.manifest.json')
    try:
        snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding='utf-8'))
        if not isinstance(snapshot_manifest, dict):
            raise ValueError('snapshot manifest is not an object')
        manifest_sha256 = snapshot_manifest.get('sha256')
        if not isinstance(manifest_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', manifest_sha256) is None:
            raise ValueError('snapshot manifest checksum is invalid')
    except FileNotFoundError:
        snapshot_manifest = {}
        manifest_sha256 = None
    if expected_sha256 is not None and re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None:
        raise ValueError('expected snapshot checksum is invalid')
    if expected_sha256 and manifest_sha256 and expected_sha256 != manifest_sha256:
        raise ValueError('snapshot manifest checksum mismatch')
    trusted_sha256 = expected_sha256 or manifest_sha256
    compressed = snapshot.suffix == '.gz'
    checksum_seconds = None
    if compressed:
        # Duyệt gzip trả về từng dòng đã giải nén; vì vậy hãy băm dữ liệu nén
        # trước và sau khi quét để tiếp tục phát hiện tệp bị thay đổi.
        checksum, checksum_seconds = _sha256_with_progress(
            snapshot, 'xác minh SHA-256 snapshot trước khi quét index')
        if trusted_sha256 and checksum != trusted_sha256:
            raise ValueError('snapshot checksum mismatch')
        trusted_sha256 = checksum
        snapshot_signature = snapshot.stat()
        scan_digest = None
    else:
        snapshot_signature = snapshot.stat()
        scan_digest = hashlib.sha256()
        checksum = None
    target = set(ids)
    candidates, invalid, duplicate, seen = {}, set(), set(), set()
    errors = 0
    started = time.monotonic()
    last_report = started
    scanned_bytes = 0
    scanned_lines = 0
    snapshot_size = snapshot_signature.st_size if not compressed else None
    print(f"Kaggle: bắt đầu quét snapshot để dựng index ({len(ids):,} ID mục tiêu).",
          flush=True)
    opener = gzip.open if compressed else open
    with opener(snapshot, 'rb') as stream:
        opened_signature = os.fstat(stream.fileno())
        if not compressed and (
                opened_signature.st_dev != snapshot_signature.st_dev
                or opened_signature.st_ino != snapshot_signature.st_ino):
            raise ValueError('snapshot changed before scanning')
        for raw in stream:
            if scan_digest is not None:
                scan_digest.update(raw)
            scanned_bytes += len(raw)
            scanned_lines += 1
            now = time.monotonic()
            if now - last_report >= SNAPSHOT_PROGRESS_INTERVAL_SECONDS:
                print(_progress_message('quét snapshot để dựng index', scanned_bytes,
                                        snapshot_size, started)
                      + f" Dòng: {scanned_lines:,}; ID khớp: {len(candidates):,}/{len(ids):,}.",
                      flush=True)
                last_report = now
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
            dates = [_calendar_day_utc(v.get('created')) for v in versions]
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
    scan_seconds = time.monotonic() - started
    print(f"Kaggle: quét snapshot hoàn tất ({scanned_lines:,} dòng; "
          f"{len(candidates):,}/{len(ids):,} ID khớp trong {scan_seconds:.1f}s).",
          flush=True)
    final_signature = snapshot.stat()
    if (final_signature.st_dev, final_signature.st_ino, final_signature.st_size,
            final_signature.st_mtime_ns) != (
            snapshot_signature.st_dev, snapshot_signature.st_ino,
            snapshot_signature.st_size, snapshot_signature.st_mtime_ns):
        raise ValueError('snapshot changed while scanning')
    if scan_digest is not None:
        checksum = scan_digest.hexdigest()
        if trusted_sha256 and checksum != trusted_sha256:
            raise ValueError('snapshot changed or checksum mismatched while scanning')
    else:
        final_checksum, final_checksum_seconds = _sha256_with_progress(
            snapshot, 'xác minh SHA-256 snapshot sau khi quét index')
        checksum_seconds += final_checksum_seconds
        if final_checksum != checksum:
            raise ValueError('snapshot changed while scanning')
    downloaded_at = snapshot_manifest.get('downloaded_at')
    provider_updated_at = snapshot_manifest.get('provider_updated_at')
    result = dict(schema_version=2, status='complete', snapshot_sha256=checksum,
                  snapshot_size_bytes=snapshot.stat().st_size, snapshot_time='unknown',
                  downloaded_at=downloaded_at, provider_updated_at=provider_updated_at,
                  comparison_precision='calendar_day_utc',
                  dataset='Cornell-University/arxiv', created_at=datetime.now(timezone.utc).isoformat(),
                  targets=sorted(ids), candidates=candidates,
                  diagnostics=dict(invalid_ids=sorted(invalid), duplicate_ids=sorted(duplicate),
                                   missing_ids=sorted(target-seen), malformed_lines=errors),
                  checksum_seconds=checksum_seconds, checksum_in_scan=scan_digest is not None,
                  scan_seconds=scan_seconds,
                  scanned_lines=scanned_lines)
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
    existing_history = meta['provenance'].get('version_history')
    if meta.get('status') == 'complete' and not (
        isinstance(existing_history, dict)
        and existing_history.get('provider') == 'kaggle_snapshot'
        and provenance.get('provenance_policy') == 'm1-kaggle-reconcile-v2'
    ):
        return False
    if existing_history and (
        existing_history.get('provider') != 'kaggle_snapshot'
        or provenance.get('provenance_policy') != 'm1-kaggle-reconcile-v2'
    ):
        return False
    versions = meta.get('versions') or []
    ids = [v.get('versioned_id') for v in versions]
    dates = candidate.get('dates') or []
    if ids != candidate.get('version_ids') or len(ids) < 2 or len(dates) != len(ids):
        return False
    strict_provenance = provenance.get('provenance_policy') == 'm1-kaggle-reconcile-v2'
    if (provenance.get('status') != 'complete'
            or not isinstance(provenance.get('snapshot_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', provenance['snapshot_sha256']) is None
            or provenance.get('dataset') != 'Cornell-University/arxiv'
            or (strict_provenance and (
                provenance.get('comparison_precision') != 'calendar_day_utc'
                or not isinstance(provenance.get('index_sha256'), str)
                or re.fullmatch(r'[0-9a-f]{64}', provenance['index_sha256']) is None))
            or (strict_provenance and provenance.get('provenance_policy') != 'm1-kaggle-reconcile-v2')
            or (not strict_provenance and not isinstance(provenance.get('snapshot_time'), str))):
        return False
    if not all(_parse_iso_date(d) == d for d in dates) or dates != sorted(dates):
        return False
    discovery = (meta['provenance'].get('arxiv') or {}).get('snapshot') or {}
    if (discovery.get('status') != 'found' or discovery.get('http_status') != 200
            or discovery.get('version_status') != 'resolved'
            or discovery.get('version_ids') != ids
            or _calendar_day_utc(discovery.get('updated')) != dates[-1]
            or _parse_iso_date(meta.get('submission_date')) != dates[0]):
        return False
    if any(v.get('date') and _calendar_day_utc(v['date']) != dates[i] for i, v in enumerate(versions)):
        return False
    # Giữ cách ghi v1 nhất quán với arXiv; chỉ dùng ngày ứng viên
    # cho các lần sửa đổi. Bằng chứng từ nhà cung cấp vẫn tách biệt với arXiv.
    complete_metadata = meta.get('status') == 'complete'
    for i, item in enumerate(versions):
        existing_date = item.get('date')
        if complete_metadata and existing_date and _calendar_day_utc(existing_date) != dates[i]:
            return False
        item['date'] = (existing_date if complete_metadata and existing_date else
                        meta['submission_date'] if i == 0 else dates[i])
        item['date_status'] = 'verified'
    if not complete_metadata:
        meta['revised_dates'] = dates[1:]
    history_dates = (meta['revised_dates'] if complete_metadata
                     and meta['revised_dates'] is not None else dates[1:])
    meta['provenance']['version_history'] = dict(
        provider='kaggle_snapshot', query_identifier=arxiv_id,
        retrieved_at=datetime.now(timezone.utc).isoformat(), snapshot=history_dates,
        snapshot_sha256=provenance['snapshot_sha256'], dataset=provenance['dataset'],
        snapshot_time=provenance.get('snapshot_time', 'unknown'),
        source_record=candidate)
    if strict_provenance:
        meta['provenance']['version_history'].update(
            downloaded_at=provenance.get('downloaded_at'),
            provider_updated_at=provenance.get('provider_updated_at'),
            index_sha256=provenance['index_sha256'],
            comparison_precision='calendar_day_utc',
            provenance_policy='m1-kaggle-reconcile-v2',
        )
    if not complete_metadata:
        meta['missing_fields'] = [f for f in meta['missing_fields'] if f != 'version_history']
    store.save_metadata(mssv, arxiv_id, meta)
    return arxiv_history._cached(store.read_metadata(mssv, arxiv_id))


def prepare_snapshot(cache_dir: str | Path, *, token: str) -> dict:
    """Tái sử dụng snapshot hợp lệ hoặc tải tệp tạm bằng Kaggle CLI.

    Yêu cầu token ngay cả khi có snapshot cache hợp lệ. Hiển thị trạng thái tải
    mà không in thông tin xác thực; snapshot cũ không bị ghi đè khi tải lỗi.
    """
    import subprocess
    import sys
    import zipfile

    if not isinstance(token, str) or not token.strip():
        raise ValueError("KAGGLE_API_TOKEN là bắt buộc để chuẩn bị snapshot.")
    token = token.strip()
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'arxiv-metadata-oai-snapshot.json'
    manifest = root / 'snapshot.manifest.json'
    overall_started = time.monotonic()
    if path.exists() or manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            if not path.is_file():
                raise ValueError('snapshot checksum mismatch')
            hash_signature = _file_signature(path.stat())
            actual_sha, checksum_seconds = _sha256_with_progress(
                path, 'xác minh SHA-256 snapshot cache')
            if _file_signature(path.stat()) != hash_signature:
                raise ValueError('snapshot changed while verifying cache')
            if saved['sha256'] != actual_sha:
                raise ValueError('snapshot checksum mismatch')
            _remember_verified_file_sha256(
                path, actual_sha, verified_signature=hash_signature,
            )
            print('Kaggle: snapshot cache hợp lệ; dùng lại bản đã xác minh.', flush=True)
            return dict(status='ready', source='cached', snapshot_path=str(path),
                        snapshot_sha256=saved['sha256'], snapshot_time=saved.get('snapshot_time', 'unknown'),
                        downloaded_at=saved.get('downloaded_at'),
                        provider_updated_at=saved.get('provider_updated_at'),
                        extraction_seconds=0.0, json_validation_seconds=0.0,
                        checksum_seconds=checksum_seconds, publish_seconds=0.0,
                        validation_seconds=time.monotonic()-overall_started,
                        snapshot_prepare_seconds=time.monotonic()-overall_started,
                        download_seconds=0.0)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            print('Kaggle: snapshot cache không hợp lệ; giữ nguyên tệp và chuyển sang arXiv.', flush=True)
            return dict(status='fallback', reason='invalid_existing_cache', download_seconds=None,
                        validation_seconds=time.monotonic()-overall_started,
                        snapshot_prepare_seconds=time.monotonic()-overall_started)
    try:
        with tempfile.TemporaryDirectory(prefix='.kaggle-', dir=root) as tmp:
            temporary = Path(tmp)
            env = os.environ.copy()
            env['KAGGLE_API_TOKEN'] = token
            download_started = time.monotonic()
            command = [sys.executable, '-c', 'from kaggle.cli import main; main()', 'datasets', 'download',
                       '-d', 'Cornell-University/arxiv', '-f', path.name, '-p', str(temporary)]
            print('Kaggle: bắt đầu tải snapshot arXiv; giới hạn thời gian là 60 phút.', flush=True)
            proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            deadline = download_started + SNAPSHOT_DOWNLOAD_TIMEOUT_SECONDS
            try:
                while True:
                    returncode = proc.poll()
                    if returncode is not None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        proc.kill()
                        proc.wait()
                        raise subprocess.TimeoutExpired(command, SNAPSHOT_DOWNLOAD_TIMEOUT_SECONDS)
                    try:
                        proc.wait(timeout=min(SNAPSHOT_PROGRESS_INTERVAL_SECONDS, remaining))
                    except subprocess.TimeoutExpired:
                        downloaded_bytes = _directory_file_bytes(temporary)
                        progress = _progress_message('dung lượng tệp tạm', downloaded_bytes,
                                                     None, download_started)
                        print('Kaggle: vẫn đang tải snapshot; '
                              + progress.removeprefix('Kaggle: '), flush=True)
            except KeyboardInterrupt:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        proc.wait()
                print('Kaggle: đã dừng tiến trình tải; tệp tạm sẽ được dọn.', flush=True)
                raise
            download_seconds = time.monotonic()-download_started
            if proc.returncode:
                print('Kaggle: tải snapshot thất bại; quy trình sẽ dùng arXiv dự phòng.', flush=True)
                return dict(status='fallback', reason='kaggle_download_failed',
                            error_type='KaggleDownloadError', download_seconds=download_seconds,
                            validation_seconds=None,
                            snapshot_prepare_seconds=time.monotonic()-overall_started)
            print('Kaggle: tải xong; đang kiểm tra snapshot và tính SHA-256.', flush=True)
            validation_started = time.monotonic()
            extraction_seconds = 0.0
            json_validation_seconds = 0.0
            checksum_seconds = 0.0
            publish_seconds = 0.0
            raw = temporary / path.name
            archive = temporary / (path.name + '.zip')
            if not raw.is_file() and archive.is_file():
                print(f'Kaggle: snapshot là ZIP; bắt đầu giải nén JSON '
                      f'({_format_bytes(archive.stat().st_size)} nén).', flush=True)
                extraction_started = time.monotonic()
                with zipfile.ZipFile(archive) as zf:
                    members = [m for m in zf.infolist() if m.filename == path.name and not m.is_dir()]
                    if len(members) != 1:
                        raise ValueError('snapshot archive has unexpected members')
                    with zf.open(members[0]) as source, raw.open('xb') as output:
                        _copy_with_progress(source, output, members[0].file_size,
                                            'giải nén snapshot JSON')
                extraction_seconds = time.monotonic() - extraction_started
            if not raw.is_file():
                raise ValueError('snapshot file missing')
            print('Kaggle: kiểm tra bản ghi JSONL đầu tiên.', flush=True)
            json_validation_started = time.monotonic()
            with raw.open('rb') as stream:
                first = json.loads(stream.readline().decode('utf-8'))
                if not isinstance(first, dict) or 'id' not in first:
                    raise ValueError('invalid snapshot')
            json_validation_seconds = time.monotonic() - json_validation_started
            digest, checksum_seconds = _sha256_with_progress(
                raw, 'tính SHA-256 snapshot JSON')
            verified_raw_signature = _file_signature(raw.stat())
            metadata = dict(dataset='Cornell-University/arxiv', snapshot_time='unknown',
                            downloaded_at=datetime.now(timezone.utc).isoformat(),
                            provider_updated_at=None,
                            size_bytes=raw.stat().st_size, sha256=digest)
            # Quy trình yêu cầu một tiến trình ghi cho mỗi bộ nhớ đệm. Cả hai tệp phải
            # chưa tồn tại; nếu tiến trình dừng giữa hai lần thay thế nguyên tử, sẽ phát hiện khi tiếp tục.
            if path.exists() or manifest.exists():
                raise FileExistsError('snapshot cache appeared during download')
            publish_started = time.monotonic()
            os.replace(raw, path)
            _atomic_json(manifest, metadata)
            _remember_verified_file_sha256(
                path, digest, verified_signature=verified_raw_signature,
            )
            publish_seconds = time.monotonic() - publish_started
            validation_seconds = time.monotonic() - validation_started
            snapshot_prepare_seconds = time.monotonic() - overall_started
            print(f'Kaggle: snapshot đã xác minh và lưu vào cache; '
                  f'tổng xử lý sau tải {validation_seconds:.1f}s.', flush=True)
            return dict(status='ready', source='downloaded', snapshot_path=str(path),
                        snapshot_sha256=digest, snapshot_time='unknown',
                        downloaded_at=metadata['downloaded_at'], provider_updated_at=None,
                        download_seconds=download_seconds,
                        extraction_seconds=extraction_seconds,
                        json_validation_seconds=json_validation_seconds,
                        checksum_seconds=checksum_seconds,
                        publish_seconds=publish_seconds,
                        validation_seconds=validation_seconds,
                        snapshot_prepare_seconds=snapshot_prepare_seconds)
    except subprocess.TimeoutExpired:
        print('Kaggle: quá thời hạn tải 60 phút; chuyển sang xác minh trực tiếp bằng arXiv.', flush=True)
        return dict(status='fallback', reason='download_timeout', error_type='TimeoutExpired',
                    download_seconds=(time.monotonic() - download_started
                                      if 'download_started' in locals() else None),
                    validation_seconds=None,
                    snapshot_prepare_seconds=time.monotonic()-overall_started)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print('Kaggle: không tải hoặc xác minh được snapshot; chuyển sang arXiv.', flush=True)
        scope = locals()
        return dict(status='fallback', reason='download_or_validation_failed',
                    error_type=type(exc).__name__,
                    download_seconds=scope.get('download_seconds'),
                    extraction_seconds=scope.get('extraction_seconds'),
                    json_validation_seconds=scope.get('json_validation_seconds'),
                    checksum_seconds=scope.get('checksum_seconds'),
                    publish_seconds=scope.get('publish_seconds'),
                    validation_seconds=(time.monotonic() - scope['validation_started']
                                        if 'validation_started' in scope else None),
                    snapshot_prepare_seconds=time.monotonic()-overall_started)


def reuse_or_build_index(snapshot: str | Path, pairs: Iterable[tuple[str, str]],
                         cache_dir: str | Path, expected_sha256: str) -> dict:
    """Reuse a completed index only for the exact snapshot and target IDs."""
    if not isinstance(expected_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None:
        raise ValueError('expected snapshot checksum is invalid')
    selected = list(pairs)
    ids = sorted(a for _, a in selected)
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate target ID')
    precheck_started = time.monotonic()
    _verified_file_sha256(snapshot, expected_sha256)
    precheck_seconds = time.monotonic() - precheck_started
    # Gắn phiên bản hợp đồng parser vào khóa bộ nhớ đệm. Phiên bản 2 chỉ nhận
    # ngày ISO và phân loại nhầm mọi bản ghi Kaggle thực tế có ngày RFC
    # là không hợp lệ; giữ nguyên các chỉ mục đó nhưng không dùng lại.
    key = hashlib.sha256(json.dumps([3, expected_sha256, ids], separators=(',', ':')).encode()).hexdigest()
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'candidates_{key}.json'
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data['schema_version'] == 2 and data['status'] == 'complete'
                    and data['snapshot_sha256'] == expected_sha256 and data['targets'] == ids
                    and isinstance(data['candidates'], dict) and isinstance(data['diagnostics'], dict)):
                return dict(index_path=str(path), reused=True, index=data,
                            snapshot_precheck_seconds=precheck_seconds,
                            index_checksum_seconds=None, index_scan_seconds=None,
                            checksum_in_scan=data.get('checksum_in_scan', False),
                            index_scanned_lines=None,
                            snapshot_size_bytes=data.get('snapshot_size_bytes'),
                            candidate_count=len(data.get('candidates', {})))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Giữ bộ nhớ đệm hỏng làm bằng chứng và từ chối ghi đè.
        raise ValueError('candidate index invalid')
    data = build_candidate_index(snapshot, selected, path, expected_sha256=expected_sha256)
    if data['snapshot_sha256'] != expected_sha256:
        raise ValueError('snapshot changed while scanning')
    return dict(index_path=str(path), reused=False, index=data,
                snapshot_precheck_seconds=precheck_seconds,
                index_checksum_seconds=data.get('checksum_seconds'),
                index_scan_seconds=data.get('scan_seconds'),
                checksum_in_scan=data.get('checksum_in_scan', False),
                index_scanned_lines=data.get('scanned_lines'),
                snapshot_size_bytes=data.get('snapshot_size_bytes'),
                candidate_count=len(data.get('candidates', {})))


def compare_candidate_to_discovery(candidate: dict | None, discovery: dict) -> dict:
    """Compare exact version coverage and endpoint dates at UTC calendar-day precision."""
    if (not isinstance(discovery, dict) or discovery.get('status') != 'found'
            or discovery.get('http_status') != 200
            or discovery.get('version_status') != 'resolved'):
        return dict(status='unusable', mismatches=['arxiv_discovery_unverified'])
    if not isinstance(candidate, dict):
        return dict(status='unusable', mismatches=['kaggle_record_missing_or_invalid'])
    version_ids, dates = candidate.get('version_ids'), candidate.get('dates')
    if (not isinstance(version_ids, list) or not version_ids
            or not isinstance(dates, list) or len(dates) != len(version_ids)
            or any(not isinstance(value, str) or _calendar_day_utc(value) != value for value in dates)
            or version_ids != [discovery['arxiv_id'] + f'v{n}' for n in range(1, len(version_ids) + 1)]):
        return dict(status='unusable', mismatches=['kaggle_record_invalid'])
    mismatches = []
    if discovery.get('version_ids') != version_ids:
        mismatches.append('version_ids')
    date_checks = []
    for label, arxiv_value, kaggle_value in (
        ('v1', discovery.get('published'), dates[0]),
        ('latest', discovery.get('updated'), dates[-1]),
    ):
        arxiv_day = _calendar_day_utc(arxiv_value)
        date_checks.append(dict(version=label, arxiv_day_utc=arxiv_day, kaggle_day_utc=kaggle_value))
        if arxiv_day is None:
            mismatches.append(f'{label}_arxiv_date_unavailable')
        elif arxiv_day != kaggle_value:
            mismatches.append(f'{label}_date')
    return dict(status='match' if not mismatches else 'discrepancy',
                mismatches=mismatches, date_checks=date_checks,
                comparison_precision='calendar_day_utc')


def persist_discrepancy_evidence(evidence: dict, *, evidence_root: str | Path,
                                 checkpoint_root: str | Path, session_id: str) -> dict:
    """Write immutable per-session JSON evidence and an atomic latest-ID pointer."""
    from src.storage import _atomic_json

    identifier = evidence.get('arxiv_id')
    if not isinstance(identifier, str) or not identifier or not re.fullmatch(r'[A-Za-z0-9./-]+', identifier):
        raise ValueError('invalid arXiv ID for discrepancy evidence')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
        raise ValueError('invalid session ID for discrepancy evidence')
    root = Path(evidence_root).expanduser().resolve()
    checkpoints = Path(checkpoint_root).expanduser().resolve()
    key = hashlib.sha256(identifier.encode('utf-8')).hexdigest()
    directory = root / key
    directory.mkdir(parents=True, exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    artifact = directory / f'{session_id}.json'
    sidecar = artifact.with_suffix('.json.sha256')
    payload = (json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n').encode('utf-8')
    digest = hashlib.sha256(payload).hexdigest()
    if artifact.exists() or sidecar.exists():
        if not artifact.is_file() or not sidecar.is_file() or artifact.read_bytes() != payload:
            raise FileExistsError('discrepancy artifact path already contains different or incomplete evidence')
        if sidecar.read_text(encoding='ascii').strip() != digest:
            raise ValueError('existing discrepancy checksum mismatch')
    else:
        _write_exclusive(artifact, payload)
        _write_exclusive(sidecar, (digest + '\n').encode('ascii'))
    pointer = dict(schema_version=1, arxiv_id=identifier, session_id=session_id,
                   artifact_path=str(artifact), sha256=digest,
                   updated_at=datetime.now(timezone.utc).isoformat())
    pointer_path = checkpoints / f'{key}.json'
    _atomic_json(pointer_path, pointer)
    return dict(**pointer, checkpoint_path=str(pointer_path))


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise


def validate_discrepancy_checkpoint(pointer_path: str | Path) -> dict:
    """Verify pointer, immutable JSON, and checksum sidecar before resume."""
    pointer_path = Path(pointer_path)
    value = json.loads(pointer_path.read_text(encoding='utf-8'))
    artifact = Path(value['artifact_path'])
    sidecar = artifact.with_suffix('.json.sha256')
    digest = file_sha256(artifact)
    if (value.get('schema_version') != 1 or digest != value.get('sha256')
            or sidecar.read_text(encoding='ascii').strip() != digest):
        raise ValueError(f'discrepancy evidence checksum mismatch: {pointer_path}')
    evidence = json.loads(artifact.read_text(encoding='utf-8'))
    if evidence.get('arxiv_id') != value.get('arxiv_id') or evidence.get('session_id') != value.get('session_id'):
        raise ValueError(f'discrepancy evidence identity mismatch: {pointer_path}')
    key = hashlib.sha256(str(evidence['arxiv_id']).encode('utf-8')).hexdigest()
    if pointer_path.stem != key or artifact.parent.name != key:
        raise ValueError(f'discrepancy evidence path mismatch: {pointer_path}')
    kaggle = evidence.get('kaggle')
    if isinstance(kaggle, dict):
        snapshot_path, index_path = kaggle.get('snapshot_path'), kaggle.get('index_path')
        snapshot_sha, index_sha = kaggle.get('snapshot_sha256'), kaggle.get('index_sha256')
        if (not isinstance(snapshot_path, str) or not isinstance(index_path, str)
                or not isinstance(snapshot_sha, str) or not isinstance(index_sha, str)):
            raise ValueError(f'discrepancy Kaggle provenance incomplete: {pointer_path}')
        _verified_file_sha256(snapshot_path, snapshot_sha)
        _verified_file_sha256(index_path, index_sha)
        index_resolved = str(Path(index_path).expanduser().resolve())
        cached_index = _VERIFIED_INDEXES.get(index_resolved)
        if cached_index is None:
            index = json.loads(Path(index_path).read_text(encoding='utf-8'))
            candidate = index.get('candidates', {}).get(evidence['arxiv_id'])
            diagnostic = kaggle.get('diagnostic')
            diagnostic_field = {"duplicate": "duplicate_ids", "invalid": "invalid_ids",
                                "missing": "missing_ids"}.get(diagnostic)
            record_matches = (
                isinstance(candidate, dict)
                and candidate.get('version_ids') == kaggle.get('version_ids')
                and candidate.get('dates') == kaggle.get('dates')
            ) or (
                candidate is None and diagnostic_field is not None
                and evidence['arxiv_id'] in index.get('diagnostics', {}).get(diagnostic_field, [])
                and kaggle.get('version_ids') is None and kaggle.get('dates') is None
            )
            if (index.get('schema_version') != 2 or index.get('status') != 'complete'
                    or index.get('snapshot_sha256') != snapshot_sha
                    or index.get('comparison_precision') != 'calendar_day_utc'
                    or not record_matches):
                raise ValueError(f'discrepancy index record mismatch: {pointer_path}')
            _VERIFIED_INDEXES[index_resolved] = index
        else:
            candidate = cached_index.get('candidates', {}).get(evidence['arxiv_id'])
            diagnostic_field = {"duplicate": "duplicate_ids", "invalid": "invalid_ids",
                                "missing": "missing_ids"}.get(kaggle.get('diagnostic'))
            record_matches = (
                isinstance(candidate, dict)
                and candidate.get('version_ids') == kaggle.get('version_ids')
                and candidate.get('dates') == kaggle.get('dates')
            ) or (
                candidate is None and diagnostic_field is not None
                and evidence['arxiv_id'] in cached_index.get('diagnostics', {}).get(diagnostic_field, [])
                and kaggle.get('version_ids') is None and kaggle.get('dates') is None
            )
            if cached_index.get('snapshot_sha256') != snapshot_sha or not record_matches:
                raise ValueError(f'discrepancy index record mismatch: {pointer_path}')
    return value


def validate_discrepancy_checkpoints(checkpoint_root: str | Path,
                                    evidence_root: str | Path | None = None) -> list[dict]:
    """Validate pointers and recover a missing pointer from durable orphan evidence."""
    root = Path(checkpoint_root)
    root.mkdir(parents=True, exist_ok=True)
    if evidence_root is not None:
        evidence_base = Path(evidence_root)
        from src.storage import _atomic_json
        for directory in sorted(evidence_base.iterdir()) if evidence_base.exists() else []:
            if not directory.is_dir():
                continue
            candidates = []
            for artifact in directory.glob('*.json'):
                sidecar = artifact.with_suffix('.json.sha256')
                if not sidecar.is_file():
                    continue
                digest = file_sha256(artifact)
                if sidecar.read_text(encoding='ascii').strip() != digest:
                    raise ValueError(f'discrepancy evidence checksum mismatch: {artifact}')
                evidence = json.loads(artifact.read_text(encoding='utf-8'))
                if hashlib.sha256(str(evidence.get('arxiv_id', '')).encode('utf-8')).hexdigest() != directory.name:
                    raise ValueError(f'discrepancy evidence ID/path mismatch: {artifact}')
                candidates.append((evidence.get('observed_at', ''), artifact, evidence, digest))
            if not candidates:
                continue
            _observed, artifact, evidence, digest = max(candidates, key=lambda item: item[0])
            pointer_path = root / f'{directory.name}.json'
            if not pointer_path.exists():
                _atomic_json(pointer_path, dict(
                    schema_version=1, arxiv_id=evidence['arxiv_id'],
                    session_id=evidence['session_id'], artifact_path=str(artifact),
                    sha256=digest,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    recovered_from_orphan=True,
                ))
    results = []
    for path in sorted(root.glob('*.json')):
        results.append(validate_discrepancy_checkpoint(path))
    return results
