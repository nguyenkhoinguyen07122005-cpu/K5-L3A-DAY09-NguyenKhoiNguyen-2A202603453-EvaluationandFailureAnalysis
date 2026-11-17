"""Lê Quang Huy (2A202603470) — host, ledger và approval cho cả 3 shape nhóm viết.

Kiểm tra shape luôn gửi full quote để host tự áp approval và trần theo đơn,
và host ghi policy_denied khi từ chối.
"""

from __future__ import annotations

import pytest

from lab8.db import connect
from lab8.shapes import SHAPES

OURS = ["workflow", "single", "orchestrator"]


def _ledger(store, claim_id):
    conn = connect(store)
    try:
        rows = conn.execute("SELECT amount_vnd FROM refund_ledger WHERE claim_id = ?", (claim_id,)).fetchall()
        denied = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE claim_id = ? AND event_type = 'policy_denied'",
            (claim_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    return [int(r["amount_vnd"]) for r in rows], int(denied)


@pytest.mark.parametrize("shape", OURS)
def test_stale_approval_is_denied_by_host(public_db, shape):
    """T07: approval cũ -> host từ chối, proposal vẫn là full quote 35000, không có tiền."""
    result = SHAPES[shape]("C-007", seed=11, store_path=str(public_db))
    rows, denied = _ledger(public_db, "C-007")
    assert result.status == "denied" and result.proposal_amount == 35000
    assert rows == [] and denied >= 1


@pytest.mark.parametrize("shape", OURS)
def test_order_already_refunded_hits_cap(public_db, shape):
    """T13: đơn đã hoàn đủ -> host từ chối theo trần, không ghi thêm."""
    result = SHAPES[shape]("C-013", seed=22, store_path=str(public_db))
    rows, denied = _ledger(public_db, "C-013")
    assert result.status == "denied" and result.proposal_amount == 35000
    assert rows == [] and denied >= 1


@pytest.mark.parametrize("shape", OURS)
def test_partial_refund_pays_only_difference(public_db, shape):
    """T14: shape gửi full quote 150000, host chỉ ghi phần chênh 120000."""
    result = SHAPES[shape]("C-014", seed=33, store_path=str(public_db))
    rows, _ = _ledger(public_db, "C-014")
    assert result.status == "resolved" and result.proposal_amount == 150000
    assert rows == [120000]


@pytest.mark.parametrize("shape", OURS)
def test_retry_is_idempotent_and_reported_once(public_db, shape):
    first = SHAPES[shape]("C-010", seed=55, store_path=str(public_db))
    second = SHAPES[shape]("C-010", seed=55, store_path=str(public_db))
    rows, _ = _ledger(public_db, "C-010")
    assert first.status == second.status == "resolved"
    assert rows == [35000] and len(second.refund_effects) == 1
