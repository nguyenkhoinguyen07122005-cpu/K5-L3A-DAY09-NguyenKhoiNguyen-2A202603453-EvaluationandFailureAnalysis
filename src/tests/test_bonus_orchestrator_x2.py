"""Tests for the R1 ablation variant (src/bonus/orchestrator_two_investigators.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bonus.orchestrator_two_investigators import run_claim
from lab8.durability import InjectedCrash
from lab8.shapes import SHAPES


def test_two_investigators_same_outcome_more_cost(public_db, tmp_path):
    base = SHAPES["orchestrator"]("C-003", seed=11, store_path=str(public_db))
    from tests.conftest import CLAIMS, WORLD
    from lab8.db import init_store

    other = tmp_path / "x2.sqlite"
    init_store(other, WORLD, CLAIMS).close()
    variant = run_claim("C-003", seed=11, store_path=str(other))
    assert variant.status == base.status == "resolved"
    assert variant.proposal_amount == base.proposal_amount == 150000
    assert variant.cost_estimate > base.cost_estimate
    body = json.loads(Path(variant.trace_path).read_text(encoding="utf-8"))
    assert sum(1 for e in body["events"] if e["kind"] == "handoff") == 2


def test_two_investigators_still_forward_crash(public_db):
    with pytest.raises(InjectedCrash):
        run_claim("C-001", seed=11, store_path=str(public_db), crash_at="after_effect_before_checkpoint")
    again = run_claim("C-001", seed=11, store_path=str(public_db))
    assert again.status == "resolved" and len(again.refund_effects) == 1
