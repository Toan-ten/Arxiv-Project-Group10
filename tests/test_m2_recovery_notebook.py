"""Recovery notebook contract: pinned runtime, no auto writes, explicit phase gates."""
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
NOTEBOOK = REPO / "notebooks/m2/m2_nocite_recovery.ipynb"


def notebook():
    return nbformat.read(NOTEBOOK, as_version=4)


def code_cells():
    return [c.source for c in notebook().cells if c.cell_type == "code"]


def pins():
    setup = next(c for c in code_cells() if 'RELEASE_COMMIT =' in c)
    tree = ast.parse(setup)
    return {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in {"RELEASE_COMMIT", "PARSER_FINGERPRINT"}}


def test_notebook_valid_no_outputs_and_every_code_cell_compiles():
    nb = notebook()
    nbformat.validate(nb)
    assert "Open in Colab" in nb.cells[0].source
    assert "fix%2Fm2-nocite-checkpoint-migration" in nb.cells[0].source
    for cell in nb.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None and cell.outputs == []
            compile(cell.source, "recovery-cell", "exec")


def test_runtime_pin_contains_exact_published_parser_and_helpers():
    release = pins()
    sha = release["RELEASE_COMMIT"]
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    git = ["git", "-C", str(REPO)]
    assert subprocess.check_output(git + ["cat-file", "-t", sha], text=True).strip() == "commit"
    names = subprocess.check_output(git + ["ls-tree", "-r", "--name-only", sha, "src/m2"], text=True).splitlines()
    names = sorted(n for n in names if n.endswith(".py"))
    blobs = []
    for name in names + ["scripts/m2_colab_recovery.py", "scripts/m2_colab_export.py",
                         "config/group10_id_ranges.json", "requirements-m2.txt"]:
        raw = subprocess.check_output(git + ["show", f"{sha}:{name}"])
        assert (REPO / name).read_bytes() == raw, f"Notebook would load stale code: {name}"
        if name in names:
            blobs.append(raw)
    assert hashlib.sha256(b"".join(blobs)).hexdigest() == release["PARSER_FINGERPRINT"]
    assert release["PARSER_FINGERPRINT"] == "4e88c7fb0d3463c4c6815b5afff3f64066815068dd4c34db1e92528966d60b70"


def controller(tmp_path, owner="24120232", start=101, end=150):
    old = tmp_path / "old"; old.mkdir()
    identity = dict(owner=owner, scope=dict(start=start, end=end), archives=str(tmp_path / "archives"), hf_repo_id="member/m2-data")
    (old / "campaign.json").write_text(json.dumps(identity), encoding="utf8")
    release = pins()
    namespace = dict(Path=Path, json=json, subprocess=Mock(), sys=SimpleNamespace(executable="python"),
        OLD_CAMPAIGN=str(old), RECOVERY_ROOT="", PILOT_COHORT=115, BATCH_SIZE=5,
        CODE_DIR=REPO, SCRATCH=str(tmp_path / "scratch"), LOCAL_CONFIG=tmp_path / "config.json",
        RELEASE_COMMIT=release["RELEASE_COMMIT"], PARSER_FINGERPRINT=release["PARSER_FINGERPRINT"],
        _SETUP_SELECTION=(str(old), "", 115), PREFLIGHT=None)
    source = next(c for c in code_cells() if "def current_config():" in c)
    exec(compile(source, "controller", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("owner,start,end", [("24120232",101,150),("24120010",1,50)])
def test_controller_inherits_original_identity_and_live_batch_size(tmp_path, owner, start, end):
    ns = controller(tmp_path, owner, start, end)
    a = ns["current_config"]()
    assert (a["student_id"], a["start_cohort"], a["end_cohort"], a["batch_size"]) == (owner,start,end,5)
    ns["BATCH_SIZE"] = 50
    b = ns["current_config"]()
    assert b == a | {"batch_size":50}
    assert b["allow_uncommitted"] is False
    assert b["recovery_root"] == f"/content/drive/MyDrive/ArxivGroup10/{owner}/m2-recovery"
    assert not ns["subprocess"].run.called  # Definitions/configuration never start work.


def test_controller_requires_preflight_and_manual_confirmation(tmp_path):
    ns = controller(tmp_path)
    ns["command"] = Mock()
    with pytest.raises(RuntimeError, match="preflight"):
        ns["write_action"]("pilot")
    ns["PREFLIGHT"] = {"ready":True, "preflight_token":"fixture"}
    ns["input"] = lambda _: "cancel"
    ns["write_action"]("pilot")
    ns["command"].assert_not_called()
    ns["input"] = lambda _: "PILOT 0115"
    ns["write_action"]("pilot")
    ns["command"].assert_called_once_with("pilot", approved=True)


def test_readonly_command_stays_local_and_has_no_approval_token(tmp_path):
    ns = controller(tmp_path)
    ns["subprocess"].run.return_value = SimpleNamespace(returncode=0, stdout='{"ready":true}\n', stderr="")
    assert ns["command"]("dry-run", capture=True) == {"ready":True}
    config = json.loads(ns["LOCAL_CONFIG"].read_text(encoding="utf8"))
    assert "approved_preflight_token" not in config
    args = ns["subprocess"].run.call_args.args[0]
    assert args[-1] == "dry-run" and "scripts.m2_colab_recovery" in args
    assert not (tmp_path / "old/verified").exists()


def test_setup_has_exact_checkout_and_never_starts_migration():
    setup = next(c for c in code_cells() if "RELEASE_COMMIT =" in c)
    assert '"--detach", RELEASE_COMMIT' in setup
    assert '"--depth"' not in setup  # Legacy source attestation needs history.
    assert 'pyarrow==26.0.1' in setup
    assert "drive.mount" in setup
    tree = ast.parse(setup)
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in {"operate", "command", "write_action"} for n in ast.walk(tree))
    calls = []
    for cell in code_cells():
        for node in ast.parse(cell).body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                call = node.value
                if call.func.id in {"show_preflight", "write_action", "command"}:
                    calls.append((call.func.id, ast.literal_eval(call.args[0])))
    assert ("show_preflight", "preflight") in calls and ("show_preflight", "dry-run") in calls
    assert ("write_action", "pilot") in calls and ("command", "audit-pilot") in calls
    assert ("write_action", "continue") in calls
    assert not any(action == "next" for _, action in calls)


def test_original_shared_notebook_remains_byte_identical():
    name = "notebooks/m2/m2_team_colab_export.ipynb"
    original = subprocess.check_output(["git", "-C", str(REPO), "show", "26b267cbbc128935a6729d8d4ec43ba4e84f0f4e:" + name])
    assert (REPO / name).read_bytes() == original
