"""Sanitization applied to every value before it is persisted to the audit trail.

Guarantees:
- Secrets never reach storage: values under sensitive keys, bearer/JWT-looking
  strings and PEM blocks are replaced with ``[REDACTED]``.
- Output is JSON-safe (Decimal -> str to keep precision, datetimes -> ISO-8601,
  UUID/Enum -> str, pydantic models -> dicts).
- Control characters are stripped (log / terminal injection) and strings,
  collections, nesting depth and the total payload size are bounded.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

REDACTED = "[REDACTED]"

MAX_STRING_LEN = 1024
MAX_COLLECTION_ITEMS = 100
MAX_DEPTH = 6
MAX_PAYLOAD_BYTES = 32 * 1024

# Matched against the key lower-cased with non-alphanumerics removed.
_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "apikey",
    "accesskey",
    "privatekey",
    "cookie",
    "credential",
    "jwt",
    "bearer",
    "signature",
    "passphrase",
)
_SENSITIVE_KEY_EXACT = {"pwd", "pin", "otp", "auth", "sessionsecret"}

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
# C0 controls except TAB, plus DEL and the C1 range.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized in _SENSITIVE_KEY_EXACT:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_SUBSTRINGS)


def clean_text(value: str, max_len: int = MAX_STRING_LEN) -> str:
    """Strip control characters, redact embedded credentials and bound length."""
    text = _CONTROL_RE.sub(" ", value)
    text = _PEM_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    if len(text) > max_len:
        text = f"{text[:max_len]}…[truncated {len(text) - max_len} chars]"
    return text


def optional_text(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(str(value), max_len=max_len).strip()
    return cleaned or None


def _sanitize(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return _sanitize(value.value, depth)
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if depth >= MAX_DEPTH:
        return "[max depth reached]"

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _sanitize(model_dump(mode="python"), depth)

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                out["_truncated_keys"] = len(value) - MAX_COLLECTION_ITEMS
                break
            key = clean_text(str(raw_key), max_len=128)
            out[key] = REDACTED if is_sensitive_key(key) else _sanitize(item, depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        cleaned = [_sanitize(item, depth + 1) for item in items[:MAX_COLLECTION_ITEMS]]
        if len(items) > MAX_COLLECTION_ITEMS:
            cleaned.append(f"[{len(items) - MAX_COLLECTION_ITEMS} more items truncated]")
        return cleaned
    return clean_text(repr(value))


def sanitize_payload(value: Any) -> Any:
    """Sanitize an arbitrary structure for JSONB persistence (bounded size)."""
    cleaned = _sanitize(value, 0)
    encoded = json.dumps(cleaned, default=str, ensure_ascii=False)
    size = len(encoded.encode("utf-8"))
    if size <= MAX_PAYLOAD_BYTES:
        return cleaned
    return {
        "_truncated": True,
        "_original_bytes": size,
        "preview": encoded[:2000],
    }


def sanitize_dict(value: Any) -> dict[str, Any]:
    """Like :func:`sanitize_payload` but always returns a dict (wraps scalars)."""
    if value is None:
        return {}
    cleaned = sanitize_payload(value)
    return cleaned if isinstance(cleaned, dict) else {"value": cleaned}
