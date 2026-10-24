"""WebSocket messages of the environment framework and of the sounding tools.

``ConnectionHandler`` (python/server.py) inherits :class:`EnvironmentMessages`, which adds:

``env_status``        -> ``env_status``: providers (+ their inventory), settings, cache usage
``env_estimate``      -> ``env_estimate_result`` (what would be downloaded; **nothing is**)
``env_fetch``         -> ``env_progress`` ... ``env_fetch_result`` | ``env_error``
                        (needs ``approve: true`` for a download, sent only after the user saw the
                        estimate for the same ``request_id``)
``env_cancel``        -> ``env_cancelled`` (from the running fetch)
``env_set_settings``  -> ``env_status``;  ``env_clear_cache`` -> ``env_status``
``sounding_open``     -> ``sounding_result`` | ``sounding_error`` (a local file or the example)
``env_profile``       -> ``sounding_result`` | ``sounding_error``: the column of a model or
                        reanalysis provider at ``lat``/``lon``, read from the cache only (the
                        data was fetched through ``env_estimate`` / ``env_fetch`` after the
                        user's approval; this message never downloads)

Not available in web mode (a public service must not download on a visitor's behalf or read the
server's disk).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from engine.environment.errors import (
    ApprovalRequired,
    ChecksumMismatch,
    EnvironmentDataError,
    FetchCancelled,
    FetchFailed,
    InvalidRequest,
    OfflineError,
    RequestTooLarge,
    UnknownProvider,
)
from engine.environment.request import Approval, Request

logger = logging.getLogger("xradar-server.environment")

MAX_PENDING = 32
PENDING_TTL_S = 1800.0

_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (ApprovalRequired, "approval_required"),
    (OfflineError, "offline"),
    (RequestTooLarge, "too_large"),
    (ChecksumMismatch, "checksum"),
    (FetchCancelled, "cancelled"),
    (FetchFailed, "failed"),
    (InvalidRequest, "invalid"),
    (UnknownProvider, "unknown_provider"),
)


def error_code(exc: BaseException) -> str:
    for cls, code in _CODES:
        if isinstance(exc, cls):
            return code
    return "failed" if isinstance(exc, EnvironmentDataError) else "internal"


class EnvironmentMessages:
    """Mixin for ``ConnectionHandler`` (uses ``self.send``, ``self.session``)."""

    def _env_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {
            "env_status": self._handle_env_status,
            "env_estimate": self._handle_env_estimate,
            "env_fetch": self._handle_env_fetch,
            "env_cancel": self._handle_env_cancel,
            "env_set_settings": self._handle_env_set_settings,
            "env_clear_cache": self._handle_env_clear_cache,
            "sounding_open": self._handle_sounding_open,
            "env_profile": self._handle_env_profile,
        }

    # -- state ------------------------------------------------------------------------------

    def _env_pending(self) -> dict[str, tuple[Request, float, Approval]]:
        if not hasattr(self, "_env_pending_map"):
            self._env_pending_map: dict[str, tuple[Request, float, Approval]] = {}
            self._env_jobs: dict[str, threading.Event] = {}
        return self._env_pending_map

    def _env_remember(self, request_id: str, request: Request, approval: Approval) -> None:
        pending = self._env_pending()
        now = time.time()
        for rid in [r for r, (_, t, _a) in pending.items() if now - t > PENDING_TTL_S]:
            pending.pop(rid, None)
        while len(pending) >= MAX_PENDING:
            pending.pop(next(iter(pending)), None)
        pending[request_id] = (request, now, approval)

    async def _env_refuse_in_web_mode(self, msg: dict[str, Any], reply: str = "env_error") -> bool:
        if getattr(self, "session", None) is None:
            return False
        await self.send(  # type: ignore[attr-defined]
            {
                "type": reply,
                "request_id": msg.get("request_id"),
                "code": "unavailable",
                "message": "Environment data and local sounding files are available in the "
                "desktop app only.",
            }
        )
        return True

    async def _env_error(
        self, request_id: Any, exc: BaseException, reply: str = "env_error"
    ) -> None:
        code = error_code(exc)
        if code == "internal":
            logger.exception("environment message failed", exc_info=exc)
            text = f"Unexpected error: {exc}"
        else:
            text = str(exc)
        await self.send(  # type: ignore[attr-defined]
            {"type": reply, "request_id": request_id, "code": code, "message": text}
        )

    @staticmethod
    def _env_id(msg: dict[str, Any]) -> str | None:
        rid = msg.get("request_id")
        return rid if isinstance(rid, str) and 0 < len(rid) <= 80 else None

    # -- handlers ---------------------------------------------------------------------------

    async def _handle_env_status(self, msg: dict[str, Any]) -> None:
        if await self._env_refuse_in_web_mode(msg):
            return
        loop = asyncio.get_running_loop()

        def build() -> dict[str, Any]:
            from engine.environment.core import Environment

            env = Environment.default()
            providers = []
            for p in env.registry.all():
                d = p.describe()
                try:
                    d["inventory"] = [i.to_dict() for i in p.inventory()]
                except Exception as exc:  # an inventory must never break the status
                    d["inventory"] = []
                    d["inventory_error"] = str(exc)
                providers.append(d)
            return {"type": "env_status", "providers": providers, **env.status()}

        try:
            await self.send(await loop.run_in_executor(None, build))  # type: ignore[attr-defined]
        except Exception as exc:
            await self._env_error(msg.get("request_id"), exc)

    async def _handle_env_estimate(self, msg: dict[str, Any]) -> None:
        if await self._env_refuse_in_web_mode(msg):
            return
        rid = self._env_id(msg)
        if rid is None:
            await self._env_error(None, InvalidRequest("env_estimate needs a request_id"))
            return
        loop = asyncio.get_running_loop()
        try:
            request = Request.from_dict(msg.get("request") or {})

            def run() -> tuple[dict[str, Any], Approval]:
                from engine.environment.core import Environment

                est = Environment.default().estimate(request)
                return est.to_dict(), est.approve()

            estimate, approval = await loop.run_in_executor(None, run)
            self._env_remember(rid, request, approval)
            await self.send(  # type: ignore[attr-defined]
                {"type": "env_estimate_result", "request_id": rid, "estimate": estimate}
            )
        except Exception as exc:
            await self._env_error(rid, exc)

    async def _handle_env_fetch(self, msg: dict[str, Any]) -> None:
        if await self._env_refuse_in_web_mode(msg):
            return
        rid = self._env_id(msg)
        pending = self._env_pending()
        if rid is None or rid not in pending:
            await self._env_error(
                rid,
                ApprovalRequired(
                    "Nothing was downloaded: ask for an estimate (env_estimate) and show it to "
                    "the user before fetching."
                ),
            )
            return
        request, _, shown = pending[rid]
        approve = msg.get("approve") is True
        loop = asyncio.get_running_loop()
        cancel = threading.Event()
        self._env_jobs[rid] = cancel

        def on_progress(p: dict[str, Any]) -> None:
            if loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(
                self.send({"type": "env_progress", "request_id": rid, **p}),  # type: ignore[attr-defined]
                loop,
            )

        def run() -> dict[str, Any]:
            from engine.environment.core import Environment

            env = Environment.default()
            # the approval is the one made from the estimate the user was shown: a download that
            # grew since then is refused by the framework
            approval = shown if approve else None
            return env.fetch(request, approval, progress=on_progress, cancel=cancel).to_dict()

        try:
            result = await loop.run_in_executor(None, run)
            pending.pop(rid, None)
            await self.send(  # type: ignore[attr-defined]
                {"type": "env_fetch_result", "request_id": rid, "result": result}
            )
        except asyncio.CancelledError:  # the connection went away: stop downloading
            cancel.set()
            raise
        except FetchCancelled:
            await self.send({"type": "env_cancelled", "request_id": rid})  # type: ignore[attr-defined]
        except Exception as exc:
            await self._env_error(rid, exc)
        finally:
            self._env_jobs.pop(rid, None)

    async def _handle_env_cancel(self, msg: dict[str, Any]) -> None:
        self._env_pending()
        rid = self._env_id(msg)
        event = self._env_jobs.get(rid) if rid else None
        if event is not None:
            event.set()

    async def _handle_env_set_settings(self, msg: dict[str, Any]) -> None:
        if await self._env_refuse_in_web_mode(msg):
            return
        changes = msg.get("settings")
        if not isinstance(changes, dict):
            await self._env_error(
                msg.get("request_id"), InvalidRequest("settings must be an object")
            )
            return
        loop = asyncio.get_running_loop()

        def run() -> dict[str, Any]:
            from engine.environment.core import Environment

            return Environment.default().apply_settings(changes)

        try:
            status = await loop.run_in_executor(None, run)
            await self.send({"type": "env_status", **status})  # type: ignore[attr-defined]
        except Exception as exc:
            await self._env_error(msg.get("request_id"), exc)

    async def _handle_env_clear_cache(self, msg: dict[str, Any]) -> None:
        if await self._env_refuse_in_web_mode(msg):
            return
        loop = asyncio.get_running_loop()

        def run() -> dict[str, Any]:
            from engine.environment.core import Environment

            env = Environment.default()
            freed = env.cache.clear()
            return {**env.status(), "freed_bytes": freed}

        try:
            status = await loop.run_in_executor(None, run)
            await self.send({"type": "env_status", **status})  # type: ignore[attr-defined]
        except Exception as exc:
            await self._env_error(msg.get("request_id"), exc)

    async def _handle_env_profile(self, msg: dict[str, Any]) -> None:
        """``{type: "env_profile", request_id, request, lat, lon}``: a model column."""
        if await self._env_refuse_in_web_mode(msg, "sounding_error"):
            return
        rid = msg.get("request_id")
        loop = asyncio.get_running_loop()

        def run() -> dict[str, Any]:
            from engine import sounding
            from engine.environment.core import Environment

            lat, lon = msg.get("lat"), msg.get("lon")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
                raise InvalidRequest("env_profile needs lat and lon in degrees")
            request = Request.from_dict(msg.get("request") or {})
            env = Environment.default()
            # no approval is passed: whatever is not in the cache raises ApprovalRequired
            result = env.fetch(request)
            snd = env.provider(request.provider).profile_at(result, float(lat), float(lon), env)
            return sounding.analyse(snd)

        try:
            result = await loop.run_in_executor(None, run)
            await self.send(  # type: ignore[attr-defined]
                {"type": "sounding_result", "request_id": rid, "sounding": result}
            )
        except Exception as exc:
            logger.info("env_profile failed: %s", exc)
            await self.send(  # type: ignore[attr-defined]
                {"type": "sounding_error", "request_id": rid, "message": str(exc)}
            )

    async def _handle_sounding_open(self, msg: dict[str, Any]) -> None:
        """``{type: "sounding_open", path, format?}`` or ``{..., example: true}``."""
        if await self._env_refuse_in_web_mode(msg, "sounding_error"):
            return
        rid = msg.get("request_id")
        loop = asyncio.get_running_loop()

        def run() -> dict[str, Any]:
            from engine import sounding

            if msg.get("example") is True:
                return sounding.analyse(sounding.weisman_klemp())
            path = msg.get("path")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                raise sounding.SoundingError("sounding_open needs a file path")
            p = Path(path).expanduser()
            if not p.is_absolute() or ".." in p.parts:
                raise sounding.SoundingError("sounding_open: the path must be absolute")
            fmt = msg.get("format", "auto")
            if fmt not in ("auto", "wyoming_text", "sharppy", "iem_json", "csv", "igra2"):
                raise sounding.SoundingError(f"unknown sounding format {fmt!r}")
            units = msg.get("wind_units")
            if units is not None and not isinstance(units, str):
                raise sounding.SoundingError("wind_units must be text such as 'kt' or 'm/s'")
            return sounding.analyse_file(p, fmt, units or None)

        try:
            result = await loop.run_in_executor(None, run)
            await self.send(  # type: ignore[attr-defined]
                {"type": "sounding_result", "request_id": rid, "sounding": result}
            )
        except Exception as exc:
            logger.info("sounding_open failed: %s", exc)
            await self.send(  # type: ignore[attr-defined]
                {
                    "type": "sounding_error",
                    "request_id": rid,
                    "message": f"{type(exc).__name__}: {exc}"
                    if not isinstance(exc, ValueError | ImportError)
                    else str(exc),
                }
            )
