"""Locate M1 implementation text for existing static operational contracts."""
from pathlib import Path


def phase_implementation_source(repo_root: Path) -> str:
    """Read the facade and all extracted modules without changing assertions."""
    source = repo_root / "src"
    paths = [source / "m1_phases.py"]
    paths.extend(sorted((source / "m1_phase_modules").glob("*.py")))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)
