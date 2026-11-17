"""R1 ablation: orchestrator with two independent read-only investigators.

Only one variable changes versus `orchestrator`: the number of investigators
(1 -> 2). Model (MockLLM), tools, seeds, corpus, prices and evaluator are fixed.

    python -m lab8.benchmark --shapes orchestrator,orchestrator_x2 \
        --extra-shape orchestrator_x2=bonus.orchestrator_two_investigators:run_claim \
        --cases data/public_claims.jsonl --seeds 11,22,33,44,55 \
        --out reports/bonus_benchmark.json
"""

from __future__ import annotations

from lab8.db import connect
from lab8.schemas import RunResult
from lab8.shapes.orchestrator import _run

SHAPE = "orchestrator_x2"


def run_claim(claim_id: str, *, seed: int, store_path: str, crash_at: str | None = None) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at, investigators=2, shape=SHAPE)
    finally:
        conn.close()
