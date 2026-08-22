"""Normalize signal_jobs.strategy_id and rebuild idempotency keys.

Revision ID: a4c7e2f10938
Revises: f3a5b7d9e206
Create Date: 2026-08-22 14:00:00.000000

``compute_idempotency_key`` previously kept the alert's raw casing, so
``signal_jobs.strategy_id`` could not be joined against the normalized
``signals.strategy_id``, and one logical signal hashed differently per casing.
This backfills both columns to the canonical form.

Because the old behaviour could admit two rows for one logical signal, the
recomputed keys may collide. Collisions are resolved by keeping the earliest
row and parking the rest as DEAD_LETTER with a suffixed key, so the unique
index holds and nothing is deleted.
"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e2f10938"
down_revision: Union[str, Sequence[str], None] = "f3a5b7d9e206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_STRATEGY_ID = "default_strategy"


def _normalize_strategy_id(value) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text or DEFAULT_STRATEGY_ID


def _compute_key(strategy_id: str, signal_id: str, action: str) -> str:
    raw = f"{strategy_id}:{signal_id}:{action}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT job_id, strategy_id, signal_id, raw_payload, status, received_at "
            "FROM signal_jobs ORDER BY received_at ASC, job_id ASC"
        )
    ).mappings().all()

    if not rows:
        return

    seen: dict[str, str] = {}
    updates: list[dict] = []
    collisions: list[dict] = []

    for row in rows:
        payload = row["raw_payload"] or {}
        action = str(payload.get("action") or "").strip().upper()
        strategy_id = _normalize_strategy_id(row["strategy_id"])
        new_key = _compute_key(strategy_id, row["signal_id"], action)

        if new_key in seen:
            collisions.append(
                {
                    "job_id": row["job_id"],
                    "strategy_id": strategy_id,
                    # Suffix keeps the unique index satisfiable without deleting
                    # the audit row.
                    "new_key": f"{new_key}:dup:{row['job_id']}",
                    "kept": seen[new_key],
                }
            )
            continue

        seen[new_key] = str(row["job_id"])
        updates.append(
            {"job_id": row["job_id"], "strategy_id": strategy_id, "new_key": new_key}
        )

    # Two-phase rewrite: park every key in a temporary namespace first so an
    # intermediate state can never violate the unique index while rows are
    # being renumbered into each other's old keys.
    bind.execute(
        sa.text(
            "UPDATE signal_jobs SET idempotency_key = 'migrating:' || job_id::text"
        )
    )

    for item in updates:
        bind.execute(
            sa.text(
                "UPDATE signal_jobs SET strategy_id = :strategy_id, "
                "idempotency_key = :new_key WHERE job_id = :job_id"
            ),
            item,
        )

    for item in collisions:
        bind.execute(
            sa.text(
                "UPDATE signal_jobs SET strategy_id = :strategy_id, "
                "idempotency_key = :new_key, status = 'DEAD_LETTER', "
                "last_error = :note WHERE job_id = :job_id"
            ),
            {
                "job_id": item["job_id"],
                "strategy_id": item["strategy_id"],
                "new_key": item["new_key"],
                "note": (
                    "Parked by migration a4c7e2f10938: duplicate of job "
                    f"{item['kept']} once strategy_id casing was normalized."
                ),
            },
        )

    print(
        f"[a4c7e2f10938] rekeyed {len(updates)} signal_jobs rows, "
        f"parked {len(collisions)} casing duplicates as DEAD_LETTER"
    )


def downgrade() -> None:
    # The original keys were derived from casing that is no longer recorded
    # anywhere, so the prior digests cannot be reconstructed. Normalized keys
    # remain valid and unique; nothing to undo.
    pass
