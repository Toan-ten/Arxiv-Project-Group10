"""Notebook contract: exact runtime, plain cells, bounded smoke and live config."""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import nbformat
import pytest

REPO = Path(__file__).resolve().parents[1]
NB = REPO / "notebooks/m2/m2_colab_benchmark.ipynb"


def cells():
    return [c.source for c in nbformat.read(NB, as_version=4).cells if c.cell_type == "code"]


def pin():
    tree = ast.parse(next(c for c in cells() if "RELEASE_COMMIT =" in c))
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "RELEASE_COMMIT")


def test_valid_notebook_no_outputs_plain_python_cpu():
    nb = nbformat.read(NB, as_version=4); nbformat.validate(nb)
    assert "feat%2Fm2-colab-benchmark" in nb.cells[0].source
    for cell in nb.cells:
        if cell.cell_type == "code":
            assert cell.outputs == [] and cell.execution_count is None
            compile(cell.source, "benchmark-cell", "exec")
    assert not any("ipywidgets" in c for c in cells())
    assert any('COLAB_GPU' in c for c in cells())


def test_exact_runtime_has_current_helpers_fixed_parser_and_requirements():
    sha = pin(); assert re.fullmatch(r"[0-9a-f]{40}", sha)
    git = ["git", "-C", str(REPO)]
    names = subprocess.check_output(git + ["ls-tree", "-r", "--name-only", sha, "src/m2"], text=True).splitlines()
    names = sorted(n for n in names if n.endswith(".py"))
    parser_blobs = []
    for name in names + ["scripts/m2_colab_benchmark.py", "scripts/m2_colab_export.py",
        "scripts/m2_colab_recovery.py", "requirements-m2-benchmark.txt", "requirements-m2.txt",
        "config/group10_id_ranges.json"]:
        blob = subprocess.check_output(git + ["show", f"{sha}:{name}"])
        assert (REPO / name).read_bytes() == blob, f"Stale runtime: {name}"
        if name in names:
            parser_blobs.append(blob)
    assert hashlib.sha256(b"".join(parser_blobs)).hexdigest() == "4e88c7fb0d3463c4c6815b5afff3f64066815068dd4c34db1e92528966d60b70"
    assert sha != "072ee384efa2e22f743ada7786aa0f67113a1490"


def namespace(tmp_path):
    root = tmp_path / "benchmark-fixture"
    process = Mock()
    process.check_output.return_value = json.dumps(dict(campaign=str(root), cohorts=["cohort_0101_corpus"], existing_completions=[],
        batches=[["cohort_0101_corpus"]], input_inventory=[]))
    process.run.return_value = SimpleNamespace(returncode=0)
    ns = dict(Path=Path, json=json, subprocess=process, sys=SimpleNamespace(executable="python"),
        CODE_DIR=REPO, RELEASE_COMMIT=pin(), LOCAL_CONFIG=tmp_path / "local.json",
        START_COHORT=101, END_COHORT=150, STUDENT_ID="24120232", INPUT_DRIVE_DIR="shared/archive-root",
        OUTPUT_DRIVE_DIR="separate/benchmark-output", SCRATCH="local/scratch",
        COHORTS_PER_BATCH=50, AUTO_CONTINUE=True, BENCHMARK_CAMPAIGN="")
    exec(next(c for c in cells() if "def current_config():" in c), ns)
    return ns, root


@pytest.mark.parametrize("owner,start,end", [("24120232",101,150),("24120010",1,1),("24120260",1,200)])
def test_live_configuration_not_captured_and_shared_inputs(tmp_path, owner, start, end):
    ns, _ = namespace(tmp_path)
    ns.update(STUDENT_ID=owner, START_COHORT=start, END_COHORT=end)
    config = ns["current_config"]()
    assert config["start_cohort"] == start and config["end_cohort"] == end
    assert config["input_drive_dir"] == "shared/archive-root"
    ns["INPUT_DRIVE_DIR"] = "new/shared/folder"
    assert ns["current_config"]()["input_drive_dir"] == "new/shared/folder"


def test_run_range_requires_smoke_and_uses_cli_without_parsing_in_kernel(tmp_path):
    ns, root = namespace(tmp_path)
    with pytest.raises(RuntimeError, match="Smoke"):
        ns["run_benchmark"]("run")
    ns["subprocess"].run.assert_not_called()
    (root / "completed").mkdir(parents=True)
    (root / "completed/cohort_0101_corpus.json").write_text("fixture")
    ns["run_benchmark"]("run")
    args = ns["subprocess"].run.call_args.args[0]
    assert args[args.index("-m") + 1] == "scripts.m2_colab_benchmark"
    assert args[-2:] == ["--action", "run"]
    assert ns["subprocess"].run.call_args.kwargs["cwd"] == REPO


def test_smoke_report_actions_are_separate_and_no_auto_migration():
    source = "\n".join(cells())
    assert 'run_benchmark("smoke")' in cells()
    assert 'run_benchmark("run")' in cells()
    assert "old_campaign" not in source and "recover_cohort" not in source
    assert "upload" not in source and "SEMANTIC_SCHOLAR_API_KEY" not in source
    lock_cell = next(c for c in cells() if "def archive_stale_locks():" in c)
    tree = ast.parse(lock_cell)
    # The destructive/rename helper is defined, never automatically invoked.
    calls = [n.value for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert all(not isinstance(c.func, ast.Name) or c.func.id != "archive_stale_locks" for c in calls)
    assert "answer != root.name" in lock_cell


def test_existing_export_and_recovery_notebooks_unchanged():
    for name in ("m2_team_colab_export.ipynb", "m2_nocite_recovery.ipynb"):
        path = f"notebooks/m2/{name}"
        old = subprocess.check_output(["git", "-C", str(REPO), "show", f"74e846b74b8692dd717cf127681001c3613db9e2:{path}"])
        assert (REPO / path).read_bytes() == old


def test_batch_configuration_is_live_after_setup(tmp_path):
    ns, _ = namespace(tmp_path)
    assert ns["current_config"]()["cohorts_per_batch"] == 50
    ns.update(COHORTS_PER_BATCH=5, AUTO_CONTINUE=False, BENCHMARK_CAMPAIGN="old/benchmark-id")
    config = ns["current_config"]()
    assert config["cohorts_per_batch"] == 5 and config["auto_continue"] is False
    assert config["benchmark_campaign"] == "old/benchmark-id"


def test_default_automatic_200_scope_and_no_plot_dependencies():
    config = {}; exec(cells()[0], config)
    assert (config["START_COHORT"], config["END_COHORT"], config["COHORTS_PER_BATCH"], config["AUTO_CONTINUE"]) == (1,200,50,True)
    assert "matplotlib" not in (REPO / "requirements-m2-benchmark.txt").read_text().lower()
