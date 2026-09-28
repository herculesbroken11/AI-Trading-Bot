"""Persist rotated OAuth refresh tokens without logging secrets."""

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


def default_env_path() -> Path:
    return _DEFAULT_ENV_PATH


def persist_refresh_token(
    new_refresh_token: str,
    *,
    env_key: str,
    env_path: Optional[Path] = None,
    previous_refresh_token: Optional[str] = None,
    label: str = "sandbox",
) -> bool:
    """
    Write a rotated refresh token to .env (under env_key) and the process env.

    Returns True when a file write occurred. Never logs token values.
    """
    token = (new_refresh_token or "").strip()
    if not token:
        return False

    previous = (previous_refresh_token or "").strip()
    if previous and token == previous:
        return False

    key_line_re = re.compile(rf"^{re.escape(env_key)}\s*=")

    # Keep in-process env in sync even if .env is missing.
    os.environ[env_key] = token

    path = env_path if env_path is not None else default_env_path()
    try:
        if path.is_file():
            original = path.read_text(encoding="utf-8")
            lines = original.splitlines(keepends=True)
            updated = False
            new_lines = []
            for line in lines:
                stripped = line.lstrip("\ufeff")
                if key_line_re.match(stripped):
                    ending = ""
                    if line.endswith("\r\n"):
                        ending = "\r\n"
                    elif line.endswith("\n"):
                        ending = "\n"
                    new_lines.append(f"{env_key}={token}{ending}")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                suffix = ""
                if lines and not lines[-1].endswith("\n"):
                    suffix = "\n"
                new_lines.append(f"{suffix}{env_key}={token}\n")
            path.write_text("".join(new_lines), encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{env_key}={token}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "%s refresh token rotated but could not persist to .env (%s). "
            "Create a new grant and update %s manually.",
            label,
            type(exc).__name__,
            env_key,
        )
        return False

    logger.info("%s refresh token rotated and persisted to .env (value not logged)", label)
    return True


def persist_sandbox_refresh_token(
    new_refresh_token: str,
    *,
    env_path: Optional[Path] = None,
    previous_refresh_token: Optional[str] = None,
) -> bool:
    return persist_refresh_token(
        new_refresh_token,
        env_key=REFRESH_TOKEN_ENV_KEY,
        env_path=env_path,
        previous_refresh_token=previous_refresh_token,
        label="sandbox",
    )
