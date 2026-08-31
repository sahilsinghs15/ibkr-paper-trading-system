"""Async Telegram client — never blocks trading, never crashes watchdog."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        timeout: float = 5.0,
        max_retries: int = 3,
        rate_limit_per_sec: float = 1.0,
        enabled: bool = False,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limit_per_sec = rate_limit_per_sec
        self.enabled = enabled and bool(bot_token and chat_id)
        self._last_send_ts: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)

    async def send_message(self, text: str, parse_mode: str | None = "HTML") -> bool:
        if not self.configured:
            logger.debug("Telegram not configured — skipping send")
            return False
        # rate limiting
        import time

        now = time.monotonic()
        min_interval = 1.0 / self.rate_limit_per_sec if self.rate_limit_per_sec > 0 else 0
        wait = self._last_send_ts + min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send_ts = time.monotonic()

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        return True
                    if resp.status_code in (429, 500, 502, 503, 504):
                        # transient
                        logger.warning("Telegram transient %s attempt %d/%d", resp.status_code, attempt, self.max_retries)
                        await asyncio.sleep(min(2**attempt, 10))
                        continue
                    logger.error("Telegram send failed HTTP %s: %s", resp.status_code, resp.text[:500])
                    return False
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("Telegram send exception attempt %d/%d: %s", attempt, self.max_retries, exc)
                await asyncio.sleep(min(2**attempt, 5))
        logger.error("Telegram send exhausted retries: %s", last_exc)
        return False
