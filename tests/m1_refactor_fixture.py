"""Run one deterministic M1 characterization fixture in a fresh interpreter.

Only test inputs are fixed: clocks, UUIDs, PID, filesystem metadata, resource
readers and temporary names. Persisted bytes and checksums are never normalized.
"""
from __future__ import annotations

import argparse
import base64
import datetime as datetime_module
import importlib
import io
import itertools
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import uuid
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from functools import wraps
from pathlib import Path
from unittest.mock import patch

BASELINE_COMMIT = "75b029cc5a39e3a4f7174c2e38ff3cf2212e67e3"
REPO_ROOT = Path(__file__).resolve().parents[1]
EPOCH = 1794432000


def baseline_module() -> types.ModuleType:
    """Load the immutable pre-refactor implementation without checking it out."""
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_COMMIT}:src/m1_phases.py"],
        cwd=REPO_ROOT, text=True,
    )
    module = types.ModuleType("m1_phases_baseline")
    exec(compile(source, "m1_phases_baseline.py", "exec"), module.__dict__)
    return module


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(EPOCH, tz=tz)


def run_fixture(implementation: str, case: str, workspace: Path) -> dict:
    """Execute an existing behavioral assertion and retain its exact artifacts."""
    import test_m1_phases as fixtures

    from src import m1_phases, session_evidence
    from src.benchmark_metrics import ResourceSampler
    from src.fast_production import FastProductionRunner
    from src.storage import _atomic_json

    # Import lazy providers before replacing their datetime bindings.
    for name in (
        "archive_validation", "bbl_handling", "export", "kaggle_candidates",
        "reporting", "s2_integration", "storage_integrity",
    ):
        importlib.import_module("src." + name)
    baseline = baseline_module() if implementation in {"baseline", "mixed"} else None
    module = baseline if implementation == "baseline" else m1_phases
    if implementation == "mixed":
        module = types.ModuleType("m1_phases_mixed")
        module.__dict__.update({k: v for k, v in vars(m1_phases).items()
                                if not k.startswith("__")})
        s2_calls = 0

        @wraps(m1_phases.run_m1_s2_member)
        def resume_s2(*args, **kwargs):
            nonlocal s2_calls
            provider = baseline if s2_calls == 0 else m1_phases
            s2_calls += 1
            return provider.run_m1_s2_member(*args, **kwargs)

        module.run_m1_s2_member = resume_s2
    fixture_class, method = case.split(".")
    test = getattr(fixtures, fixture_class)(method)
    original_init = FastProductionRunner.__init__
    original_tarinfo = tarfile.TarFile.gettarinfo
    names, identifiers = itertools.count(), itertools.count(1)

    class FixedSampler:
        def __init__(self, root, **kwargs):
            self.sampler = ResourceSampler(
                root, ram_reader=lambda: 1024, system_ram_reader=lambda: 4096,
                disk_reader=lambda _root: 2048, **kwargs,
            )

        def start(self):
            self.sampler.sample_disk()
            self.sampler.sample_ram()

        def stop(self):
            self.sampler.sample_ram()
            self.sampler.sample_disk(final=True)

        def report(self):
            return self.sampler.report()

    def isolated_init(self, *args, **kwargs):
        local = kwargs.get("local_root", args[0] if args else None)
        kwargs.setdefault("scratch_root", Path(local) / "scratch")
        return original_init(self, *args, **kwargs)

    def fixed_tarinfo(self, *args, **kwargs):
        info = original_tarinfo(self, *args, **kwargs)
        info.mtime, info.uid, info.gid = EPOCH, 0, 0
        info.uname = info.gname = "fixture"
        return info

    def no_network(*_args, **_kwargs):
        raise AssertionError("Live network forbidden in M1 characterization")

    workspace.mkdir(parents=True, exist_ok=True)
    captured = io.StringIO()
    results, writes = [], []

    def record_result(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                results.append({"api": function.__name__, "raised": type(exc).__name__,
                                "message": str(exc)})
                raise
            results.append({"api": function.__name__, "result": result})
            return result
        return invoke

    def record_write(path, payload):
        _atomic_json(path, payload)
        writes.append(str(Path(path).relative_to(workspace)))

    with ExitStack() as stack, redirect_stdout(captured):
        stack.enter_context(patch.object(fixtures, "m1_phases", module))
        stack.enter_context(patch.object(FastProductionRunner, "__init__", isolated_init))
        stack.enter_context(patch.object(socket.socket, "connect", no_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", no_network))
        stack.enter_context(patch.object(tempfile, "tempdir", str(workspace)))
        stack.enter_context(patch.object(
            tempfile._RandomNameSequence, "__next__",
            lambda self: f"{next(names):08d}",
        ))
        stack.enter_context(patch.object(time, "time", return_value=float(EPOCH)))
        stack.enter_context(patch.object(time, "monotonic", return_value=1000.0))
        stack.enter_context(patch.object(time, "perf_counter", return_value=1000.0))
        stack.enter_context(patch.object(time, "sleep"))
        # These defaults captured the real clocks when their functions loaded.
        stack.enter_context(patch.dict(
            session_evidence.SessionBudget.__init__.__kwdefaults__,
            clock=lambda: 1000.0,
        ))
        stack.enter_context(patch.dict(
            session_evidence.finish_session_envelope.__kwdefaults__,
            clock=lambda: 1000.0,
        ))
        stack.enter_context(patch.object(os, "getpid", return_value=424242))
        stack.enter_context(patch.object(uuid, "uuid4", side_effect=lambda: uuid.UUID(int=next(identifiers))))
        stack.enter_context(patch.object(tarfile.TarFile, "gettarinfo", fixed_tarinfo))
        # Also covers function-local datetime imports in archive creation.
        stack.enter_context(patch.object(datetime_module, "datetime", FrozenDateTime))
        for loaded in [module, baseline, *list(sys.modules.values())]:
            if loaded is None or not getattr(loaded, "__name__", "").startswith(("src.", "m1_phases_")):
                continue
            if getattr(loaded, "datetime", None) is datetime:
                stack.enter_context(patch.object(loaded, "datetime", FrozenDateTime))
            if getattr(loaded, "ResourceSampler", None) is ResourceSampler:
                stack.enter_context(patch.object(loaded, "ResourceSampler", FixedSampler))
            if getattr(loaded, "_atomic_json", None) is _atomic_json:
                stack.enter_context(patch.object(loaded, "_atomic_json", record_write))
        for name in ("run_m1_arxiv_member", "run_m1_s2_member", "run_m1_audit_member"):
            stack.enter_context(patch.object(module, name, record_result(getattr(module, name))))
        test.setUp()
        try:
            getattr(test, method)()
            if method == "test_s2_before_arxiv_stops_on_429_then_resumes_without_arxiv_or_duplicate_ids":
                test.metadata_calls.clear()
                test.reference_calls.clear()
                completed = test.run_s2()
                assert completed["metadata_complete"] == 100
                assert completed["references_complete"] == 100
                assert test.metadata_calls == test.reference_calls == []
            artifacts = {
                str(p.relative_to(workspace)): base64.b64encode(p.read_bytes()).decode("ascii")
                for p in sorted(workspace.rglob("*")) if p.is_file()
            }
        finally:
            test.tearDown()
    return {"artifacts": artifacts, "stdout": captured.getvalue(),
            "results": results, "write_order": writes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("implementation", choices=("baseline", "current", "mixed"))
    parser.add_argument("case")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    result = run_fixture(arguments.implementation, arguments.case, arguments.workspace)
    arguments.output.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
