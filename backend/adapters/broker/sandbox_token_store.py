"""Persist rotated sandbox OAuth refresh tokens without logging secrets."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REFRESH_TOKEN_ENV_KEY = "TASTYTRADE_REFRESH_TOKEN"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ENV_PATH = _REPO_ROOT / ".env"
_KEY_LINE_RE = re.compile(rf"^{re.escape(REFRESH_TOKEN_ENV_KEY)}\s*=")


def default_env_path() -> Path:
    return _DEFAULT_ENV_PATH


def persist_sandbox_refresh_token(
    new_refresh_token: str,
    *,
    env_path: Optional[Path] = None,
    previous_refresh_token: Optional[str] = None,
) -> bool:
    """
    Write rotated refresh token to .env and process env.

    Returns True when a file write occurred.
    Never logs token values.
    """
    token = (new_refresh_token or "").strip()
    if not token:
        return False

    previous = (previous_refresh_token or "").strip()
    if previous and token == previous:
        return False

    # Keep in-process env in sync even if .env is missing.
    os.environ[REFRESH_TOKEN_ENV_KEY] = token

    path = env_path if env_path is not None else default_env_path()
    try:
        if path.is_file():
            original = path.read_text(encoding="utf-8")
            lines = original.splitlines(keepends=True)
            updated = False
            new_lines = []
            for line in lines:
                stripped = line.lstrip("\ufeff")
                if _KEY_LINE_RE.match(stripped):
                    ending = ""
                    if line.endswith("\r\n"):
                        ending = "\r\n"
                    elif line.endswith("\n"):
                        ending = "\n"
                    new_lines.append(f"{REFRESH_TOKEN_ENV_KEY}={token}{ending}")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                suffix = ""
                if lines and not lines[-1].endswith("\n"):
                    suffix = "\n"
                new_lines.append(f"{suffix}{REFRESH_TOKEN_ENV_KEY}={token}\n")
            path.write_text("".join(new_lines), encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{REFRESH_TOKEN_ENV_KEY}={token}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "sandbox refresh token rotated but could not persist to .env (%s). "
            "Create a new sandbox grant and update TASTYTRADE_REFRESH_TOKEN manually.",
            type(exc).__name__,
        )
        return False

    logger.info(
        "sandbox refresh token rotated and persisted to .env (value not logged)"
    )
    return True
