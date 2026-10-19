"""Settings of the environment-data framework: cache location and size, offline mode.

Stored in ``~/.xradar-desktop/environment.json`` (``$XRADAR_ENV_SETTINGS`` points elsewhere).
Environment variables win over the file, so a CI job or a locked-down machine can pin them:

* ``XRADAR_OFFLINE=1``           never touch the network;
* ``XRADAR_ENV_CACHE_DIR``       where fetched files are kept;
* ``XRADAR_ENV_CACHE_MAX_MB``    size limit of that folder (least recently used files go first).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.environment.errors import InvalidRequest

MIN_CACHE_MB = 16
MAX_CACHE_MB = 4 * 1024 * 1024  # 4 TB
DEFAULT_CACHE_MB = 2048
_TRUE = {"1", "true", "yes", "on"}


def _home() -> Path:
    return Path.home() / ".xradar-desktop"


def settings_path() -> Path:
    env = os.environ.get("XRADAR_ENV_SETTINGS")
    return Path(env).expanduser() if env else _home() / "environment.json"


def default_cache_dir() -> Path:
    return _home() / "environment-cache"


@dataclass
class EnvSettings:
    cache_dir: str = ""  # "" means the default folder
    cache_max_mb: int = DEFAULT_CACHE_MB
    offline: bool = False
    max_concurrent: int = 2

    # -- resolved values (environment variables win) -----------------------------------
    @property
    def effective_cache_dir(self) -> Path:
        env = os.environ.get("XRADAR_ENV_CACHE_DIR", "").strip()
        chosen = env or self.cache_dir.strip()
        return Path(chosen).expanduser() if chosen else default_cache_dir()

    @property
    def effective_cache_max_mb(self) -> int:
        env = os.environ.get("XRADAR_ENV_CACHE_MAX_MB", "").strip()
        if env.isdigit():
            return min(MAX_CACHE_MB, max(MIN_CACHE_MB, int(env)))
        return self.cache_max_mb

    @property
    def effective_offline(self) -> bool:
        return self.offline or os.environ.get("XRADAR_OFFLINE", "").strip().lower() in _TRUE

    def locked(self) -> list[str]:
        """Names of the settings an environment variable currently overrides."""
        out = []
        if os.environ.get("XRADAR_OFFLINE", "").strip().lower() in _TRUE:
            out.append("offline")
        if os.environ.get("XRADAR_ENV_CACHE_DIR", "").strip():
            out.append("cache_dir")
        if os.environ.get("XRADAR_ENV_CACHE_MAX_MB", "").strip().isdigit():
            out.append("cache_max_mb")
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            effective_cache_dir=str(self.effective_cache_dir),
            effective_cache_max_mb=self.effective_cache_max_mb,
            effective_offline=self.effective_offline,
            locked=self.locked(),
            default_cache_dir=str(default_cache_dir()),
        )
        return d

    # -- changes ---------------------------------------------------------------------------
    def updated(self, changes: dict[str, Any]) -> EnvSettings:
        """A validated copy with *changes* applied (unknown keys are rejected)."""
        new = EnvSettings(**asdict(self))
        for key, value in changes.items():
            if key == "cache_dir":
                text = str(value or "").strip()
                if text:
                    p = Path(text).expanduser()
                    if not p.is_absolute() or ".." in p.parts:
                        raise InvalidRequest("The cache folder must be an absolute path.")
                    if p.exists() and not p.is_dir():
                        raise InvalidRequest(f"{p} exists and is not a folder.")
                new.cache_dir = text
            elif key == "cache_max_mb":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise InvalidRequest("The cache size must be a number of megabytes.")
                new.cache_max_mb = int(min(MAX_CACHE_MB, max(MIN_CACHE_MB, value)))
            elif key == "offline":
                new.offline = bool(value)
            elif key == "max_concurrent":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise InvalidRequest("max_concurrent must be a number.")
                new.max_concurrent = int(min(4, max(1, value)))
            else:
                raise InvalidRequest(f"Unknown environment setting {key!r}.")
        return new


def load_settings(path: Path | None = None) -> EnvSettings:
    """The saved settings, or the defaults when the file is missing or damaged."""
    p = path or settings_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            known = {k: v for k, v in raw.items() if k in EnvSettings.__dataclass_fields__}
            return EnvSettings().updated(known)
    except (OSError, ValueError, InvalidRequest):
        pass
    return EnvSettings()


def save_settings(settings: EnvSettings, path: Path | None = None) -> None:
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, p)
