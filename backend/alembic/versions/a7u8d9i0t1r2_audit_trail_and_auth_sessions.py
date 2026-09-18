"""Operator audit trail (audit_events) and server-side auth sessions (auth_sessions).

Replaces the legacy "audit logs" view over ``event_log`` with a dedicated,
append-only ledger of human/operator actions. ``event_log`` itself is untouched
and remains the machine/system event journal used by notifications and pair
timelines.

Immutability is enforced in the database:
- DELETE and TRUNCATE on ``audit_events`` are rejected.
- UPDATE is only permitted to finalize a ``PENDING`` row exactly once, and may
  only touch outcome columns (result, reason, after_state, related, ref_ids,
  completed_at, and target_id when it was previously NULL).

Historical operator actions found in ``event_log`` and ``manual_audit_events``
are imported with ``provenance`` LEGACY_* and ``legacy_ref`` so the import is
idempotent. No actor/IP/session data is invented for them.

Revision ID: a7u8d9i0t1r2
Revises: 44d33de233a2, c9d0e1f2a3b4
Create Date: 2026-09-18 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7u8d9i0t1r2"
down_revision: str | Sequence[str] | None = ("44d33de233a2", "c9d0e1f2a3b4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CATEGORIES = (
    "AUTHENTICATION",
    "AUTHORIZATION",
    "ACCOUNT_ADMIN",
    "SETTINGS",
    "INVENTORY",
    "MANUAL_TRADING",
    "POSITIONS",
    "EMERGENCY",
    "SYSTEM_CONTROL",
)
_RESULTS = (
    "PENDING",
    "SUCCEEDED",
    "ACCEPTED",
    "PARTIAL",
    "REJECTED",
    "DENIED",
    "FAILED",
    "UNKNOWN",
)
_ACTOR_TYPES = ("USER", "SERVICE", "ANONYMOUS", "UNATTRIBUTED")
_PROVENANCE = ("NATIVE", "LEGACY_EVENT_LOG", "LEGACY_MANUAL_AUDIT")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_events_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable_cols text[] := ARRAY[
        'result', 'result_reason', 'after_state', 'related', 'ref_ids',
        'completed_at', 'target_id'
    ];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit_events is append-only: DELETE is not permitted'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- UPDATE: only the single PENDING -> terminal finalization is allowed.
    IF OLD.result <> 'PENDING' THEN
        RAISE EXCEPTION 'audit_events row % is finalized and immutable', OLD.event_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.result = 'PENDING' THEN
        RAISE EXCEPTION 'audit_events finalization must set a terminal result'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.target_id IS NOT NULL AND NEW.target_id IS DISTINCT FROM OLD.target_id THEN
        RAISE EXCEPTION 'audit_events target_id cannot be changed once set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (to_jsonb(NEW) - mutable_cols) IS DISTINCT FROM (to_jsonb(OLD) - mutable_cols) THEN
        RAISE EXCEPTION 'audit_events finalization may only change outcome columns'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
"""

_TRUNCATE_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_events_block_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: TRUNCATE is not permitted'
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
"""


# Legacy operator actions recorded in event_log by the old audit-log writers.
# Only rows that were written by operator-facing routes are imported; machine
# events stay exclusively in event_log.
_IMPORT_EVENT_LOG = """
INSERT INTO audit_events (
    event_id, occurred_at, recorded_at, completed_at, category, action, result,
    summary, actor_type, actor_email, account_id, ibkr_account, target_type,
    target_id, parameters, before_state, after_state, related, ref_ids, context,
    provenance, legacy_ref
)
SELECT
    gen_random_uuid(),
    e.ts,
    clock_timestamp(),
    e.ts,
    m.category,
    m.action,
    m.result,
    m.summary,
    CASE WHEN m.actor_email IS NULL THEN 'UNATTRIBUTED' ELSE 'USER' END,
    m.actor_email,
    CASE WHEN (e.detail->>'account_id') ~ '^[0-9]+$'
         THEN (e.detail->>'account_id')::bigint END,
    NULLIF(e.detail->>'ibkr_account', ''),
    m.target_type,
    m.target_id,
    e.detail,
    CASE WHEN e.kind = 'PAIR_EXIT_THRESHOLDS_UPDATED' THEN e.detail->'old' END,
    CASE WHEN e.kind = 'PAIR_EXIT_THRESHOLDS_UPDATED' THEN e.detail->'new' END,
    jsonb_strip_nulls(jsonb_build_object(
        'operation_id', e.detail->>'operation_id',
        'trade_id', e.detail->>'trade_id',
        'legacy_event_log_id', e.id
    )),
    ARRAY_REMOVE(ARRAY[e.detail->>'operation_id', e.detail->>'trade_id'], NULL),
    jsonb_build_object('legacy', jsonb_build_object(
        'source_table', 'event_log',
        'source_id', e.id,
        'process', e.process,
        'kind', e.kind,
        'note', 'Imported from legacy audit log. Only fields recorded at the time are shown; no IP, session or role was captured.'
    )),
    'LEGACY_EVENT_LOG',
    'event_log:' || e.id
