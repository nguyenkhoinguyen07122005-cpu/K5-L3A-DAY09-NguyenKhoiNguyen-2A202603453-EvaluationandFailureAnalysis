"""Shape B — one ReAct agent with read-only tools; the shape code is the only writer.

Loop: `lab8.runtime.run_react(max_steps=8)`. Each `decide` step is one MockLLM
call (counted in the Budget) followed by a bounded policy that may only pick a
READ tool from the manifest:

    get_claim_view -> lookup_order -> lookup_refund_history -> compute_compensation -> final

Boundaries kept in code, not in the model:
- the model never names an amount: the quote is whatever `compute_compensation`
  returned for the order row the loop read (ticket numbers are ignored, a
  superseded clause cited in the ticket is never fetched as a payout basis);
- the model cannot call `issue_refund` (`dispatch` rejects write tools); after
  the loop the shape re-checks the final against the observed tool results and
  calls `durability.issue_refund` with the FULL quote, passing `crash_at`;
- `lookup_refund_history` is read only for the trace / explanation. The host
  subtracts earlier refunds itself, so the shape never sends a net amount.
- step cap hit -> `failed` (`step_cap`), no write.

Trace contract v1 on every path. Events record tool names, booleans, reasons,
the allowlisted order view and quote, never the ticket text or claim secrets.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8 import durability
from lab8.db import connect
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import tool_cost
from lab8.trace import Trace

SHAPE = "single"
MAX_STEPS = 8


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


class _Agent:
    """Bounded decide(): one model call per step, then a read-only tool choice.

    State lives here because run_react only passes the last tool result back.
    """

    def __init__(self, claim_id: str, seed: int, budget: Budget, trace: Trace) -> None:
        self.claim_id = claim_id
        self.seed = seed
        self.budget = budget
        self.trace = trace
        self.claim: dict[str, Any] | None = None
        self.order_view: dict[str, Any] | None = None
        self.history: dict[str, Any] | None = None
        self.quote: dict[str, Any] | None = None
        self.step = 0

    def _think(self, observation: str) -> None:
        # The model "reads" the state; its text is never parsed for money or tools.
        complete(self.budget, seed=self.seed, purpose="react", prompt=observation)

    def _absorb(self, seen: dict[str, Any]) -> None:
        name, result = seen.get("last_tool"), seen.get("last_result")
        if name is None:
            return
        if name == "get_claim_view":
            self.claim = result
            self.trace.add("observe", {"tool": name, "found": bool(result.get("found")),
                                       "has_order_id": bool(result.get("order_id"))})
        elif name == "lookup_order":
            self.order_view = result
            payload = {"tool": name, "visible": bool(result.get("visible")), "reason": result.get("reason")}
            if result.get("visible"):
                payload["order"] = dict(result["order"])
            self.trace.add("observe", payload)
        elif name == "lookup_refund_history":
            self.history = result
            self.trace.add("observe", {"tool": name, "visible": bool(result.get("visible")),
                                       "refund_count": result.get("refund_count"),
                                       "refunded_vnd": result.get("refunded_vnd")})
        elif name == "compute_compensation":
            self.quote = result
            self.trace.add("observe", {"tool": name, "amount_vnd": int(result["amount_vnd"]),
                                       "reason": result["reason"], "clause_ids": list(result["clause_ids"])})

    def decide(self, seen: dict[str, Any]) -> dict[str, Any]:
        self.step += 1
        self._absorb(seen)
        if self.claim is None:
            self._think(f"step={self.step} need claim view for {self.claim_id}")
            return self._act("get_claim_view", {"claim_id": self.claim_id})
        if not self.claim.get("found"):
            self._think("claim not found")
            return {"final": {"status": "failed", "reason": "unknown_claim"}}
        if not self.claim.get("order_id"):
            # Ticket text is shown to the model, but an order id in it is not trusted.
            self._think(f"ticket without order id: {self.claim.get('ticket', '')}")
            return {"final": {"status": "needs_info", "reason": "missing_order_id"}}
        if self.order_view is None:
            self._think(f"ticket: {self.claim.get('ticket', '')} -> look up the order row")
            return self._act("lookup_order", {"order_id": self.claim["order_id"],
                                              "principal_id": self.claim["principal_id"]})
        if not self.order_view.get("visible"):
            self._think(f"order not visible: {self.order_view.get('reason')}")
            return {"final": {"status": "escalated", "reason": self.order_view.get("reason")}}
        if self.history is None:
            self._think("check refunds already paid on this order")
            return self._act("lookup_refund_history", {"order_id": self.claim["order_id"],
                                                       "principal_id": self.claim["principal_id"]})
        if self.quote is None:
            self._think("quote from the order row, not from the ticket")
            return self._act("compute_compensation", {"order": dict(self.order_view["order"])})
        self._think(f"quote={self.quote['amount_vnd']} reason={self.quote['reason']}")
        if int(self.quote["amount_vnd"]) <= 0:
            return {"final": {"status": "denied", "reason": self.quote["reason"]}}
        return {"final": {"status": "propose_refund", "reason": "entitled"}}

    def _act(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.trace.add("act", {"step": self.step, "tool": tool})
        return {"tool": tool, "args": args}


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))
    trace.add("start", {"shape": SHAPE, "claim_id": claim_id, "seed": seed, "max_steps": MAX_STEPS})
    agent = _Agent(claim_id, seed, budget, trace)

    loop = run_react(conn, budget, observation={"claim": claim_id}, decide=agent.decide,
                     max_steps=MAX_STEPS, worker=False)
    final = loop["final"]
    trace.add("loop_end", {"stopped": loop["stopped"], "steps": agent.step, "final_status": final.get("status")})

    if loop["stopped"] != "final":
        return _done(conn, trace, budget, claim_id, seed, "failed", 0, "step_cap")
    status = final.get("status")
    if status in ("failed", "needs_info", "escalated"):
        complete(budget, seed=seed, purpose="draft", prompt=str(status))
        return _done(conn, trace, budget, claim_id, seed, status, 0, final.get("reason"))

    # Code re-checks the model's final against what the tools actually returned.
    if agent.quote is None or not (agent.order_view or {}).get("visible"):
        return _done(conn, trace, budget, claim_id, seed, "failed", 0, "final_without_quote")
    proposal = int(agent.quote["amount_vnd"])
    if proposal <= 0:
        complete(budget, seed=seed, purpose="draft", prompt="denied")
        return _done(conn, trace, budget, claim_id, seed, "denied", 0, agent.quote["reason"])

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
