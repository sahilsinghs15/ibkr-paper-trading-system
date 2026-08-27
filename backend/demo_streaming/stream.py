"""Redis Streams helper. Read-only consumers; publisher only XADDs JSON payloads."""

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeout

STREAM_NAME = "positions:stream"


class PositionStream:
    def __init__(
        self,
        redis: Redis,
        stream_name: str = STREAM_NAME,
        *,
        stream_maxlen: int | None = 10000,
    ) -> None:
        self._redis = redis
        self.stream_name = stream_name
        self._stream_maxlen = stream_maxlen

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def xadd(self, payload: dict[str, Any]) -> str:
        fields = {key: _encode(value) for key, value in payload.items()}
        kwargs: dict[str, Any] = {}
        if self._stream_maxlen is not None and self._stream_maxlen > 0:
            kwargs["maxlen"] = self._stream_maxlen
            kwargs["approximate"] = True
        entry_id = await self._redis.xadd(self.stream_name, fields, **kwargs)
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def xread(
        self, last_id: str = "$", block_ms: int = 5000, count: int = 50
    ) -> list[tuple[str, dict[str, Any]]]:
        try:
            rows = await self._redis.xread(
                {self.stream_name: last_id}, block=block_ms, count=count
            )
        except RedisTimeout:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        if not rows:
            return out
        for _name, entries in rows:
            for entry_id, fields in entries:
                decoded_id = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                decoded = {
                    (k.decode() if isinstance(k, bytes) else k): _decode_field(
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in fields.items()
                }
                out.append((decoded_id, decoded))
        return out

    async def listen(
        self, last_id: str = "$", block_ms: int = 5000
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        cursor = last_id
        while True:
            for entry_id, fields in await self.xread(cursor, block_ms=block_ms):
                cursor = entry_id
                yield entry_id, fields


def _encode(value: Any) -> str:
    return json.dumps(value, default=str)


def _decode_field(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Legacy entries used str() (Python repr / bare tokens).
        return value