FROM event_log e
CROSS JOIN LATERAL (
    SELECT
        CASE
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED' THEN 'SETTINGS'
            WHEN e.kind = 'PAIR_EXIT_THRESHOLDS_UPDATED' THEN 'POSITIONS'
            WHEN e.kind = 'FIX_BUTTON_CLICKED' THEN 'INVENTORY'
            ELSE 'EMERGENCY'
        END AS category,
        CASE
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED'
                 AND e.detail->>'setting' = 'strategy_allocation' THEN 'ALLOCATION_UPDATED'
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED'
                 AND e.detail->>'setting' LIKE 'symbol_limit:%' THEN 'SYMBOL_LIMIT_SET'
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED'
                 AND e.detail->>'setting' = 'default_symbol_limit' THEN 'DEFAULT_SYMBOL_LIMIT_UPDATED'
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED' THEN 'ACCOUNT_SETTINGS_UPDATED'
            WHEN e.kind = 'PAIR_EXIT_THRESHOLDS_UPDATED' THEN 'PAIR_EXITS_UPDATED'
            WHEN e.kind = 'FIX_BUTTON_CLICKED' THEN 'INVENTORY_FIX_ALIGN'
            WHEN e.kind = 'ENGINE_POSITION_FLATTEN' THEN 'KILL_SWITCH_ENGINE_FLATTEN'
            WHEN e.kind = 'MANUAL_POSITION_FLATTEN' THEN 'KILL_MANUAL_FLATTEN'
            WHEN e.kind = 'ACCOUNT_POSITION_FLATTEN' THEN 'COMPLETE_ACCOUNT_FLATTEN'
            WHEN e.kind = 'KILL_SWITCH_CLEARED' THEN 'KILL_SWITCH_CLEARED'
        END AS action,
        CASE
            WHEN e.kind IN ('ACCOUNT_SETTINGS_CHANGED', 'PAIR_EXIT_THRESHOLDS_UPDATED',
                            'KILL_SWITCH_CLEARED') THEN 'SUCCEEDED'
            WHEN e.kind LIKE '%_POSITION_FLATTEN' THEN 'ACCEPTED'
            ELSE 'UNKNOWN'
        END AS result,
        NULLIF(COALESCE(e.detail->>'operator', e.detail->>'cleared_by'), 'operator') AS actor_email,
        CASE
            WHEN e.kind = 'PAIR_EXIT_THRESHOLDS_UPDATED' THEN 'POSITION'
            WHEN e.kind = 'FIX_BUTTON_CLICKED' THEN 'BROKER_POSITION_LINE'
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED'
                 AND e.detail->>'setting' = 'strategy_allocation' THEN 'ALLOCATION'
            WHEN e.kind = 'ACCOUNT_SETTINGS_CHANGED'
                 AND e.detail->>'setting' LIKE 'symbol_limit:%' THEN 'SYMBOL_LIMIT'
            ELSE 'ACCOUNT'
        END AS target_type,
        COALESCE(
            e.detail->>'trade_id',
            CASE WHEN e.kind = 'FIX_BUTTON_CLICKED'
                 THEN (e.detail->>'symbol') || ' con_id=' || (e.detail->>'con_id') END,
            e.detail->>'allocation_id',
            e.detail->>'symbol',
            e.detail->>'ibkr_account',
            e.detail->>'account_id'
        ) AS target_id,
        'Legacy: ' || e.kind || COALESCE(' — ' || NULLIF(e.detail->>'setting', ''), '')
            || COALESCE(' — ' || NULLIF(e.detail->>'action', ''), '') AS summary
) m
WHERE (
        (e.process = 'config' AND e.kind IN ('ACCOUNT_SETTINGS_CHANGED', 'PAIR_EXIT_THRESHOLDS_UPDATED'))
     OR (e.process = 'reconcile' AND e.kind = 'FIX_BUTTON_CLICKED')
     OR (e.process = 'kill_switch' AND e.kind IN (
            'ENGINE_POSITION_FLATTEN', 'MANUAL_POSITION_FLATTEN', 'ACCOUNT_POSITION_FLATTEN'))
     OR (e.process = 'kill_switch' AND e.kind = 'KILL_SWITCH_CLEARED'
         AND e.detail->>'source' = 'frontend')
  )
