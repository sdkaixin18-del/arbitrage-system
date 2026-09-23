from __future__ import annotations

import os
import re
from pathlib import Path


_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_project_env() -> None:
    configured_path = os.environ.get("STOCK_REVIEW_ENV_FILE", "").strip()
    env_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path(__file__).resolve().parents[2] / ".env"
    )
    if not env_path.is_file():
        return

    # The installed launcher points at a mode-600 credential file. Reject an
    # explicitly configured file that other users could read; source-tree
    # development keeps the existing local .env behaviour for compatibility.
    if configured_path:
        stat = env_path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("STOCK_REVIEW_ENV_FILE must be owned by the current user with mode 600")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME_PATTERN.fullmatch(key) or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
