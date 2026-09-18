"""Structured audit taxonomy: categories, actions, results and actor types.

Every action belongs to exactly one category. Categories are also enforced by a
database CHECK constraint; actions are validated here so new operations can be
added without a migration.
"""

from __future__ import annotations

from enum import StrEnum


class AuditCategory(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    ACCOUNT_ADMIN = "ACCOUNT_ADMIN"
    SETTINGS = "SETTINGS"
    INVENTORY = "INVENTORY"
    MANUAL_TRADING = "MANUAL_TRADING"
    POSITIONS = "POSITIONS"
    EMERGENCY = "EMERGENCY"
    SYSTEM_CONTROL = "SYSTEM_CONTROL"


class AuditResult(StrEnum):
    PENDING = "PENDING"  # intent durably recorded; outcome not yet known
    SUCCEEDED = "SUCCEEDED"
    ACCEPTED = "ACCEPTED"  # accepted/enqueued; completion is asynchronous
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"  # refused by validation / business rules (4xx)
    DENIED = "DENIED"  # refused by authentication / authorization
    FAILED = "FAILED"  # attempted but errored (5xx / exception)
    UNKNOWN = "UNKNOWN"  # outcome could not be determined (legacy, cancelled request)


class ActorType(StrEnum):
    USER = "USER"  # authenticated human account (JWT session)
    SERVICE = "SERVICE"  # authenticated machine caller (shared secret)
    ANONYMOUS = "ANONYMOUS"  # unauthenticated caller (e.g. failed login)
    UNATTRIBUTED = "UNATTRIBUTED"  # legacy record without captured identity


class AuditAction(StrEnum):
    # Authentication / session security
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    SESSION_TOKEN_REJECTED = "SESSION_TOKEN_REJECTED"
    # Authorization
    ACCESS_DENIED = "ACCESS_DENIED"
    # Account administration
    ACCOUNT_CREATED = "ACCOUNT_CREATED"
    ACCOUNT_DELETED = "ACCOUNT_DELETED"
    # Settings
    ACCOUNT_SETTINGS_UPDATED = "ACCOUNT_SETTINGS_UPDATED"
    DEFAULT_SYMBOL_LIMIT_UPDATED = "DEFAULT_SYMBOL_LIMIT_UPDATED"
    SYMBOL_LIMIT_SET = "SYMBOL_LIMIT_SET"
    SYMBOL_LIMIT_REMOVED = "SYMBOL_LIMIT_REMOVED"
    ALLOCATION_CREATED = "ALLOCATION_CREATED"
    ALLOCATION_UPDATED = "ALLOCATION_UPDATED"
    EXECUTION_SETTINGS_UPDATED = "EXECUTION_SETTINGS_UPDATED"
    MARGIN_SETTINGS_UPDATED = "MARGIN_SETTINGS_UPDATED"
    # Inventory (broker vs ledger reconciliation fixes)
    INVENTORY_FIX_QUANTITY_MISMATCH = "INVENTORY_FIX_QUANTITY_MISMATCH"
    INVENTORY_FIX_LEDGER_GHOST = "INVENTORY_FIX_LEDGER_GHOST"
    INVENTORY_FIX_BROKER_GHOST = "INVENTORY_FIX_BROKER_GHOST"
    INVENTORY_FIX_UNCLASSIFIED = "INVENTORY_FIX_UNCLASSIFIED"
    INVENTORY_BROKER_LINE_FLATTEN = "INVENTORY_BROKER_LINE_FLATTEN"
    INVENTORY_FIX_ALIGN = "INVENTORY_FIX_ALIGN"  # legacy import only (fix type not recorded)
    # Manual trading / order control
    MANUAL_ORDER_SUBMIT = "MANUAL_ORDER_SUBMIT"
    MANUAL_ORDER_CANCEL = "MANUAL_ORDER_CANCEL"
    ENGINE_ORDER_CANCEL = "ENGINE_ORDER_CANCEL"
    # Positions
    CLOSE_PAIR = "CLOSE_PAIR"
    PAIR_EXITS_UPDATED = "PAIR_EXITS_UPDATED"
    # Emergency / kill switch / trading controls
    KILL_SWITCH_ENGINE_FLATTEN = "KILL_SWITCH_ENGINE_FLATTEN"
    KILL_MANUAL_FLATTEN = "KILL_MANUAL_FLATTEN"
    COMPLETE_ACCOUNT_FLATTEN = "COMPLETE_ACCOUNT_FLATTEN"
    KILL_SWITCH_CLEARED = "KILL_SWITCH_CLEARED"
    KILL_SWITCH_ARMED_EXTERNAL = "KILL_SWITCH_ARMED_EXTERNAL"
    TRADING_PAUSED = "TRADING_PAUSED"
    TRADING_RESUMED = "TRADING_RESUMED"
    # System / service lifecycle
    SERVICE_START = "SERVICE_START"
    SERVICE_STOP = "SERVICE_STOP"
    SERVICE_RESTART = "SERVICE_RESTART"
    CREDIT_LEDGER_REFRESH = "CREDIT_LEDGER_REFRESH"


# action -> (category, human label)
ACTION_CATALOG: dict[AuditAction, tuple[AuditCategory, str]] = {
    AuditAction.LOGIN_SUCCEEDED: (AuditCategory.AUTHENTICATION, "Login succeeded / session created"),
    AuditAction.LOGIN_FAILED: (AuditCategory.AUTHENTICATION, "Login failed"),
    AuditAction.LOGOUT: (AuditCategory.AUTHENTICATION, "Logout / session ended"),
    AuditAction.SESSION_TOKEN_REJECTED: (
        AuditCategory.AUTHENTICATION,
        "Token for an ended session was presented",
    ),
    AuditAction.ACCESS_DENIED: (AuditCategory.AUTHORIZATION, "Access denied"),
    AuditAction.ACCOUNT_CREATED: (AuditCategory.ACCOUNT_ADMIN, "Trading account created"),
    AuditAction.ACCOUNT_DELETED: (AuditCategory.ACCOUNT_ADMIN, "Trading account deleted"),
    AuditAction.ACCOUNT_SETTINGS_UPDATED: (AuditCategory.SETTINGS, "Account settings updated"),
    AuditAction.DEFAULT_SYMBOL_LIMIT_UPDATED: (
        AuditCategory.SETTINGS,
        "Default symbol limit updated",
    ),
    AuditAction.SYMBOL_LIMIT_SET: (AuditCategory.SETTINGS, "Symbol risk limit set"),
    AuditAction.SYMBOL_LIMIT_REMOVED: (AuditCategory.SETTINGS, "Symbol risk limit removed"),
    AuditAction.ALLOCATION_CREATED: (AuditCategory.SETTINGS, "Strategy allocation created"),
    AuditAction.ALLOCATION_UPDATED: (AuditCategory.SETTINGS, "Strategy allocation updated"),
    AuditAction.EXECUTION_SETTINGS_UPDATED: (
        AuditCategory.SETTINGS,
        "Execution (square-off/retry) settings updated",
    ),
    AuditAction.MARGIN_SETTINGS_UPDATED: (AuditCategory.SETTINGS, "Margin controls updated"),
    AuditAction.INVENTORY_FIX_QUANTITY_MISMATCH: (
        AuditCategory.INVENTORY,
        "Inventory fix: quantity mismatch",
    ),
    AuditAction.INVENTORY_FIX_LEDGER_GHOST: (AuditCategory.INVENTORY, "Inventory fix: ledger ghost"),
    AuditAction.INVENTORY_FIX_BROKER_GHOST: (AuditCategory.INVENTORY, "Inventory fix: broker ghost"),
    AuditAction.INVENTORY_FIX_UNCLASSIFIED: (
        AuditCategory.INVENTORY,
        "Inventory fix: unclassified difference",
    ),
    AuditAction.INVENTORY_BROKER_LINE_FLATTEN: (
        AuditCategory.INVENTORY,
        "Inventory: broker line flatten",
    ),
    AuditAction.INVENTORY_FIX_ALIGN: (AuditCategory.INVENTORY, "Inventory fix (legacy, type not recorded)"),
    AuditAction.MANUAL_ORDER_SUBMIT: (AuditCategory.MANUAL_TRADING, "Manual order submitted"),
    AuditAction.MANUAL_ORDER_CANCEL: (AuditCategory.MANUAL_TRADING, "Manual order cancel"),
    AuditAction.ENGINE_ORDER_CANCEL: (AuditCategory.MANUAL_TRADING, "Engine order cancel (operator)"),
    AuditAction.CLOSE_PAIR: (AuditCategory.POSITIONS, "Close pair"),
    AuditAction.PAIR_EXITS_UPDATED: (AuditCategory.POSITIONS, "Pair stop/target updated"),
    AuditAction.KILL_SWITCH_ENGINE_FLATTEN: (
        AuditCategory.EMERGENCY,
        "Kill Switch (flatten signal/engine positions)",
    ),
    AuditAction.KILL_MANUAL_FLATTEN: (AuditCategory.EMERGENCY, "Kill Manual (flatten manual positions)"),
    AuditAction.COMPLETE_ACCOUNT_FLATTEN: (
        AuditCategory.EMERGENCY,
        "Complete Flatten (entire IBKR account)",
    ),
    AuditAction.KILL_SWITCH_CLEARED: (AuditCategory.EMERGENCY, "Kill Switch cleared (Start Again)"),
    AuditAction.KILL_SWITCH_ARMED_EXTERNAL: (
        AuditCategory.EMERGENCY,
        "Kill Switch armed by external emergency webhook",
    ),
    AuditAction.TRADING_PAUSED: (AuditCategory.EMERGENCY, "Trading paused (new opens blocked)"),
    AuditAction.TRADING_RESUMED: (AuditCategory.EMERGENCY, "Trading resumed"),
    AuditAction.SERVICE_START: (AuditCategory.SYSTEM_CONTROL, "Service start"),
    AuditAction.SERVICE_STOP: (AuditCategory.SYSTEM_CONTROL, "Service stop"),
    AuditAction.SERVICE_RESTART: (AuditCategory.SYSTEM_CONTROL, "Service restart"),
    AuditAction.CREDIT_LEDGER_REFRESH: (
        AuditCategory.SYSTEM_CONTROL,
        "Instance credit ledger refresh (AWS)",
    ),
}

CATEGORY_LABELS: dict[AuditCategory, str] = {
    AuditCategory.AUTHENTICATION: "Authentication / Security",
    AuditCategory.AUTHORIZATION: "Authorization",
    AuditCategory.ACCOUNT_ADMIN: "Account Administration",
    AuditCategory.SETTINGS: "Settings",
    AuditCategory.INVENTORY: "Inventory",
    AuditCategory.MANUAL_TRADING: "Manual Trading",
    AuditCategory.POSITIONS: "Positions",
    AuditCategory.EMERGENCY: "Emergency / Kill Switch",
    AuditCategory.SYSTEM_CONTROL: "System Monitor Controls",
}


def category_of(action: AuditAction) -> AuditCategory:
    return ACTION_CATALOG[action][0]


# Reconcile diff kinds (see reconcile_service) -> inventory fix actions.
INVENTORY_FIX_ACTIONS: dict[str, AuditAction] = {
    "QTY_DRIFT": AuditAction.INVENTORY_FIX_QUANTITY_MISMATCH,
    "LEDGER_GHOST": AuditAction.INVENTORY_FIX_LEDGER_GHOST,
    "BROKER_ORPHAN": AuditAction.INVENTORY_FIX_BROKER_GHOST,
}
