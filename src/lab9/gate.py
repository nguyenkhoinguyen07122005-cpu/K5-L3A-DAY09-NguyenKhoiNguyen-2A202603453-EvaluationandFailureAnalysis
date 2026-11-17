"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


SAFETY_COUNTS = {"secret_leak", "approval_bypass"}


def _bad_number(v) -> bool:
    return isinstance(v, float) and (math.isnan(v) or math.isinf(v))


def _graded_ids(spec: dict, rows: list[dict], base_rows: list[dict]) -> list[str]:
    """Spec metrics that are graded per row (a grader column exists in the baseline rows)."""
    from lab9.graders import GRADERS
    cols = set().union(*(r.keys() for r in base_rows)) if base_rows else set()
    return [m["id"] for m in spec.get("metrics", []) if m["id"] in GRADERS and (m["id"] in cols or not cols)]


def _recompute(rows: list[dict], metric_ids: list[str], k_values) -> dict:
    """Metric values a report MUST carry given its own rows (same formulas as lab9.eval.aggregate)."""
    out: dict = {}
    for m in metric_ids:
        vals = [r.get(m) for r in rows if r.get(m) is not None]
        out[m] = sum(bool(v) for v in vals) if m in SAFETY_COUNTS else (
            round(sum(bool(v) for v in vals) / len(vals), 4) if vals else None)
    by_item: dict[str, list[bool]] = {}
    for r in rows:
        by_item.setdefault(r["item_id"], []).append(bool(r.get("pass_rate")))
    if any("pass_rate" in r for r in rows):
        out["pass_5"] = round(sum(all(v) for v in by_item.values()) / len(by_item), 4) if by_item else None
        for k in k_values or []:
            if by_item and min(len(v) for v in by_item.values()) >= k:
                try:
                    for name, fn in (("pass_at", stats.pass_at_k), ("pass_hat", stats.pass_hat_k)):
                        est = [fn(len(v), sum(v), k) for v in by_item.values()]
                        out[f"{name}_{k}"] = round(sum(est) / len(est), 4)
                except (NotImplementedError, ValueError):
                    pass
    if rows and all(isinstance(r.get("cost"), (int, float)) for r in rows):
        passes = sum(bool(r.get("pass_rate")) for r in rows)
        out["cost_per_success"] = round(sum(r["cost"] for r in rows) / passes, 4) if passes else None
    return out


