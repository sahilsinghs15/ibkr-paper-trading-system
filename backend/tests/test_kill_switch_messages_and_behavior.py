"""Regression tests verifying kill-switch messages do not contain hardcoded 'paper'

and ensuring account information and idempotency behavior remain strictly intact.
"""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
)
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    is_account_kill_switch_active,
)


def test_kill_switch_modals_do_not_contain_hardcoded_paper_wording():
    """Verify frontend KillSwitchModal and StartAgainModal do not hardcode 'paper account'."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    kill_switch_modal_path = (
        repo_root / "frontend" / "src" / "components" / "KillSwitchModal.tsx"
    )
    start_again_modal_path = (
        repo_root / "frontend" / "src" / "components" / "StartAgainModal.tsx"
    )

    assert kill_switch_modal_path.exists(), f"Missing {kill_switch_modal_path}"
    assert start_again_modal_path.exists(), f"Missing {start_again_modal_path}"

    ks_content = kill_switch_modal_path.read_text(encoding="utf-8")
    sa_content = start_again_modal_path.read_text(encoding="utf-8")

    # Prove that 'paper account' is absent
    assert "paper account" not in ks_content.lower(), (
        "KillSwitchModal contains hardcoded 'paper account' wording!"
    )
    assert "paper account" not in sa_content.lower(), (
        "StartAgainModal contains hardcoded 'paper account' wording!"
    )

    # Prove that account information is properly displayed
    assert "account{' '}" in ks_content or "account <strong>{ibkrAccount}</strong>" in ks_content
    assert "{ibkrAccount}" in ks_content
    assert "{ibkrAccount}" in sa_content


@pytest.mark.asyncio
async def test_kill_switch_idempotency_and_account_metadata_intact(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify kill switch idempotency and correct account handling for both live and paper formats."""
    # Test with a live format account ID (e.g. U7211090) and paper format (e.g. DU123456)
    for acct_prefix in ["U", "DU"]:
        test_id = uuid4().hex[:6]
        ibkr_acc = f"{acct_prefix}{test_id}"

        async with session_factory() as session, session.begin():
            acc = AccountModel(
                name=f"KillSwitchMsgTest-{ibkr_acc}",
                ibkr_account=ibkr_acc,
                total_margin=Decimal("100000.00"),
            )
            session.add(acc)
            await session.flush()
            acc_id = acc.id

        svc = KillSwitchService(session_factory=session_factory)

        # 1. Trigger square off: creates new operation and arms kill switch
        op1, created1 = await svc.initiate_square_off(
            account_id=acc_id, requested_by="operator"
        )
        assert created1 is True
        assert op1.status == KILL_SWITCH_STATUS_ACTIVATING
        assert op1.account_id == acc_id
        assert is_account_kill_switch_active(acc_id) is True

        # 2. Idempotent retry: returns existing operation without duplicate creation
        op2, created2 = await svc.initiate_square_off(
            account_id=acc_id, requested_by="operator"
        )
        assert created2 is False
        assert op2.operation_id == op1.operation_id
        assert is_account_kill_switch_active(acc_id) is True

        # 3. Clear kill switch: disarms active flag
        cleared_count = await clear_account_kill_switch(
            session_factory=session_factory,
            account_id=acc_id,
            cleared_by="operator",
        )
        assert cleared_count == 1
        assert is_account_kill_switch_active(acc_id) is False
