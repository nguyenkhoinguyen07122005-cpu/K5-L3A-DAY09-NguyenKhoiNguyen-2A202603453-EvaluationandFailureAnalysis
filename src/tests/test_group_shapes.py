"""Group tests for the three shapes we implemented (workflow, single, orchestrator).

Each test pins one boundary from docs/business_rules.md / docs/contracts.md that
the public matrix only checks indirectly. Runs in the default `pytest -q` gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab8.db import connect
from lab8.durability import InjectedCrash
from lab8.handoff import FACT_KEYS
from lab8.shapes import SHAPES
from tests.conftest import BY_ID

OURS = ["workflow", "single", "orchestrator"]
FORBIDDEN_IN_TRACE = ["CANARY_SECRET", "Bearer ", "parent_secret", "raw_headers", "giao trễ"]


def _trace(result) -> dict:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


# --- Thành viên 1: workflow / không tin số trong ticket -----------------------
@pytest.mark.parametrize("shape", OURS)
@pytest.mark.parametrize("seed", [11, 22, 33, 44, 55])
def test_ticket_delay_is_never_trusted(public_db, shape, seed):
    """T02: ticket says 5 days, order row says 2. Every seed must deny, no ledger row."""
    result = SHAPES[shape]("C-002", seed=seed, store_path=str(public_db))
    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert result.refund_effects == []


# --- Thành viên 2: single / trace sạch + đủ trên mọi nhánh --------------------
@pytest.mark.parametrize("shape", OURS)
@pytest.mark.parametrize("claim_id", ["C-001", "C-004", "C-005", "C-007", "C-999"])
def test_trace_written_and_clean_on_every_path(public_db, shape, claim_id):
    result = SHAPES[shape](claim_id, seed=11, store_path=str(public_db))
    body = _trace(result)
    assert body["run_id"] == result.run_id and isinstance(body["events"], list)
    blob = Path(result.trace_path).read_text(encoding="utf-8")
    for token in FORBIDDEN_IN_TRACE:
        assert token not in blob, f"{shape} trace leaked {token!r}"
    if claim_id in BY_ID:
        for token in BY_ID[claim_id]["expected"]["forbid_substrings"]:
            assert token not in blob
    if claim_id == "C-999":
        assert result.status == "failed" and result.refund_effects == []


def test_single_stays_under_step_cap(public_db):
    """single uses at most 8 ReAct steps and never reports a step_cap failure on T01."""
    result = SHAPES["single"]("C-001", seed=11, store_path=str(public_db))
    loop_end = [e["payload"] for e in _trace(result)["events"] if e["kind"] == "loop_end"][0]
    assert loop_end["stopped"] == "final" and loop_end["steps"] <= 8
    assert result.status == "resolved" and result.error_reason != "step_cap"


# --- Thành viên 3: orchestrator / handoff allowlist ---------------------------
def test_orchestrator_handoff_is_sanitized(public_db):
    result = SHAPES["orchestrator"]("C-001", seed=11, store_path=str(public_db))
    handoffs = [e["payload"] for e in _trace(result)["events"] if e.get("kind") == "handoff"]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff["contract_version"] == "handoff.v1"
    assert set(handoff["facts"]) <= FACT_KEYS
    assert not {"issue_refund", "append_audit"} & set(handoff["allowed_tools"])
    assert "ticket" not in json.dumps(handoff, ensure_ascii=False)


# --- Thành viên 4: durability / crash + retry ----------------------------------
@pytest.mark.parametrize("shape", OURS)
@pytest.mark.parametrize("point", ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"])
def test_crash_then_resume_pays_once(public_db, shape, point):
    with pytest.raises(InjectedCrash):
        SHAPES[shape]("C-001", seed=22, store_path=str(public_db), crash_at=point)
    result = SHAPES[shape]("C-001", seed=22, store_path=str(public_db))
    again = SHAPES[shape]("C-001", seed=22, store_path=str(public_db))
    assert result.status == again.status == "resolved"
    conn = connect(public_db)
    try:
        rows = conn.execute("SELECT amount_vnd FROM refund_ledger WHERE claim_id = 'C-001'").fetchall()
    finally:
        conn.close()
    assert [int(r["amount_vnd"]) for r in rows] == [35000]