ON CONFLICT (legacy_ref) WHERE legacy_ref IS NOT NULL DO NOTHING;
"""

# Legacy manual trading operator actions. user_id was captured server-side at
# the time; the e-mail is resolved from users at import time and flagged so.
_IMPORT_MANUAL_AUDIT = """
INSERT INTO audit_events (
    event_id, occurred_at, recorded_at, completed_at, category, action, result,
    summary, actor_type, actor_user_id, actor_email, account_id, ibkr_account,
    target_type, target_id, parameters, related, ref_ids, context, provenance,
    legacy_ref
)
SELECT
    gen_random_uuid(),
    m.created_at,
    clock_timestamp(),
    m.created_at,
    'MANUAL_TRADING',
    CASE m.action
        WHEN 'MANUAL_ORDER_SUBMITTED' THEN 'MANUAL_ORDER_SUBMIT'
        ELSE 'MANUAL_ORDER_CANCEL'
    END,
    CASE m.action
        WHEN 'MANUAL_ORDER_SUBMITTED' THEN 'SUCCEEDED'
        WHEN 'MANUAL_ORDER_CANCELLED' THEN 'SUCCEEDED'
        WHEN 'MANUAL_ORDER_CANCEL_REQUESTED' THEN 'ACCEPTED'
        ELSE 'REJECTED'
    END,
    'Legacy: ' || m.action || COALESCE(' — ' || NULLIF(m.payload->>'symbol', ''), ''),
    CASE WHEN m.user_id IS NULL THEN 'UNATTRIBUTED' ELSE 'USER' END,
    m.user_id,
    u.email,
    m.account_id,
    a.ibkr_account,
    'MANUAL_ORDER',
    COALESCE(m.payload->>'internal_order_id', m.request_id),
    m.payload,
    jsonb_strip_nulls(jsonb_build_object(
        'internal_order_id', COALESCE(m.payload->>'internal_order_id', m.request_id),
        'broker_order_id', m.payload->>'broker_order_id',
        'legacy_manual_audit_event_id', m.id
    )),
    ARRAY_REMOVE(ARRAY[
        COALESCE(m.payload->>'internal_order_id', m.request_id),
        m.payload->>'broker_order_id'
    ], NULL),
    jsonb_build_object('legacy', jsonb_build_object(
        'source_table', 'manual_audit_events',
        'source_id', m.id,
        'kind', m.action,
        'email_resolved_at_import', u.email IS NOT NULL,
        'note', 'Imported from legacy manual audit trail. user_id was captured at the time; e-mail was resolved from the users table during import. No IP, session or role was captured.'
    )),
    'LEGACY_MANUAL_AUDIT',
    'manual_audit_events:' || m.id
