from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    TELEGRAM_BOT_TOKEN: str
    DB_PATH: str


def _parse_dotenv_file(dotenv_path: Path) -> None:
    """
    Minimal .env loader (no external deps).

    Supports lines:
      KEY=VALUE

    Rules:
    - ignores empty lines and comments starting with '#'
    - trims whitespace
    - supports optional surrounding single/double quotes around VALUE
    - does NOT override existing environment variables
    """
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        # Do not override already-set env vars.
        if key and not os.getenv(key):
            os.environ[key] = value


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value


def load_settings() -> Settings:
    # Load project-root .env (if present) before reading env vars.
    project_root = Path(__file__).resolve().parents[1]
    _parse_dotenv_file(project_root / ".env")

    token = _require_env("TELEGRAM_BOT_TOKEN")
    db_path = os.getenv("DB_PATH", os.path.join("data", "products.db"))
    return Settings(TELEGRAM_BOT_TOKEN=token, DB_PATH=db_path)