def _ratchet(spec: dict, base_spec: dict) -> list[str]:
    """Same eval-set version: a threshold may only get stricter. Returns loosening problems."""
    if spec.get("eval_set_version") != base_spec.get("eval_set_version"):
        return []                                  # new eval-set version = new baseline, new floors
    problems = []
    for key in ("eval_set", "labels", "seeds", "adversarial_pack"):
        if base_spec.get(key) != spec.get(key):
            problems.append(f"ratchet: spec {key} changed from {base_spec.get(key)!r} to {spec.get(key)!r}")
    b_bs, s_bs = base_spec.get("bootstrap") or {}, spec.get("bootstrap") or {}
    if s_bs.get("confidence", 0.95) < b_bs.get("confidence", 0.95):
        problems.append("ratchet: bootstrap confidence lowered")
    if s_bs.get("n_resamples", 1000) < b_bs.get("n_resamples", 1000):
        problems.append("ratchet: bootstrap n_resamples lowered")

    def compare(kind: str, name: str, old: dict, new: dict | None) -> None:
        if new is None:
            problems.append(f"ratchet: {kind} {name} removed")
            return
        if old.get("blocking") and not new.get("blocking"):
            problems.append(f"ratchet: {kind} {name} no longer blocking")
        o_th = old.get("threshold", old)
        n_th = new.get("threshold", new)
        if "max" in o_th and ("max" not in n_th or n_th["max"] > o_th["max"]):
            problems.append(f"ratchet: {kind} {name} max {o_th['max']} -> {n_th.get('max')}")
        if "min" in o_th and ("min" not in n_th or n_th["min"] < o_th["min"]):
            problems.append(f"ratchet: {kind} {name} min {o_th['min']} -> {n_th.get('min')}")
        if "max_increase" in o_th and ("max_increase" not in n_th or n_th["max_increase"] > o_th["max_increase"]):
            problems.append(f"ratchet: {kind} {name} max_increase {o_th['max_increase']} -> {n_th.get('max_increase')}")
        if o_th.get("no_significant_drop") and not n_th.get("no_significant_drop"):
            problems.append(f"ratchet: {kind} {name} dropped no_significant_drop")
        if kind == "slice" and old.get("metric") != new.get("metric"):
            problems.append(f"ratchet: slice {name} metric {old.get('metric')} -> {new.get('metric')}")

    new_metrics = {m["id"]: m for m in spec.get("metrics", [])}
    for m in base_spec.get("metrics", []):
        compare("metric", m["id"], m, new_metrics.get(m["id"]))
    new_slices = {(sl["name"], sl.get("metric")): sl for sl in spec.get("slices") or []}
    for sl in base_spec.get("slices") or []:
        compare("slice", f"{sl['name']}/{sl.get('metric')}", sl, new_slices.get((sl["name"], sl.get("metric"))))
    return problems


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Catches: NaN/inf metric values; row count != items x seeds; duplicated (item, seed) rows;
    an (item, seed) set or seed list different from the baseline's; evaluator failures; a graded
    metric that is None / missing / not a boolean in some row; reported metrics that do not
    match the rows they claim to summarise; slice tags that differ from the baseline's; and,
    with base_spec, any loosening of the spec on the same eval-set version (ratchet)."""
    problems: list[str] = []
    rows = pr.get("rows")
    base_rows = base.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return ["report has no rows"]
    metrics = pr.get("metrics")
    if not isinstance(metrics, dict):
        return ["report has no metrics"]

    # 1. Values that cannot be compared: NaN / inf, anywhere in the reported metrics.
    for k, v in metrics.items():
        if _bad_number(v):
            problems.append(f"metric {k} is {v} (NaN/inf cannot be compared with a threshold)")
        elif v is not None and not isinstance(v, (int, float)):
            problems.append(f"metric {k} is not a number: {v!r}")

    # 2. Shape of the sample: items x seeds, no duplicates, same (item, seed) set as the baseline.
    man, bman = pr.get("manifest") or {}, base.get("manifest") or {}
    seeds = list(man.get("seeds") or [])
    want_n = int(man.get("items") or 0) * len(seeds)
    if len(rows) != want_n:
        problems.append(f"{len(rows)} rows but manifest says {man.get('items')} items x {len(seeds)} seeds = {want_n}")
    if sorted(seeds) != sorted(bman.get("seeds") or []):
        problems.append(f"seeds {seeds} differ from the baseline's {bman.get('seeds')}")
    if man.get("items") != bman.get("items"):
        problems.append(f"manifest items {man.get('items')} differ from the baseline's {bman.get('items')}")
    keys = Counter((r.get("item_id"), r.get("seed")) for r in rows)
    dups = sorted(k for k, n in keys.items() if n > 1)
    if dups:
        problems.append(f"{len(dups)} duplicated (item, seed) row(s), first {dups[0]}")
    base_keys = {(r.get("item_id"), r.get("seed")) for r in base_rows}
    missing, extra = base_keys - set(keys), set(keys) - base_keys
    if missing:
        problems.append(f"{len(missing)} (item, seed) row(s) of the baseline missing, first {sorted(missing)[0]}")
    if extra:
        problems.append(f"{len(extra)} (item, seed) row(s) not in the baseline, first {sorted(extra)[0]}")
    if any(r.get("seed") not in seeds for r in rows):
        problems.append("rows carry seeds that are not in the manifest")

    # 3. Graders must have run on every row.
    if pr.get("evaluator_failures"):
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s): {pr['evaluator_failures'][0]}")
    graded = _graded_ids(spec, rows, base_rows)
    for m in graded:
        bad = [r for r in rows if not isinstance(r.get(m), bool)]
        if bad:
            problems.append(f"metric {m}: {len(bad)} row(s) with no boolean grade (None/missing/crashed), "
                            f"first {bad[0].get('item_id')}@{bad[0].get('seed')}")

    # 4. Reported numbers must be what the rows say (hand-edited / recomputed-on-a-subset metrics).
    graded_present = [m for m in graded if all(m in r for r in rows)]
    recomputed = _recompute(rows, graded_present, spec.get("pass_k"))
    for k, v in recomputed.items():
        if k in metrics and v is not None and isinstance(metrics[k], (int, float)) and not _bad_number(metrics[k]):
            if abs(float(metrics[k]) - float(v)) > 1e-3:
                problems.append(f"metric {k} = {metrics[k]} but its rows give {v}")
    for m in spec.get("metrics", []):
        if m.get("blocking") and m["id"] in recomputed and metrics.get(m["id"]) is None \
                and recomputed[m["id"]] is not None:
            problems.append(f"blocking metric {m['id']} missing from the report but computable from rows")

    # 5. Slice tags are derived from labels; relabelling an item hides it from the slice gate.
    base_slices = {r["item_id"]: sorted(r.get("slices") or []) for r in base_rows}
    moved = sorted({r["item_id"] for r in rows
                    if r.get("item_id") in base_slices and sorted(r.get("slices") or []) != base_slices[r["item_id"]]})
    if moved:
        problems.append(f"slice tags differ from the baseline for {len(moved)} item(s), first {moved[0]}")

    # 6. Ratchet: the PR may not loosen the spec it is judged by.
    if base_spec is not None:
        problems += _ratchet(spec, base_spec)
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for sl in spec.get("slices") or []:
        name, metric = sl["name"], sl.get("metric", "pass_rate")
        sid = f"slice:{name}/{metric}"
        a, b = per_item(pr, metric, slice_name=name), per_item(base, metric, slice_name=name)
        verdict, note, val = "ok", "", None
        if not a:
            verdict = "missing"
        else:
            val = round(sum(a.values()) / len(a), 4)
            if "min" in sl and val < sl["min"]:
                verdict = "fail"
            if "max" in sl and val > sl["max"]:
                verdict = "fail"
            if sl.get("no_significant_drop"):
                keys = sorted(set(a) & set(b))
                if keys:
                    d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                       n_resamples=bs.get("n_resamples", 1000),
                                                       rng=bs.get("rng", 2027),
                                                       confidence=bs.get("confidence", 0.95))
                    note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                    if hi < 0:
                        verdict = "fail"
                else:
                    verdict = "missing"
        if verdict != "ok" and sl.get("blocking"):
            blocked.append(sid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if sl.get("blocking") else "warn")
        th = {k: sl[k] for k in ("min", "max", "no_significant_drop") if k in sl}
        lines.append(f"  {sid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label} (n={len(a)} items)")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
