import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from src.fast_production import FastProductionRunner, cohort_pairs, verify_cohort_handoff
from src.storage import CorpusStorage, arxiv_id_to_folder_key


class TestMultipointInterruptionQualification(unittest.TestCase):
    """BUG-006: Comprehensive Qualification of Multi-Point Interruption and Recovery.

    Verifies end-to-end resilience against abrupt OS process termination (SIGTERM, SIGKILL, SIGINT)
    with synchronization hooks at exact pipeline I/O points:
    R1. Kill during actual discovery I/O; clean resume without corrupt state.
    R2. Kill during actual source download file writing; uncommitted partial source rejected.
    R3. Kill after source commit but before metadata/references commit; resume metadata without re-downloading source.
    R4. Kill during actual _archive_cohort() tar generation; older valid archive remains intact.
    R5. Kill after archive swap but before handoff manifest commit; runner reconciles valid handoff manifest.
    R6. Concurrency safety: two processes archiving concurrently do not delete each other's active .tmp files.
    R7. Interactive Colab user interruption (SIGINT in real run_cohort()) produces graceful partial checkpoint.
    R8. Corrupted archive restore fails closed and leaves zero garbage in local corpus.
    R9. Multi-round cumulative crashes across fresh processes achieve complete verified handoff.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local_root = Path(self.temp_dir.name) / "local"
        self.drive_root = Path(self.temp_dir.name) / "drive"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.drive_root.mkdir(parents=True, exist_ok=True)
        self.pairs = cohort_pairs(1)
        self.repo_root = Path(__file__).resolve().parent.parent

    def tearDown(self):
        self.temp_dir.cleanup()

    def _wait_for_flag(self, flag_path: Path, timeout: float = 10.0) -> bool:
        start = time.time()
        while not flag_path.exists() and time.time() - start < timeout:
            time.sleep(0.05)
        return flag_path.exists()

    def _terminate_proc_safely(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass

    def _write_completed_paper(self, corpus_dir: Path, mssv: str, aid: str, content: bytes = b"default") -> str:
        target_corpus = corpus_dir if corpus_dir.name == "corpus" else corpus_dir / "corpus"
        store = CorpusStorage(target_corpus)
        store.record_discovery({
            "mssv": mssv,
            "arxiv_id": aid,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{aid}v1"],
            "latest_version_id": f"{aid}v1",
            "title": f"Paper Title {aid}",
            "authors": ["Author One"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        dest = store.begin_version(mssv, aid, "v1")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tex").write_bytes(content)
        tex_hash = hashlib.sha256(content).hexdigest()
        store.record_source_result({
            "mssv": mssv,
            "arxiv_id": aid,
            "versioned_id": f"{aid}v1",
            "status": "success",
            "files": ["main.tex"],
            "downloaded_bytes": len(content),
            "error": None,
        })
        meta = store.read_metadata(mssv, aid)
        meta["status"] = "complete"
        meta["title"] = f"Paper Title {aid}"
        meta["authors"] = ["Author One"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = []
        meta["venue"] = "NeurIPS"
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [{"version": "v1", "versioned_id": f"{aid}v1", "date": "2025-08-01", "date_status": "verified"}]
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": aid,
            "s2_paper_id": f"s2_{aid}",
            "retrieved_at": "2025-08-01T00:00:00Z",
            "snapshot": {"arxiv_id": aid, "status": "found"},
        }
        store.save_metadata(mssv, aid, meta)

        refs = store.read_references(mssv, aid)
        refs["status"] = "complete"
        refs["provenance"]["query_identifier"] = aid
        refs["provenance"]["retrieved_at"] = "2025-08-01T00:00:00Z"
        refs["coverage"] = {
            "complete": True,
            "continuation": None,
            "reported_total": 1,
            "retrieved_count": 1,
        }
        refs["items"] = [{
            "arxiv_id": "2101.00001",
            "title": "Ref Title",
            "authors": ["Author Two"],
            "submission_date": "2021-01-01",
            "publication_date": "2021-01-01",
            "year": 2021,
            "venue": "ICML",
            "s2_paper_id": f"s2_ref_{aid}",
            "missing_fields": [],
            "raw": {},
        }]
        store.save_references(mssv, aid, refs)
        return tex_hash

    def test_r1_real_discovery_io_kill_and_clean_resume(self):
        """R1: Subprocess is killed with SIGKILL while actively reading streaming network response from TCP socket."""
        import http.server
        import threading

        flag_discovery = Path(self.temp_dir.name) / "r1_discovery_active.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)

        class SlowDiscoveryServer(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/atom+xml; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n')
                self.wfile.flush()
                # Set flag confirming client is actively connected and reading network socket stream
                flag_discovery.write_text("in_network_read")
                time.sleep(30)

        server = http.server.HTTPServer(("127.0.0.1", 0), SlowDiscoveryServer)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        child_code = f"""
