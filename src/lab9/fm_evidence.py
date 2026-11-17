"""Taxonomy evidence (M1): which public claims each single failure mode breaks, and where.

    python -m lab9.fm_evidence                 # every FM, seed 11, all 14 public claims
    python -m lab9.fm_evidence --fm FM-08 --seed 22

Each FM is the Lab 9 variant agent with exactly ONE bug switch on top of the `fixed` behaviour
(no_override), built here with lab9.variant_agent.make_shape. Nothing is registered into
lab9.variants, no Lab 8 file is touched, and every run uses a fresh temp SQLite store.
Output per failing claim: the Lab 8 grader verdict, which lab9 graders fire, and the first
trace event that departs from the `fixed` run of the same claim@seed.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from lab8.console import utf8_stdio
from lab8.db import load_json, load_jsonl
from lab8.shapes import SHAPES
from lab9 import variants
from lab9.eval import WORLD, make_task
from lab9.graders import GRADERS
from lab9.variant_agent import make_shape

# FM code -> bug switch in lab9.variant_agent (names fixed by docs/failure_taxonomy.md).
FM_BUG = {
    "FM-01": "leaky_trace",
    "FM-02": "ignore_denied",
    "FM-03": "ticket_trust",
    "FM-04": "sum_parts",
    "FM-05": "pre_deducted",
    "FM-06": "stale_clause",
    "FM-07": "sibling_refund",
    "FM-08": "misreport",
}


def _grade(trial: dict, label: dict) -> list[str]:
    from pydantic_evals.evaluators import EvaluatorContext

    ctx = EvaluatorContext(name="e", inputs={}, metadata=None, expected_output=label, output=trial,
                           duration=0.0, _span_tree=None, attributes={}, metrics={})
    fired = []
    for mid, cls in GRADERS.items():
        res = cls().evaluate(ctx)
        bad = res.value if mid in ("secret_leak", "approval_bypass") else not res.value
        if bad:
            fired.append(mid)
    return fired


def _first_diff(bad: list[dict], good: list[dict]) -> tuple[int, str, str]:
    for i, (b, g) in enumerate(zip(bad, good), start=1):
        if b.get("kind") != g.get("kind") or b.get("payload") != g.get("payload"):
            return i, b.get("kind", "?"), json.dumps(b.get("payload"), ensure_ascii=False)[:90]
    if len(bad) != len(good):
        i = min(len(bad), len(good)) + 1
        e = (bad + [{}])[i - 1] if len(bad) >= i else {"kind": "(missing)"}
        return i, e.get("kind", "?"), json.dumps(e.get("payload"), ensure_ascii=False)[:90]
    return 0, "-", ""


def evidence(fm: str, seed: int) -> list[dict]:
    variants.register()
    name = f"fm_{fm.lower().replace('-', '')}"
    SHAPES.setdefault(name, make_shape(name, {"no_override", FM_BUG[fm]}))
    claims = load_jsonl(Path("data/public_claims.jsonl"))
    labels = {c["claim_id"]: c["expected"] for c in claims}
    out = []
    with tempfile.TemporaryDirectory(prefix="lab9-fm-", ignore_cleanup_errors=True) as tmp:
        run_bad = make_task(name, load_json(WORLD), claims, labels, Path(tmp) / "bad")
        run_good = make_task("fixed", load_json(WORLD), claims, labels, Path(tmp) / "good")
        (Path(tmp) / "bad").mkdir()
        (Path(tmp) / "good").mkdir()
        for c in claims:
            cid = c["claim_id"]
            bad = run_bad({"claim_id": cid, "seed": seed})
            fired = _grade(bad, labels[cid])
            if bad["lab8_pass"] and not fired:
                continue
            good = run_good({"claim_id": cid, "seed": seed})
            step, kind, payload = _first_diff(bad["events"], good["events"])
            out.append({"claim": cid, "matrix_id": c["matrix_id"], "lab8": "PASS" if bad["lab8_pass"] else "FAIL",
                        "lab8_reason": bad["lab8_reason"], "graders": fired, "first_step": step,
                        "kind": kind, "payload": payload})
    return out


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.fm_evidence")
    ap.add_argument("--fm", choices=sorted(FM_BUG), action="append")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args(argv)
    for fm in args.fm or sorted(FM_BUG):
        rows = evidence(fm, args.seed)
        print(f"== {fm} ({FM_BUG[fm]}) @seed {args.seed}: {len(rows)} claim(s) broken")
        for r in rows:
            print(f"  {r['claim']} {r['matrix_id']} lab8={r['lab8']}({r['lab8_reason']}) graders={r['graders']} "
                  f"first wrong step #{r['first_step']} {r['kind']} {r['payload']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
