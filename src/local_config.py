"""Read the optional root .env for S2, lazily and without modifying os.environ.

This deliberately supports single-line assignments, not the full dotenv language.
No filesystem reads, key reads, logging or network activity happen on import.
"""
import os
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY_NAME = "SEMANTIC_SCHOLAR_API_KEY"
_MAX_ENV_BYTES = 65_536
_ASSIGNMENT = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)")


def _root_file_key():
    path = PROJECT_ROOT / ".env"
    if path.is_symlink():
        raise ValueError("Root .env must not be a symbolic link")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_ENV_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Cannot read root .env") from None
    if len(raw) > _MAX_ENV_BYTES:
        raise ValueError("Root .env exceeds the size limit")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Root .env must use UTF-8") from None

    value, found = None, False
    for line in content.split("\n"):
        line = line.removesuffix("\r").strip(" \t")
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError("Invalid root .env assignment")
        name, candidate = match.groups()
        if name != KEY_NAME:
            continue  # Không nạp các thiết lập không liên quan vào tiến trình.
        if found:
            raise ValueError("Duplicate SEMANTIC_SCHOLAR_API_KEY in root .env")
        found = True
        candidate = candidate.strip(" \t")
        if candidate.startswith(("'", '"')):
            quote = candidate[0]
            closing = candidate.find(quote, 1)
            if closing < 0:
                raise ValueError("Invalid quoted key in root .env")
            suffix = candidate[closing + 1:].strip(" \t")
            if suffix and not suffix.startswith("#"):
                raise ValueError("Invalid quoted key in root .env")
            value = candidate[1:closing]
        else:
            value = re.split(r"[ \t]+#", candidate, maxsplit=1)[0].rstrip(" \t")
            if "'" in value or '"' in value:
                raise ValueError("Invalid quoted key in root .env")
        if any(marker in value for marker in ("${", "$(", "`")):
            raise ValueError("Key interpolation is not supported in root .env")
    return value


def get_semantic_scholar_api_key():
    """Return a validated key or None; never print or persist credentials.

    An explicitly set environment value, including an empty value, wins over
    .env. Invalid environment values raise instead of silently using another key.
    """
    value = os.environ.get(KEY_NAME)
    if value is None:
        value = _root_file_key()
    if not value:
        return None
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid SEMANTIC_SCHOLAR_API_KEY header")
    value = value.strip()
    if not value:
        return None
    if not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Invalid SEMANTIC_SCHOLAR_API_KEY header")
    return value
