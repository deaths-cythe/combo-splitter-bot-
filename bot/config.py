from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_base_url: str
    max_download_mb: int
    max_upload_mb: int
    temp_dir: str
    log_level: str
    default_preview_lines: int
    max_parts: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN is required")
        return cls(
            bot_token=token,
            api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "").strip(),
            max_download_mb=_env_int("MAX_DOWNLOAD_MB", 20),
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 50),
            temp_dir=os.getenv("TEMP_DIR", "./tmp"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            default_preview_lines=_env_int("DEFAULT_PREVIEW_LINES", 10),
            max_parts=_env_int("MAX_PARTS", 100),
        )

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024