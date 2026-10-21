"""One-time local setup for the Price receiver; preserves existing bot settings."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import tempfile


def configure(path: Path, supplied_key: str | None = None) -> str:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    current = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            current[name.strip()] = value.strip().strip("\"'")
    key = supplied_key or current.get("PRICE_SYNC_KEY") or secrets.token_urlsafe(32)
    if not 32 <= len(key) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("Existing PRICE_SYNC_KEY has invalid length")
    settings = dict(PRICE_SYNC_KEY=key, PRICE_SYNC_HOST=current.get("PRICE_SYNC_HOST") or "127.0.0.1",
                    PRICE_SYNC_PORT=current.get("PRICE_SYNC_PORT") or "8082")
    output = [line for line in lines if line.split("=", 1)[0].strip() not in settings]
    output.extend(f"{name}={value}" for name, value in settings.items())
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-c3-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(output) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return key


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure C3 Trade push receiver in the bot .env")
    parser.add_argument("--show-key", action="store_true", help="Show the exchange key locally for entry into C3 Trade")
    parser.add_argument("--if-configured", action="store_true", help="Only configure when a key already exists or PRICE_SYNC_KEY is supplied by deployment")
    args = parser.parse_args()
    env_path = Path(__file__).resolve().parents[1] / ".env"
    supplied = os.getenv("PRICE_SYNC_KEY")
    if args.if_configured and not supplied:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        configured = any(line.partition("=")[0].strip() == "PRICE_SYNC_KEY"
                         and line.partition("=")[2].strip().strip("\"'") for line in existing.splitlines())
        if not configured:
            print("C3 receiver is not configured; existing HTML workflow preserved.")
            raise SystemExit(0)
    key = configure(env_path, supplied)
    if args.show_key:
        print(key)
    else:
        print("C3 receiver configured. Restart price-bot; use --show-key locally to copy the exchange key.")
