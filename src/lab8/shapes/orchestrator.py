"""Shape C — orchestrator plus read-only investigators.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Flow:
1. The orchestrator reads only the allowlisted claim view (`get_claim_view`).
   Unknown claim -> `failed`; missing order id -> `needs_info` (no investigator,
   no write).
2. It builds a `handoff.v1` with `make_handoff`: claim id, task, read-only tools
   (`lookup_order`, `lookup_refund_history`, `compute_compensation`). `facts` is
   empty at dispatch time — the ticket text, the delay the customer typed,
   parent_secret and raw headers never enter the handoff.
3. `run_readonly_investigators` runs those tools under the claim's principal.
   Investigators have no write registry (`dispatch(worker=True)`).
4. The orchestrator (code) re-quotes with `compute_compensation` on the
   investigator's structured order view and checks that it matches the
   investigator's quote. Mismatch -> `failed`, no write.
5. Quote 0 -> `denied`. Quote > 0 -> `durability.issue_refund` with the FULL
   quote and `crash_at`. Host refusal -> `denied`; amount conflict -> `failed`.
   `InjectedCrash` is re-raised after the trace is flushed.
6. MockLLM writes a "peer" review note and the customer draft; neither is parsed.

Trace: the sanitized handoff (plus the allowlisted facts the investigator
returned), never the parent context. Written on every return path.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8 import durability
from lab8.db import connect
from lab8.handoff import FACT_KEYS, make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, tool_cost
from lab8.trace import Trace

SHAPE = "orchestrator"
INVESTIGATOR_TOOLS = ["lookup_order", "lookup_refund_history", "compute_compensation"]
# Map the order row onto the handoff fact names (allowlist in code).
_FACT_FROM_ORDER = {
    "delay_days": "delay_days",
    "damage_verified": "damage_verified",
    "carrier": "carrier",
    "order_status": "status",
    "shipping_fee_vnd": "shipping_fee_vnd",
    "paid_vnd": "paid_vnd",
}


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _done(conn, trace, budget, claim_id, seed, status, proposal, reason=None, shape=SHAPE) -> RunResult:
    trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
    return finish(
        conn,
        shape=shape,
        claim_id=claim_id,
        seed=seed,
        status=status,
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=reason,
    )


def _facts(order: dict[str, Any]) -> dict[str, Any]:
    facts = {key: order[src] for key, src in _FACT_FROM_ORDER.items() if src in order}
    return {key: value for key, value in facts.items() if key in FACT_KEYS}


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
    investigators: int = 1,
    shape: str = SHAPE,
) -> RunResult:
    """investigators=1 is the official shape. investigators=2 is the R1 ablation
    (src/bonus/orchestrator_two_investigators.py): a second, independent read-only
    investigator re-reads the order and quotes; any disagreement -> failed."""
    from functools import partial

    budget = Budget()
    run_id = make_run_id(shape, claim_id, seed)
    trace = Trace(run_id)
    trace.add("start", {"shape": shape, "claim_id": claim_id, "seed": seed, "investigators": investigators})
    _done_ = partial(_done, shape=shape)

    # 1. Parent reads the allowlisted claim view only.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    trace.add("parent_view", {"found": bool(claim.get("found")), "has_order_id": bool(claim.get("order_id"))})
    if not claim.get("found"):
        return _done_(conn, trace, budget, claim_id, seed, "failed", 0, "unknown_claim")
    if not claim.get("order_id"):
        complete(budget, seed=seed, purpose="draft", prompt="needs_info:missing_order_id")
        return _done_(conn, trace, budget, claim_id, seed, "needs_info", 0, "missing_order_id")

    # 2. Sanitized handoff: no ticket, no secret, no headers, no customer-typed numbers.
    handoff = make_handoff(
        request_id=run_id,
        claim_id=claim_id,
        task="verify order row, refund history and quote; read-only",
        facts={},
        allowed_tools=INVESTIGATOR_TOOLS,
    )
    trace.add("handoff", handoff)

    # 3. Read-only investigators under the claim's principal.
    report = run_readonly_investigators(conn, budget, handoff, claim["principal_id"])
    tools = report["tools"]
    order_view = tools.get("lookup_order") or {}
    if not order_view.get("visible"):
        trace.add("investigator", {"lookup_order": {"visible": False, "reason": order_view.get("reason")}})
        complete(budget, seed=seed, purpose="draft", prompt="escalated")
        return _done_(conn, trace, budget, claim_id, seed, "escalated", 0, order_view.get("reason"))
    order = dict(order_view["order"])
    history = tools.get("lookup_refund_history") or {}
    worker_quote = tools.get("compute_compensation") or {}
    trace.add("investigator", {
        "facts": _facts(order),
        "refund_history": {"refund_count": history.get("refund_count"), "refunded_vnd": history.get("refunded_vnd")},
        "worker_quote": {"amount_vnd": worker_quote.get("amount_vnd"), "reason": worker_quote.get("reason")},
    })

    # 4. Orchestrator re-quotes in code and cross-checks the worker.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", {"amount_vnd": proposal, "reason": quote["reason"],
                        "clause_ids": list(quote["clause_ids"]), "parts": dict(quote["parts"])})
    complete(budget, seed=seed, purpose="peer", prompt=f"review quote={proposal} reason={quote['reason']}")
    if worker_quote and int(worker_quote.get("amount_vnd", -1)) != proposal:
        return _done_(conn, trace, budget, claim_id, seed, "failed", 0, "investigator_quote_mismatch")
    for index in range(2, investigators + 1):
        second = make_handoff(
            request_id=f"{run_id}-inv{index}",
            claim_id=claim_id,
            task="independent re-check of order row and quote; read-only",
            facts={},
            allowed_tools=["lookup_order", "lookup_policy", "compute_compensation"],
        )
        trace.add("handoff", second)
        extra = run_readonly_investigators(conn, budget, second, claim["principal_id"])["tools"]
        complete(budget, seed=seed, purpose="peer", prompt=f"investigator {index} report")
        extra_quote = int((extra.get("compute_compensation") or {}).get("amount_vnd", -1))
        trace.add("investigator_recheck", {"index": index, "amount_vnd": extra_quote})
        if extra_quote != proposal:
            return _done_(conn, trace, budget, claim_id, seed, "failed", 0, "investigator_disagree")

    if proposal <= 0:
        complete(budget, seed=seed, purpose="draft", prompt="denied:no_entitlement")
        return _done_(conn, trace, budget, claim_id, seed, "denied", 0, quote["reason"])

    # 5. Only the orchestrator writes, through the host.
    trace.add("host_call", {"tool": "durability.issue_refund", "proposed_vnd": proposal})
    try:
        outcome = durability.issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except durability.InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    trace.add("host_result", {"ok": bool(outcome["ok"]), "reason": outcome["reason"],
                              "conflict": bool(outcome["conflict"])})
    complete(budget, seed=seed, purpose="draft", prompt=f"outcome={outcome['reason']}")
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return _done_(conn, trace, budget, claim_id, seed, status, proposal, outcome["reason"])
    return _done_(conn, trace, budget, claim_id, seed, "resolved", proposal, None)
