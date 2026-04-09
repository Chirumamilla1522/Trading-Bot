"""
Patch alpaca-py DataStream._run_forever to stop infinite retry loops on
connection limit / HTTP 429 (and add backoff).

The stock SDK retries immediately after auth failure, which spams the Alpaca
endpoint and can keep the account stuck at the connection limit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

_PATCH_ATTR = "_trading_bot_limit_patch_v1"
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    from alpaca.data.live import websocket as aw

    if getattr(aw.DataStream, _PATCH_ATTR, False):
        _installed = True
        return

    async def _run_forever(self: Any) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        while not any(
            v
            for k, v in self._handlers.items()
            if k not in ("cancelErrors", "corrections")
        ):
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                return
            await asyncio.sleep(0)
        log.info("started %s stream", self._name)
        self._should_run = True
        self._running = False
        backoff_s = 2.0
        max_backoff_s = 120.0
        while True:
            try:
                if not self._should_run:
                    log.info("%s stream stopped", self._name)
                    return
                if not self._running:
                    log.info("starting %s websocket connection", self._name)
                    await self._start_ws()
                    await self._send_subscribe_msg()
                    self._running = True
                    backoff_s = 2.0
                await self._consume()
            except websockets.WebSocketException as wse:
                await self.close()
                self._running = False
                msg = str(wse).lower()
                if "429" in msg or "connection limit" in msg or "too many" in msg:
                    self._should_run = False
                    log.error(
                        "Alpaca data stream: %s — stopping retries. Close other "
                        "market streams, duplicate api_server processes, or notebooks "
                        "using the same API keys.",
                        wse,
                    )
                    return
                log.warning("data websocket error, restarting connection: %s", wse)
                await asyncio.sleep(backoff_s)
                backoff_s = min(max_backoff_s, backoff_s * 1.5)
            except ValueError as ve:
                if "insufficient subscription" in str(ve):
                    await self.close()
                    self._running = False
                    log.exception("error during websocket communication: %s", ve)
                    return
                low = str(ve).lower()
                if "connection limit" in low or "429" in low:
                    await self.close()
                    self._running = False
                    self._should_run = False
                    log.error(
                        "Alpaca data stream auth failed (%s) — stopping retries. "
                        "Your account hit its concurrent WebSocket limit; "
                        "terminate other connections using the same keys.",
                        ve,
                    )
                    return
                log.exception("error during websocket communication: %s", ve)
                await asyncio.sleep(backoff_s)
                backoff_s = min(max_backoff_s, backoff_s * 1.5)
            except Exception as e:
                log.exception("error during websocket communication: %s", e)
                await asyncio.sleep(backoff_s)
                backoff_s = min(max_backoff_s, backoff_s * 1.5)
            finally:
                await asyncio.sleep(0)

    aw.DataStream._run_forever = _run_forever  # type: ignore[method-assign]
    setattr(aw.DataStream, _PATCH_ATTR, True)
    _installed = True
    log.debug("alpaca_stream_limit_patch: installed")