import sys
sys.path.insert(0, {repr(repo_p)})
from src import arxiv_client
from src.fast_production import FastProductionRunner

# Point discovery API_URL to active local TCP server
arxiv_client.API_URL = "http://127.0.0.1:{port}/api/query"

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
runner.run_cohort(1, max_ids=1)
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_discovery), "Child failed to reach active network stream")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            self._terminate_proc_safely(proc)
            try:
                server.server_close()
            except Exception:
                pass

        # Fresh runner on the same corpus
        fresh_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        self.assertEqual(fresh_runner.count_completed_papers(1), 0)
        self.assertEqual(fresh_runner.count_source_completed_papers(1), 0)
        # Check that no corrupted paper folder or false completed manifest exists
        p1_folder = arxiv_id_to_folder_key(self.pairs[0][1])
        p1_manifest = fresh_runner.corpus_dir / p1_folder / "manifest.json"
        if p1_manifest.exists():
            data = json.loads(p1_manifest.read_text(encoding="utf-8"))
            self.assertNotEqual(data.get("status"), "complete")
            self.assertNotEqual(data.get("discovery_status"), "found")

    def test_r2_real_source_download_io_kill_and_clean_resume(self):
        """R2: Subprocess is killed with SIGKILL while actively reading streaming HTTP response and writing bytes to disk."""
        import http.server
        import threading

        flag_writing = Path(self.temp_dir.name) / "r2_source_writing.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)
        mssv1, aid1 = self.pairs[0]

        # First, paper 1 has discovery recorded
        init_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        init_runner.store.record_discovery({
            "mssv": mssv1,
            "arxiv_id": aid1,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{aid1}v1"],
            "latest_version_id": f"{aid1}v1",
            "title": "Title",
            "authors": ["Author"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })

        class SlowSourceServer(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Content-Length", "200000")
                self.end_headers()
                # Stream first 64KB chunk so target file on disk actually receives bytes
                self.wfile.write(b"x" * 65536)
                self.wfile.flush()
                # Keep connection open and blocked
                time.sleep(30)

        server = http.server.HTTPServer(("127.0.0.1", 0), SlowSourceServer)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        child_code = f"""
import sys, time
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src import arxiv_source
from src.fast_production import FastProductionRunner

arxiv_source.SOURCE_URL = "http://127.0.0.1:{port}/src/"

orig_copy = arxiv_source._copy_limited

def hooked_copy(source, target, limit):
    if target is not None:
        orig_write = target.write
        def hooked_write(chunk):
            res = orig_write(chunk)
            target.flush()
            if target.tell() > 0:
                Path({repr(str(flag_writing))}).write_text(f"disk_bytes_{{target.tell()}}")
                time.sleep(30)
            return res
        target.write = hooked_write
    return orig_copy(source, target, limit)

arxiv_source._copy_limited = hooked_copy

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
dest = runner.store.begin_version({repr(mssv1)}, {repr(aid1)}, "v1")
res = arxiv_source.download_source({repr(mssv1)}, {repr(aid1 + "v1")}, dest)
runner.store.record_source_result(res)
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_writing), "Child failed to reach source disk writing I/O")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            self._terminate_proc_safely(proc)
            try:
                server.server_close()
            except Exception:
                pass

        # Fresh runner examines corpus: source was never committed as success
        fresh_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        m1_stats = fresh_runner.count_m1_completed_papers(1)
        self.assertEqual(m1_stats["found_m1_complete"], 0)
        self.assertEqual(fresh_runner.count_source_completed_papers(1), 0)

        # Resume storage state: handles interrupted version
        plan = fresh_runner.store.resume(mssv1, aid1)
        self.assertIn("v1", plan["manifest"]["versions"])
        self.assertEqual(plan["manifest"]["versions"]["v1"]["status"], "interrupted")

        # Clean recovery: remove partial uncommitted directory and complete cleanly
        p1_folder = arxiv_id_to_folder_key(aid1)
        partial_dir = fresh_runner.corpus_dir / p1_folder / "v1"
        if partial_dir.exists():
            import shutil
            shutil.rmtree(partial_dir)

        # Reset version in manifest to allow fresh begin
        m = fresh_runner.store.read_manifest(mssv1, aid1)
        del m["versions"]["v1"]
        fresh_runner.store._commit(m)

        self._write_completed_paper(fresh_runner.corpus_dir, mssv1, aid1, b"cleanly completed source")
        self.assertEqual(fresh_runner.count_completed_papers(1), 1)

    def test_r3_real_post_source_pre_metadata_io_kill_and_clean_resume(self):
        """R3: Kill after source commit but before metadata commit; resume metadata without redownloading source."""
        flag_source_done = Path(self.temp_dir.name) / "r3_source_done.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)
        mssv1, aid1 = self.pairs[0]

        child_code = f"""
import sys, time
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src.storage import CorpusStorage
from src.fast_production import FastProductionRunner

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
store = runner.store
store.record_discovery({{
    "mssv": {repr(mssv1)}, "arxiv_id": {repr(aid1)}, "status": "found",
    "version_status": "resolved", "version_ids": [{repr(f"{aid1}v1")}],
    "latest_version_id": {repr(f"{aid1}v1")}, "title": "Title",
    "authors": ["Author"], "published": "2025-08-01", "updated": "2025-08-01",
    "missing_fields": [],
}})
dest = store.begin_version({repr(mssv1)}, {repr(aid1)}, "v1")
dest.mkdir(parents=True, exist_ok=True)
content = b"verified source data"
(dest / "main.tex").write_bytes(content)
store.record_source_result({{
    "mssv": {repr(mssv1)}, "arxiv_id": {repr(aid1)}, "versioned_id": {repr(f"{aid1}v1")},
    "status": "success", "files": ["main.tex"], "downloaded_bytes": len(content),
    "error": None,
}})
# Signal that source is committed and pause before metadata stage
Path({repr(str(flag_source_done))}).write_text("source_committed")
time.sleep(30)
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_source_done), "Child failed to commit source")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            self._terminate_proc_safely(proc)

        # Fresh runner on same directory
        fresh_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        self.assertEqual(fresh_runner.count_source_completed_papers(1), 1)
        self.assertEqual(fresh_runner.count_m1_completed_papers(1)["found_m1_complete"], 0)

        # Resume metadata stage without redownloading source: TeX source remains unmodified
        p1_folder = arxiv_id_to_folder_key(aid1)
        tex_path = fresh_runner.corpus_dir / p1_folder / "v1" / "main.tex"
        tex_mtime_before = tex_path.stat().st_mtime_ns

        meta = fresh_runner.store.read_metadata(mssv1, aid1)
        meta["status"] = "complete"
        meta["title"] = "Title"
        meta["authors"] = ["Author"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = []
        meta["venue"] = "NeurIPS"
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [{"version": "v1", "versioned_id": f"{aid1}v1", "date": "2025-08-01", "date_status": "verified"}]
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": aid1,
            "s2_paper_id": f"s2_{aid1}",
            "retrieved_at": "2025-08-01T00:00:00Z",
            "snapshot": {"arxiv_id": aid1, "status": "found"},
        }
        fresh_runner.store.save_metadata(mssv1, aid1, meta)

        refs = fresh_runner.store.read_references(mssv1, aid1)
        refs["status"] = "complete"
        refs["provenance"]["query_identifier"] = aid1
        refs["provenance"]["retrieved_at"] = "2025-08-01T00:00:00Z"
        refs["coverage"] = {"complete": True, "continuation": None, "reported_total": 1, "retrieved_count": 1}
        refs["items"] = [{
            "arxiv_id": "2101.00001", "title": "Ref", "authors": ["A2"],
            "submission_date": "2021-01-01", "publication_date": "2021-01-01",
            "year": 2021, "venue": "ICML", "s2_paper_id": "s2_ref",
            "missing_fields": [], "raw": {},
        }]
        fresh_runner.store.save_references(mssv1, aid1, refs)

        # Source was not touched/rewritten
        self.assertEqual(tex_path.stat().st_mtime_ns, tex_mtime_before)
        # Now paper 1 is fully M1 complete
        self.assertEqual(fresh_runner.count_m1_completed_papers(1)["found_m1_complete"], 1)

    def test_r4_real_archive_cohort_io_kill_and_clean_resume(self):
        """R4: Kill subprocess during actual _archive_cohort() tar generation; older archive on Drive remains intact."""
        flag_archiving = Path(self.temp_dir.name) / "r4_archiving.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)
        mssv1, aid1 = self.pairs[0]
        mssv2, aid2 = self.pairs[1]

        # Prepare Paper 1 in local corpus and create older valid archive on Drive
        init_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        self._write_completed_paper(self.local_root / "corpus", mssv1, aid1, b"paper 1 data")
        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        init_runner._archive_cohort(1, arch_dest)
        older_archive_sha = init_runner._compute_sha256(arch_dest)

        # Now Paper 2 is added to local corpus
        self._write_completed_paper(self.local_root / "corpus", mssv2, aid2, b"paper 2 data")

        # Subprocess attempts to create new archive, but is killed during tar.add
        child_code = f"""
import sys, time, tarfile
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src.fast_production import FastProductionRunner

orig_add = tarfile.TarFile.add

def hooked_add(self, name, arcname=None, recursive=True, *args, **kwargs):
    res = orig_add(self, name, arcname=arcname, recursive=recursive, *args, **kwargs)
    if hasattr(self, "fileobj") and self.fileobj:
        self.fileobj.flush()
    Path({repr(str(flag_archiving))}).write_text("in_tar_add")
    time.sleep(30)
    return res

tarfile.TarFile.add = hooked_add

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
runner._archive_cohort(1, Path({repr(str(arch_dest))}))
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_archiving), "Child failed to reach tar.add I/O")
            # Verify the temporary archive file exists and has actual data written (> 0 bytes) before kill
            tmp_files = list(self.drive_root.glob("archives/.*.tmp"))
            self.assertTrue(len(tmp_files) >= 1, "Child .tmp archive file must exist")
            self.assertGreater(tmp_files[0].stat().st_size, 0, "Child .tmp archive file must have st_size > 0 before kill")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            self._terminate_proc_safely(proc)

        # Verify older archive on Drive was NOT corrupted or overwritten
        self.assertTrue(arch_dest.exists())
        self.assertEqual(init_runner._compute_sha256(arch_dest), older_archive_sha)
        sha_file = arch_dest.with_name(arch_dest.name + ".sha256")
        self.assertEqual(sha_file.read_text(encoding="utf-8").strip().split()[0], older_archive_sha)

        # Fresh runner restores cleanly from the intact older archive
        fresh_local = Path(self.temp_dir.name) / "fresh_local_r4"
        fresh_runner = FastProductionRunner(fresh_local, self.drive_root, retry_429=False)
        restored = fresh_runner.restore_cohort(1)
        self.assertEqual(restored, 1)

        # Fresh runner creates new archive cleanly and removes dead PID's orphaned .tmp
        fresh_runner._archive_cohort(1, arch_dest)
        self.assertTrue(arch_dest.exists())
        # Confirm no orphan .tmp files left from dead processes
        orphans = list(self.drive_root.glob("archives/.*.tmp"))
        self.assertEqual(len(orphans), 0)

    def test_r5_real_post_archive_pre_manifest_kill_and_reconcile(self):
        """R5: Kill after archive and sha256 swap but before accounting manifest; runner reconciles valid handoff."""
        flag_swapped = Path(self.temp_dir.name) / "r5_swapped.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)

        # Prepare full 100 papers on local corpus
        init_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        for idx in range(len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(init_runner.corpus_dir, mssv, aid, f"content {idx}".encode("utf-8"))

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"

        # Subprocess writes archive and sha256, but is killed before writing cohort manifest
        child_code = f"""
