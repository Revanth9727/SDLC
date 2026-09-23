"""Best-effort, non-persistent credential hints for repository test runs."""
from __future__ import annotations

import re
import threading
from pathlib import Path


_MAX_FILE_BYTES = 512_000
_MAX_TOTAL_BYTES = 2_000_000
_ENV_NAME = r"[A-Z_][A-Z0-9_]*"
_CANDIDATE_PATTERNS = (
    re.compile(rf"(?m)^\s*(?:export\s+)?({_ENV_NAME})\s*="),
    re.compile(rf"\$\{{({_ENV_NAME})(?::[-?][^}}]*)?\}}"),
    re.compile(rf"(?:os\.getenv|os\.environ\.get)\(\s*['\"]({_ENV_NAME})['\"]"),
    re.compile(rf"os\.environ\[\s*['\"]({_ENV_NAME})['\"]\s*\]"),
    re.compile(rf"(?:secrets|vars)\.({_ENV_NAME})\b", re.I),
)
_SECRET_WORDS = (
    "PASSWORD", "PASSWD", "TOKEN", "SECRET", "API_KEY", "PRIVATE_KEY",
    "CREDENTIAL", "AUTH", "DATABASE_URL", "DB_URL", "DSN",
)
_active: dict[str, set[str]] = {}
_offered: dict[str, set[str]] = {}
_lock = threading.Lock()


def predict_test_credentials(checkout: Path) -> list[str]:
    """Scan only conventional setup files and return likely secret variable names."""
    root = Path(checkout).resolve()
    candidates = [root / ".env.example", root / "docker-compose.yml", root / "docker-compose.yaml",
                  root / "conftest.py", root / "pyproject.toml"]
    candidates.extend(sorted(root.glob("requirements*.txt")))
    candidates.extend(sorted((root / ".github" / "workflows").glob("*.yml")))
    candidates.extend(sorted((root / ".github" / "workflows").glob("*.yaml")))
    found: set[str] = set()
    total = 0
    for path in dict.fromkeys(candidates):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES or total + size > _MAX_TOTAL_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            total += size
        except OSError:
            continue
        for pattern in _CANDIDATE_PATTERNS:
            found.update(name.upper() for name in pattern.findall(text) if _likely_secret(name))
    return sorted(found)


def remember_preflight(subtask_id: str, names: list[str]) -> None:
    """Keep hints in process memory only; already-offered hints stay suppressed."""
    with _lock:
        active = set(names) - _offered.get(str(subtask_id), set())
        if active:
            _active[str(subtask_id)] = active
        else:
            _active.pop(str(subtask_id), None)


def credential_prompt(subtask_id: str, runtime_required: list[str]) -> list[dict[str, object]]:
    """Merge runtime truth with optional preflight hints for an API response."""
    required = set(runtime_required)
    with _lock:
        predicted = set(_active.get(str(subtask_id), set()))
    return [
        {"name": name, "required": name in required,
         "source": "runtime" if name in required else "preflight"}
        for name in sorted(required | predicted)
    ]


def mark_prompt_offered(subtask_id: str) -> None:
    """Do not repeat optional fields after the first credential round-trip."""
    key = str(subtask_id)
    with _lock:
        offered = _active.pop(key, set())
        if offered:
            _offered.setdefault(key, set()).update(offered)


def clear_preflight(subtask_id: str) -> None:
    key = str(subtask_id)
    with _lock:
        _active.pop(key, None)
        _offered.pop(key, None)


def _likely_secret(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in _SECRET_WORDS)
