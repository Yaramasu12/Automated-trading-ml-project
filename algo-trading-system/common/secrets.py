"""
Secrets loading — loads credentials from .env, never from git.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def load_env_file(path: Optional[Path] = None) -> dict[str, str]:
    """Load a .env file into a dict. Returns empty dict if file doesn't exist."""
    if path is None:
        path = Path(".env")
    if not path.exists():
        return {}

    env: dict[str, str] = {}
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


_CACHED_ENV: dict[str, str] = {}


def get_env(key: str, default: str = "") -> str:
    """Get an environment variable, falling back to .env file, then default."""
    # First check real OS env
    val = os.getenv(key)
    if val is not None:
        return val
    # Then check loaded .env
    global _CACHED_ENV
    if not _CACHED_ENV:
        _CACHED_ENV = load_env_file()
    return _CACHED_ENV.get(key, default)


def get_secret(key: str) -> str:
    """Get a secret value. Raises if not found — secrets are required."""
    val = get_env(key)
    if not val:
        raise ValueError(f"Required secret '{key}' is not set. Add it to your .env file.")
    return val


def has_secret(key: str) -> bool:
    """Check if a secret is available."""
    return bool(get_env(key))


def save_secrets_to_json(secrets: dict[str, str], path: Path) -> None:
    """Save secrets to a protected JSON file (never commit this)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(secrets, f, indent=2)
    # Restrict permissions on Unix
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_secrets_from_json(path: Path) -> dict[str, str]:
    """Load secrets from a protected JSON file."""
    if not path.exists():
        return {}
    with path.open("r") as f:
        return json.load(f)


__all__ = ["get_env", "get_secret", "has_secret", "load_env_file", "save_secrets_to_json", "load_secrets_from_json"]