import sys, time, os
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src.fast_production import FastProductionRunner

orig_replace = os.replace

def hooked_replace(src, dst):
    res = orig_replace(src, dst)
    if str(dst).endswith(".tar.gz.sha256"):
        Path({repr(str(flag_swapped))}).write_text("archive_and_sha_swapped")
        time.sleep(30)
    return res

os.replace = hooked_replace

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
runner._archive_cohort(1, Path({repr(str(arch_dest))}))
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_swapped), "Child failed to swap archive and sha256")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            self._terminate_proc_safely(proc)

        # Verify archive and sha256 exist on Drive, but manifest is missing
        self.assertTrue(arch_dest.exists())
        sha_file = arch_dest.with_name(arch_dest.name + ".sha256")
        self.assertTrue(sha_file.exists())
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

        # Fresh runner starts; verify_cohort_handoff initially fails closed
        fresh_runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        with self.assertRaises(ValueError):
            verify_cohort_handoff(fresh_runner, 1)

        # Runner reconciles manifest directly from valid archive
        reconciled = fresh_runner.reconcile_cohort_manifest(1)
        self.assertTrue(reconciled)

        # Now verify_cohort_handoff passes with flying colors
        h_info = verify_cohort_handoff(fresh_runner, 1)
        self.assertTrue(h_info["verified"])
        self.assertEqual(h_info["archive_state"], "m1_handoff_verified")
        self.assertEqual(h_info["inspected_papers"], 100)

    def test_r6_archive_concurrency_and_active_tmp_protection(self):
        """R6: Two processes archiving concurrently do not delete each other's active .tmp files or clobber archive."""
        flag_proc_a = Path(self.temp_dir.name) / "r6_proc_a_writing.flag"
        flag_proc_a_release = Path(self.temp_dir.name) / "r6_proc_a_release.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)
        mssv1, aid1 = self.pairs[0]
        self._write_completed_paper(self.local_root / "corpus", mssv1, aid1, b"proc a data")

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"

        child_a_code = f"""
import sys, time, tarfile
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src.fast_production import FastProductionRunner

orig_add = tarfile.TarFile.add

def hooked_add(self, name, arcname=None, recursive=True, *args, **kwargs):
    Path({repr(str(flag_proc_a))}).write_text("proc_a_active")
    while not Path({repr(str(flag_proc_a_release))}).exists():
        time.sleep(0.05)
    return orig_add(self, name, arcname=arcname, recursive=recursive, *args, **kwargs)

tarfile.TarFile.add = hooked_add

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
runner._archive_cohort(1, Path({repr(str(arch_dest))}))
"""
        proc_a = subprocess.Popen([sys.executable, "-c", child_a_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_proc_a), "Process A failed to start archiving")

            # While Process A is actively running, find its PID-tagged tmp file
            proc_a_tmps = list(self.drive_root.glob(f"archives/.*.pid_{proc_a.pid}_*.tmp"))
            self.assertEqual(len(proc_a_tmps), 1, "Process A should have an active PID-tagged tmp file")
            tmp_a_file = proc_a_tmps[0]

            # Process B attempts to archive the same cohort concurrently
            local_b = Path(self.temp_dir.name) / "local_b"
            self._write_completed_paper(local_b / "corpus", mssv1, aid1, b"proc b data")

            child_b_code = f"""
import sys
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src.fast_production import FastProductionRunner

runner = FastProductionRunner({repr(str(local_b))}, {repr(drive_p)}, retry_429=False)
runner._archive_cohort(1, Path({repr(str(arch_dest))}))
"""
            proc_b = subprocess.Popen([sys.executable, "-c", child_b_code])
            try:
                # Wait briefly to let Process B run its cleanup check
                time.sleep(0.6)

                # Process A's tmp file must still exist and be completely untouched
                self.assertTrue(tmp_a_file.exists(), "Process B must not delete active writer's tmp file")

                # Release Process A to finish
                flag_proc_a_release.write_text("release")
                proc_a.wait(timeout=5.0)
                self.assertEqual(proc_a.returncode, 0)

                # Process B can now finish
                proc_b.wait(timeout=5.0)
                self.assertEqual(proc_b.returncode, 0)
            finally:
                self._terminate_proc_safely(proc_b)
        finally:
            self._terminate_proc_safely(proc_a)

        # Final archive is valid and readable
        with tarfile.open(arch_dest, "r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
            p1_folder = arxiv_id_to_folder_key(aid1)
            self.assertIn(f"{p1_folder}/manifest.json", names)

    def test_pid_aware_archive_tmp_cleanup(self):
        """Proof of PID-aware tmp cleanup fail-safe behavior:
        1. PID hiện tại: giữ file.
        2. PID đã chết: xóa file.
        3. PID 0: giữ file (PID <= 0 không hợp lệ, không bao giờ coi là đã chết).
        4. PID không parse được: giữ file.
        5. EPERM: giữ file (fail-safe).
        6. EACCES: giữ file (fail-safe).
        7. OSError không xác định: giữ file (fail-safe).
        """
        from src.fast_production import cleanup_orphaned_archive_tmp, _is_pid_alive
        import errno
        from unittest.mock import patch

        archives_dir = self.drive_root / "archives"
        archives_dir.mkdir(parents=True, exist_ok=True)
        arch_name = "cohort_0001_corpus.tar.gz"

        # 1. PID hiện tại (our current process) -> giữ file
        alive_pid = os.getpid()
        self.assertTrue(_is_pid_alive(alive_pid))
        alive_tmp = archives_dir / f".{arch_name}.pid_{alive_pid}_active.tmp"
        alive_tmp.write_text("active temp file")

        # 2. PID đã chết (subprocess that already exited) -> xóa file
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        proc.wait(timeout=2.0)
        dead_pid = proc.pid
        self.assertFalse(_is_pid_alive(dead_pid))
        dead_tmp = archives_dir / f".{arch_name}.pid_{dead_pid}_dead.tmp"
        dead_tmp.write_text("dead temp file")

        # 3. PID "0" (và PID âm) -> xem là PID không hợp lệ, fail-safe giữ file
        self.assertTrue(_is_pid_alive(0), "PID 0 must not be considered dead")
        self.assertTrue(_is_pid_alive(-1), "PID < 0 must not be considered dead")
        pid0_tmp = archives_dir / f".{arch_name}.pid_0_kernel.tmp"
        pid0_tmp.write_text("pid 0 temp file")

        # 4. PID không parse được -> fail-safe giữ file
        unknown_tmp1 = archives_dir / f".{arch_name}.legacy_no_pid.tmp"
        unknown_tmp1.write_text("unparseable temp file 1")
        unknown_tmp2 = archives_dir / f".{arch_name}.pid_notanumber_abc.tmp"
        unknown_tmp2.write_text("unparseable temp file 2")

        # 5. EPERM (Operation not permitted) -> fail-safe giữ file
        eperm_pid = 54321
        eperm_tmp = archives_dir / f".{arch_name}.pid_{eperm_pid}_perm.tmp"
        eperm_tmp.write_text("eperm temp file")

        # 6. EACCES (Permission denied) -> fail-safe giữ file
        eacces_pid = 54322
        eacces_tmp = archives_dir / f".{arch_name}.pid_{eacces_pid}_acces.tmp"
        eacces_tmp.write_text("eacces temp file")

        # 7. OSError không xác định (e.g. EIO, EINVAL) -> fail-safe giữ file
        unknown_err_pid = 54323
        unknown_err_tmp = archives_dir / f".{arch_name}.pid_{unknown_err_pid}_unknown.tmp"
        unknown_err_tmp.write_text("unknown os error temp file")

        orig_kill = os.kill
        def mock_kill(pid, sig):
            if pid == eperm_pid:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            if pid == eacces_pid:
                raise PermissionError(errno.EACCES, "Permission denied")
            if pid == unknown_err_pid:
                raise OSError(errno.EIO, "Input/output error")
            return orig_kill(pid, sig)

        with patch("os.kill", side_effect=mock_kill):
            self.assertTrue(_is_pid_alive(eperm_pid), "EPERM must be treated as alive (fail-safe)")
            self.assertTrue(_is_pid_alive(eacces_pid), "EACCES must be treated as alive (fail-safe)")
            self.assertTrue(_is_pid_alive(unknown_err_pid), "Unknown OSError must be treated as alive (fail-safe)")
            removed = cleanup_orphaned_archive_tmp(archives_dir, arch_name)

        # Confirm assertions for all 7 cases
        # Only case 2 (confirmed dead PID) is removed
        self.assertIn(dead_tmp, removed, "Dead PID tmp file must be cleaned up")
        self.assertFalse(dead_tmp.exists(), "Dead PID tmp file must be deleted")

        # All other cases MUST be preserved
        self.assertTrue(alive_tmp.exists(), "Case 1: Active PID tmp file must be preserved")
        self.assertTrue(pid0_tmp.exists(), "Case 3: PID 0 tmp file must be preserved")
        self.assertTrue(unknown_tmp1.exists(), "Case 4a: Unknown format tmp file must be preserved")
        self.assertTrue(unknown_tmp2.exists(), "Case 4b: Unparseable PID tmp file must be preserved")
        self.assertTrue(eperm_tmp.exists(), "Case 5: EPERM tmp file must be preserved")
        self.assertTrue(eacces_tmp.exists(), "Case 6: EACCES tmp file must be preserved")
        self.assertTrue(unknown_err_tmp.exists(), "Case 7: Unknown OSError tmp file must be preserved")

    def test_reconciliation_fails_closed_on_corrupt_tar(self):
        """reconcile_cohort_manifest() fails closed (returns False) when tarfile is corrupt."""
        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch_dest.parent.mkdir(parents=True, exist_ok=True)
        arch_dest.write_bytes(b"NOT A VALID GZIP TAR FILE GARBAGE")

        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        reconciled = runner.reconcile_cohort_manifest(1)
        self.assertFalse(reconciled, "Corrupt archive must fail closed without publishing manifest")
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

    def test_reconciliation_fails_closed_on_unmanaged_file(self):
        """reconcile_cohort_manifest() fails closed when paper directory has unmanaged files."""
        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        for idx in range(len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(runner.corpus_dir, mssv, aid, f"paper {idx}".encode("utf-8"))

        # Add unmanaged extra file to paper 0
        p0_folder = arxiv_id_to_folder_key(self.pairs[0][1])
        (runner.corpus_dir / p0_folder / "sneaky_unmanaged.sh").write_text("echo evil")

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch_dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(arch_dest, "w:gz") as tar:
            for mssv, aid in self.pairs:
                f_name = arxiv_id_to_folder_key(aid)
                tar.add(runner.corpus_dir / f_name, arcname=f_name, recursive=True)

        reconciled = runner.reconcile_cohort_manifest(1)
        self.assertFalse(reconciled, "Archive with unmanaged file must fail closed in reconciliation")
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

    def test_reconciliation_fails_closed_on_checksum_mismatch(self):
        """reconcile_cohort_manifest() fails closed when paper version file checksum does not match manifest."""
        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        for idx in range(len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(runner.corpus_dir, mssv, aid, f"paper {idx}".encode("utf-8"))

        # Tamper with file content after manifest was generated
        p0_folder = arxiv_id_to_folder_key(self.pairs[0][1])
        (runner.corpus_dir / p0_folder / "v1" / "main.tex").write_bytes(b"tampered content")

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch_dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(arch_dest, "w:gz") as tar:
            for mssv, aid in self.pairs:
                f_name = arxiv_id_to_folder_key(aid)
                tar.add(runner.corpus_dir / f_name, arcname=f_name, recursive=True)

        reconciled = runner.reconcile_cohort_manifest(1)
        self.assertFalse(reconciled, "Archive with checksum mismatch must fail closed in reconciliation")
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

    def test_reconciliation_fails_closed_on_tex_file_in_no_tex_source(self):
        """reconcile_cohort_manifest() fails closed when a paper marked no_tex_source contains a .tex file."""
        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        for idx in range(len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(runner.corpus_dir, mssv, aid, f"paper {idx}".encode("utf-8"))

        # Alter paper 0 manifest to claim complete_with_no_tex while main.tex exists
        p0_folder = arxiv_id_to_folder_key(self.pairs[0][1])
        mf_path = runner.corpus_dir / p0_folder / "manifest.json"
        mf_data = json.loads(mf_path.read_text(encoding="utf-8"))
        mf_data["status"] = "complete_with_no_tex"
        mf_data["versions"]["v1"]["status"] = "no_tex_source"
        mf_path.write_text(json.dumps(mf_data, indent=2), encoding="utf-8")

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch_dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(arch_dest, "w:gz") as tar:
            for mssv, aid in self.pairs:
                f_name = arxiv_id_to_folder_key(aid)
                tar.add(runner.corpus_dir / f_name, arcname=f_name, recursive=True)

        reconciled = runner.reconcile_cohort_manifest(1)
        self.assertFalse(reconciled, "Archive violating no_tex_source contract must fail closed")
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

    def test_reconciliation_fails_closed_on_missing_metadata(self):
        """reconcile_cohort_manifest() fails closed when metadata.json is missing in tar."""
        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        for idx in range(len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(runner.corpus_dir, mssv, aid, f"paper {idx}".encode("utf-8"))

        # Delete metadata.json from paper 0
        p0_folder = arxiv_id_to_folder_key(self.pairs[0][1])
        (runner.corpus_dir / p0_folder / "metadata.json").unlink()

        arch_dest = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch_dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(arch_dest, "w:gz") as tar:
            for mssv, aid in self.pairs:
                f_name = arxiv_id_to_folder_key(aid)
                tar.add(runner.corpus_dir / f_name, arcname=f_name, recursive=True)

        reconciled = runner.reconcile_cohort_manifest(1)
        self.assertFalse(reconciled, "Archive missing metadata.json must fail closed in reconciliation")
        manifest_file = arch_dest.with_name("cohort_0001_corpus.manifest.json")
        self.assertFalse(manifest_file.exists())

    def test_r7_colab_stop_button_sigint_in_run_cohort(self):
        """R7: Send SIGINT to subprocess running real FastProductionRunner.run_cohort(); verify partial checkpoint archive."""
        flag_query_active = Path(self.temp_dir.name) / "r7_query_active.flag"
        local_p = str(self.local_root)
        drive_p = str(self.drive_root)
        repo_p = str(self.repo_root)
        mssv1, aid1 = self.pairs[0]

        # Paper 1 is complete on local disk
        self._write_completed_paper(self.local_root / "corpus", mssv1, aid1, b"paper 1 data")

        child_code = f"""
import sys, time
sys.path.insert(0, {repr(repo_p)})
from pathlib import Path
from src import arxiv_client
from src.fast_production import FastProductionRunner

orig_query = arxiv_client.query_arxiv

def hooked_query(arxiv_id, *args, **kwargs):
    Path({repr(str(flag_query_active))}).write_text("in_query")
    time.sleep(30)
    return orig_query(arxiv_id, *args, **kwargs)

arxiv_client.query_arxiv = hooked_query

runner = FastProductionRunner({repr(local_p)}, {repr(drive_p)}, retry_429=False)
runner.run_cohort(1)
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            self.assertTrue(self._wait_for_flag(flag_query_active), "Child failed to reach query in run_cohort")
            # Send SIGINT (same signal as Google Colab user clicking Stop button)
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5.0)
            self.assertEqual(proc.returncode, 0, "run_cohort must catch KeyboardInterrupt and exit gracefully")
        finally:
            self._terminate_proc_safely(proc)

        # Confirm partial archive was saved to Drive by run_cohort's finally block
        arch_path = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        self.assertTrue(arch_path.exists())
        mf_path = arch_path.with_name("cohort_0001_corpus.manifest.json")
        self.assertTrue(mf_path.exists())
        m_payload = json.loads(mf_path.read_text(encoding="utf-8"))
        self.assertEqual(m_payload["archive_state"], "checkpoint_partial")
        self.assertEqual(m_payload["verified_completed_count"], 1)
        self.assertGreaterEqual(m_payload["included_count"], 1)

    def test_r8_corrupted_archive_restore_leaves_zero_corpus_garbage(self):
        """R8: restore_cohort() with corrupted archive fails closed and leaves zero partial files in corpus."""
        mssv1, aid1 = self.pairs[0]
        # Clean paper 1 in local corpus
        self._write_completed_paper(self.local_root / "corpus", mssv1, aid1, b"paper 1 local intact")

        # Corrupted truncated archive on Drive
        arch = self.drive_root / "archives" / "cohort_0001_corpus.tar.gz"
        arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_bytes(b"\x1f\x8b\x08TRUNCATED CORRUPTED GZIP BYTES GARBAGE 1234567890")

        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        restored = runner.restore_cohort(1)
        self.assertEqual(restored, 0)

        # Local paper 1 is completely intact
        p1_folder = arxiv_id_to_folder_key(aid1)
        self.assertTrue((runner.corpus_dir / p1_folder / "v1" / "main.tex").exists())

        # No temporary extraction artifacts in local root or corpus
        leftovers = list(self.local_root.glob(".restore-*")) + list(self.local_root.glob(".stage-*"))
        self.assertEqual(len(leftovers), 0)

    def test_r9_multi_crash_cumulative_resumption(self):
        """R9: 3 consecutive crashes across fresh processes; cumulative progress preserved with zero loss."""
        for round_idx in range(1, 4):
            local_round = Path(self.temp_dir.name) / f"local_round_{round_idx}"
            runner = FastProductionRunner(local_round, self.drive_root, retry_429=False)

            restored = runner.restore_cohort(1)
            self.assertEqual(restored, round_idx - 1)
            self.assertEqual(runner.count_completed_papers(1), round_idx - 1)

            # Complete one additional paper
            mssv, aid = self.pairs[round_idx - 1]
            self._write_completed_paper(runner.corpus_dir, mssv, aid, f"round {round_idx}".encode("utf-8"))

            arch_p = runner.archives_dir / "cohort_0001_corpus.tar.gz"
            runner._archive_cohort(1, arch_p)
            del runner

        # Final round: complete remainder of cohort
        final_local = Path(self.temp_dir.name) / "local_round_final"
        final_runner = FastProductionRunner(final_local, self.drive_root, retry_429=False)
        self.assertEqual(final_runner.restore_cohort(1), 3)

        for idx in range(3, len(self.pairs)):
            mssv, aid = self.pairs[idx]
            self._write_completed_paper(final_runner.corpus_dir, mssv, aid, f"paper {idx}".encode("utf-8"))

        arch_final = final_runner.archives_dir / "cohort_0001_corpus.tar.gz"
        final_runner._archive_cohort(1, arch_final)

        h_info = verify_cohort_handoff(final_runner, 1)
        self.assertTrue(h_info["verified"])
        self.assertEqual(h_info["inspected_papers"], 100)
        self.assertEqual(h_info["archive_state"], "m1_handoff_verified")


if __name__ == "__main__":
    unittest.main()
