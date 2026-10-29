"""Settings from environment variables, falling back to the macOS Keychain."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# macOS Keychain items to fall back to for local runs. On the VM, secrets come from the env file.
KEYCHAIN = {
    "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN_CALBOT",
    "GEMINI_API_KEY": "GEMINI_API_KEY_CALBOT",
    "ACCESS_CODE": "ACCESS_CODE_CALBOT",
}


def _secret(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if not value and name in KEYCHAIN:
        try:
            value = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", KEYCHAIN[name], "-w"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            value = ""
    if required and not value:
        raise SystemExit(f"Missing {name}: set the env var (on a Mac, the Keychain item {KEYCHAIN.get(name, name)} also works)")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    gemini_api_key: str
    access_code: str
    db_path: Path
    default_tz: str
    gemini_model: str
    daily_ai_cap: int
    owner_chat_id: int | None = None  # gets a DM when Gemini keeps failing


def load() -> Settings:
    return Settings(
        telegram_token=_secret("TELEGRAM_BOT_TOKEN"),
        gemini_api_key=_secret("GEMINI_API_KEY"),
        access_code=_secret("ACCESS_CODE"),
        db_path=Path(os.environ.get("CALBOT_DB", Path(__file__).resolve().parent.parent / "data" / "calbot.db")),
        default_tz=os.environ.get("CALBOT_TZ", "Asia/Singapore"),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
        daily_ai_cap=int(os.environ.get("CALBOT_DAILY_AI_CAP", "80")),
        owner_chat_id=int(os.environ["CALBOT_OWNER_ID"]) if os.environ.get("CALBOT_OWNER_ID") else None,
    )
