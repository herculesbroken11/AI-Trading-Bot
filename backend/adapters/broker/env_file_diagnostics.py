"""Safe .env diagnostics for sandbox OAuth (no secret values in output)."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_TASTYTRADE_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class DuplicateEnvKeyReport:
    path: str
    duplicate_keys: Dict[str, int]

    @property
    def has_duplicates(self) -> bool:
        return bool(self.duplicate_keys)

    def format_safe(self) -> str:
        if not self.duplicate_keys:
            return "duplicate_tastytrade_env_keys: none"
        lines = ["duplicate_tastytrade_env_keys:"]
        for key, count in sorted(self.duplicate_keys.items()):
            lines.append(f"  {key}: {count} definitions")
        lines.append(
            "warning: duplicate keys mean the last definition wins; "
            "remove extras to avoid secret/token mismatch."
        )
        return "\n".join(lines)


def safe_fingerprint(value: str, *, label: str = "value") -> str:
    """Return first4...last4 + short hash; never the full secret."""
    text = (value or "").strip()
    if not text:
        return f"{label}: empty"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    if len(text) <= 8:
        return f"{label}: len={len(text)} sha256_8={digest}"
    return f"{label}: {text[:4]}...{text[-4:]} len={len(text)} sha256_8={digest}"


def find_duplicate_tastytrade_env_keys(env_path: Path) -> DuplicateEnvKeyReport:
    """Detect duplicate TASTYTRADE_* keys in a .env file (values never returned)."""
    counts: Counter[str] = Counter()
    if not env_path.is_file():
        return DuplicateEnvKeyReport(path=str(env_path), duplicate_keys={})

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _TASTYTRADE_KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key.startswith("TASTYTRADE_"):
            counts[key] += 1

    duplicates = {key: count for key, count in counts.items() if count > 1}
    return DuplicateEnvKeyReport(path=str(env_path), duplicate_keys=duplicates)


def list_tastytrade_env_keys(env_path: Path) -> List[str]:
    """Return ordered TASTYTRADE_* key names from .env (no values)."""
    keys: List[str] = []
    if not env_path.is_file():
        return keys
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _TASTYTRADE_KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key.startswith("TASTYTRADE_"):
            keys.append(key)
    return keys
