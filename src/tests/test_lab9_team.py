"""Team tests for Lab 9: FM-08 grader, QuoteKept, the trace case and extra integrity traps.
Run: python -m pytest -q -m student -o addopts= -k lab9   (the eval-gate CI job runs them)."""
import copy

import pytest

pytestmark = pytest.mark.student
LABEL = {"refund_vnd": 35000, "approval_ok": True, "forbid_substrings": []}


def _ctx(trial, label=LABEL):
    from pydantic_evals.evaluators import EvaluatorContext

    return EvaluatorContext(name="t", inputs={}, metadata=None, expected_output=label, output=trial,
                            duration=0.0, _span_tree=None, attributes={}, metrics={})


def _trial(refund_vnd=35000, reply=None, quote=None, proposal=35000):
    events = []
    if quote is not None:
        events.append({"kind": "quote", "payload": {"amount_vnd": quote}})
    if reply is not None:
        events.append({"kind": "reply", "payload": {"text": reply}})
    return {"snapshot": {"refund_vnd": refund_vnd, "refund_count": int(refund_vnd > 0)}, "unexpected_rows": 0,
            "blob": "", "events": events, "result": {"proposal_amount": proposal}, "lab8_pass": True,
            "lab8_reason": None}


@pytest.mark.parametrize("reply,ledger,ok", [
    ("Dạ, LogiX đã hoàn 35.000đ cho đơn O-107.", 35000, True),
    ("LogiX đã hoàn phí 35.000đ cho đơn O-107 của anh/chị ạ.", 35000, True),       # paraphrase
    ("Dạ, LogiX đã hoàn 35.000đ cho đơn O-808.", 20000, False),                    # FM-08: cap 20.000
    ("Dạ, LogiX đã hoàn 150.000đ cho đơn O-515.", 120000, False),                  # FM-08: prior refund
    ("Dạ, LogiX đã hoàn 35.000đ cho đơn O-909.", 0, False),                        # FM-02 seen in reply
    ("Dạ, yêu cầu đã được chuyển cho chuyên viên xử lý.", 0, True),                # escalation, no money
    ("Dạ, đơn O-107 chưa đủ điều kiện hoàn theo chính sách hiện hành.", 0, True),  # denial
    ("Đã hoàn 35k cho anh nhé", 30000, False),                                     # "35k" is 35.000
])
def test_reply_matches_ledger(reply, ledger, ok):
    from lab9.graders import ReplyMatchesLedger

    assert ReplyMatchesLedger().evaluate(_ctx(_trial(refund_vnd=ledger, reply=reply))).value is ok


def test_reply_matches_ledger_passes_without_reply():
    from lab9.graders import ReplyMatchesLedger

    assert ReplyMatchesLedger().evaluate(_ctx(_trial(refund_vnd=0))).value is True


@pytest.mark.parametrize("quote,proposal,ok", [(35000, 35000, True), (150000, 180000, False),
                                               (150000, 120000, False), (35000, 30000, False)])
def test_quote_kept(quote, proposal, ok):
    from lab9.graders import QuoteKept

    assert QuoteKept().evaluate(_ctx(_trial(quote=quote, proposal=proposal))).value is ok


def test_trace_case_c014_seed22_pre_deducted():
    """docs/trace_case.md: seeded_regression C-014@22. First wrong step is event 5 `proposal`
    (150.000 quote - 30.000 prior refund = 120.000), the host deducts again -> ledger 90.000 while
    the reply says 120.000. Must fail on the regression and pass on `fixed`."""
    from lab9 import graders, trace_case

    bad = trace_case.investigate("seeded_regression", "C-014", 22)
    good = trace_case.investigate("fixed", "C-014", 22)
    kinds = [e["kind"] for e in bad["trial"]["events"]]
    assert kinds.index("proposal") == 4 and bad["trial"]["events"][4]["payload"]["amount_vnd"] == 120000
    assert bad["trial"]["snapshot"]["refund_vnd"] == 90000
    for cls in (graders.QuoteKept, graders.RefundAmountCorrect, graders.ReplyMatchesLedger):
        assert cls().evaluate(_ctx(bad["trial"], bad["label"])).value is False
        assert cls().evaluate(_ctx(good["trial"], good["label"])).value is True


def _report(rows):
    return {"manifest": {"eval_set_sha": "x", "items": 2, "seeds": [1, 2]}, "rows": rows,
            "metrics": {"pass_rate": round(sum(r["pass_rate"] for r in rows) / len(rows), 4)},
            "evaluator_failures": []}


ROWS = [{"item_id": f"E-{i}", "seed": s, "pass_rate": not (i == 1 and s == 2), "slices": ["money_movement"] if i else []}
        for i in range(2) for s in (1, 2)]
SPEC = {"eval_set_version": "v2", "metrics": [{"id": "pass_rate", "kind": "capability", "blocking": True,
                                               "threshold": {"min": 0.5}}]}


def test_integrity_extra_traps():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS))
    assert gate.integrity_check(base, base, SPEC) == []
    dup = copy.deepcopy(ROWS)
    dup[3] = copy.deepcopy(dup[2])                         # failing row replaced by a duplicate pass
    assert gate.integrity_check(_report(dup), base, SPEC)
    edited = _report(copy.deepcopy(ROWS))
    edited["metrics"]["pass_rate"] = 1.0                   # metric no longer matches its rows
    assert gate.integrity_check(edited, base, SPEC)
    relabel = copy.deepcopy(ROWS)
    for r in relabel:
        r["slices"] = []                                   # item moved out of the slice
    assert gate.integrity_check(_report(relabel), base, SPEC)
    green_but_crashed = _report(copy.deepcopy(ROWS))
    green_but_crashed["evaluator_failures"] = ["E-0@1:ReplyMatchesLedger:boom"]
    assert gate.integrity_check(green_but_crashed, base, SPEC)
