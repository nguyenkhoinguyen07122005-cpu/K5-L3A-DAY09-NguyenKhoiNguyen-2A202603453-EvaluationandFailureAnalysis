"""Shape A — deterministic workflow.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Fixed Python steps; the code decides everything that touches money:

1. Read the claim through `get_claim_view` (allowlisted columns, never SELECT *).
   Unknown claim -> `failed`.
2. Missing order_id -> `needs_info`. No ledger write. The order id is never
   taken from the ticket text.
3. `lookup_order` (principal-scoped). Not visible -> `escalated`; only the
   reason is kept, never phone / address / item / internal note.
4. Quote with `compute_compensation` from the structured order row and the
   current policy only. Numbers written in the ticket are ignored.
5. Quote 0 -> `denied`, `issue_refund` is not called.
6. Quote > 0 -> `durability.issue_refund(conn, claim_id, <full quote>, crash_at=...)`.
   The host checks the claim's own approval, the per-order cap (subtracts earlier
   refunds) and idempotency. `proposal_amount` stays the full quote.
   Host refusal -> `denied` (host wrote `policy_denied`); amount conflict -> `failed`.
7. Committed -> `resolved`.
8. MockLLM is used once, only to draft the customer reply. It never sees the
   amount decision as an input it can change and its text is not parsed.

`InjectedCrash` is not caught: the trace is flushed and the exception re-raised.

Trace contract v1: every return path writes `traces/<run_id>.json` with
{"run_id", "events"}. Events hold booleans, reasons, the allowlisted order view,
the quote and the host outcome. The ticket text, parent_secret, raw headers and
order PII are never written.
"""

from __future__ import annotations

import sqlite3

from lab8 import durability
from lab8.db import connect
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow"


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


def _done(conn, trace, budget, claim_id, seed, status, proposal, reason=None) -> RunResult:
    trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
    return finish(
        conn,
        shape=SHAPE,
        claim_id=claim_id,
        seed=seed,
        status=status,
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=reason,
    )


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))
    trace.add("start", {"shape": SHAPE, "claim_id": claim_id, "seed": seed})

    # 1. Claim view (allowlisted columns only).
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    trace.add("tool", {"name": "get_claim_view", "found": bool(claim.get("found")),
                       "has_order_id": bool(claim.get("order_id"))})
    if not claim.get("found"):
        return _done(conn, trace, budget, claim_id, seed, "failed", 0, "unknown_claim")

    # 2. Missing order id: ask, do not guess from the ticket.
    if not claim.get("order_id"):
        complete(budget, seed=seed, purpose="draft", prompt="needs_info:missing_order_id")
        return _done(conn, trace, budget, claim_id, seed, "needs_info", 0, "missing_order_id")

    # 3. Principal-scoped order lookup.
    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, claim["order_id"], claim["principal_id"])
    if not order_view.get("visible"):
        trace.add("tool", {"name": "lookup_order", "visible": False, "reason": order_view.get("reason")})
        complete(budget, seed=seed, purpose="draft", prompt="escalated")
        return _done(conn, trace, budget, claim_id, seed, "escalated", 0, order_view.get("reason"))
    order = order_view["order"]  # allowlisted ORDER_FIELDS only
    trace.add("tool", {"name": "lookup_order", "visible": True, "order": dict(order)})

    # 4. Quote in code from the structured row and current policy.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", {"amount_vnd": proposal, "reason": quote["reason"],
                        "clause_ids": list(quote["clause_ids"]), "parts": dict(quote["parts"])})

    # 5. Nothing owed: deny without touching the host write path.
    if proposal <= 0:
        complete(budget, seed=seed, purpose="draft", prompt="denied:no_entitlement")
        return _done(conn, trace, budget, claim_id, seed, "denied", 0, quote["reason"])

    # 6. Host gate + ledger. Full quote; the host applies approval / per-order cap.
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
        return _done(conn, trace, budget, claim_id, seed, status, proposal, outcome["reason"])
    return _done(conn, trace, budget, claim_id, seed, "resolved", proposal, None)