FROM manual_audit_events m
LEFT JOIN users u ON u.id = m.user_id
LEFT JOIN accounts a ON a.id = m.account_id
WHERE m.action IN (
    'MANUAL_ORDER_SUBMITTED', 'MANUAL_ORDER_CANCELLED',
    'MANUAL_ORDER_CANCEL_REQUESTED', 'MANUAL_ORDER_CANCEL_REJECTED'
)
ON CONFLICT (legacy_ref) WHERE legacy_ref IS NOT NULL DO NOTHING;
"""


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("user_role", sa.String(50), nullable=False),
        sa.Column("auth_method", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(32), nullable=True),
        sa.Column("login_ip", postgresql.INET(), nullable=True),
        sa.Column("login_user_agent", sa.Text(), nullable=True),
        sa.Column("login_device_id", sa.String(64), nullable=True),
        sa.Column("login_app_version", sa.String(64), nullable=True),
        sa.Column("login_audit_event_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_auth_sessions_user_created",
        "auth_sessions",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("result_reason", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("actor_role", sa.String(50), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("auth_method", sa.String(32), nullable=True),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("client_device_id", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("http_method", sa.String(8), nullable=True),
        sa.Column("http_path", sa.Text(), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("ibkr_account", sa.String(32), nullable=True),
        sa.Column("target_type", sa.String(48), nullable=True),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column(
            "parameters",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("before_state", postgresql.JSONB(), nullable=True),
        sa.Column("after_state", postgresql.JSONB(), nullable=True),
        sa.Column(
            "related",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "ref_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "provenance",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'NATIVE'"),
        ),
        sa.Column("legacy_ref", sa.String(64), nullable=True),
        sa.CheckConstraint(f"category IN ({_in_list(_CATEGORIES)})", name="ck_audit_events_category"),
        sa.CheckConstraint(f"result IN ({_in_list(_RESULTS)})", name="ck_audit_events_result"),
        sa.CheckConstraint(
            f"actor_type IN ({_in_list(_ACTOR_TYPES)})", name="ck_audit_events_actor_type"
        ),
        sa.CheckConstraint(
            f"provenance IN ({_in_list(_PROVENANCE)})", name="ck_audit_events_provenance"
        ),
    )

    # Indexes follow the investigation query patterns: newest-first browsing,
    # category/action drill-down, per-actor, per-account, per-session, IP/CIDR,
    # and "everything touching identifier X".
    op.create_index(
        "ix_audit_events_occurred",
        "audit_events",
        [sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_events_category_action_time",
        "audit_events",
        ["category", "action", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_actor_time",
        "audit_events",
        ["actor_user_id", sa.text("occurred_at DESC")],
        postgresql_where=sa.text("actor_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_actor_email",
        "audit_events",
        [sa.text("lower(actor_email)"), sa.text("occurred_at DESC")],
        postgresql_where=sa.text("actor_email IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_account_time",
        "audit_events",
        ["account_id", sa.text("occurred_at DESC")],
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_session_time",
        "audit_events",
        ["session_id", "occurred_at"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_client_ip",
        "audit_events",
        [sa.text("client_ip inet_ops")],
        postgresql_using="gist",
        postgresql_where=sa.text("client_ip IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_ref_ids",
        "audit_events",
        ["ref_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_audit_events_correlation",
        "audit_events",
        ["correlation_id"],
        postgresql_where=sa.text("correlation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_pending",
        "audit_events",
        ["occurred_at"],
        postgresql_where=sa.text("result = 'PENDING'"),
    )
    op.create_index(
        "uq_audit_events_legacy_ref",
        "audit_events",
        ["legacy_ref"],
        unique=True,
        postgresql_where=sa.text("legacy_ref IS NOT NULL"),
    )

    op.execute(_GUARD_FUNCTION)
    op.execute(_TRUNCATE_FUNCTION)

    # Import legacy history before the guard trigger is attached (inserts are
    # always allowed, but keep the import independent of trigger semantics).
    op.execute(_IMPORT_EVENT_LOG)
    op.execute(_IMPORT_MANUAL_AUDIT)

    op.execute(
        "CREATE TRIGGER trg_audit_events_guard BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_block_truncate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_guard ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_block_truncate()")
    op.execute("DROP FUNCTION IF EXISTS audit_events_guard()")
    op.drop_table("audit_events")
    op.drop_index("ix_auth_sessions_user_created", table_name="auth_sessions")
    op.drop_table("auth_sessions")
