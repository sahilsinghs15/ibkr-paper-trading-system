"""Canonical identifier normalization.

Strategy identities cross three boundaries with different casing rules:
``StrategyHandler.can_handle`` lowercases before comparing, the Model Blue
parser writes a lowercase constant into ``signals.strategy_id``, but the
webhook wrote the raw payload casing into ``signal_jobs.strategy_id``. Any
join between those tables then depended on the alert's capitalization, and the
idempotency hash produced a different digest per casing. Everything that
derives a persisted key or a join column goes through here.
"""

DEFAULT_STRATEGY_ID = "default_strategy"


def normalize_strategy_id(value: object | None) -> str:
    """Canonical form of a strategy identifier: trimmed and lowercased."""
    text = str(value).strip().lower() if value is not None else ""
    return text or DEFAULT_STRATEGY_ID


def normalize_trade_id(value: object | None) -> str:
    """Canonical form of a trade identifier: trimmed, case preserved.

    Trade ids are opaque tokens minted by TradingView and are compared against
    broker-side references, so case is significant and must not be folded.
    """
    return str(value).strip() if value is not None else ""


def normalize_symbol(value: object | None) -> str:
    """Canonical exchange symbol: trimmed and uppercased."""
    return str(value).strip().upper() if value is not None else ""


def normalize_account(value: object | None) -> str:
    """Canonical IBKR account id: trimmed and uppercased."""
    return str(value).strip().upper() if value is not None else ""


def normalize_signal_id(value: object | None) -> str:
    """Canonical signal/trade token for duplicate keys: trimmed, case preserved."""
    return str(value).strip() if value is not None else ""
