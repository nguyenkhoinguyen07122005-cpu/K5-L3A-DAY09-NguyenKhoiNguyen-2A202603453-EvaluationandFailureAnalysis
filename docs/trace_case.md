# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-014 --seed 22
python -m lab9.trace_case --shape fixed --claim C-014 --seed 22   # đối chứng
```

## Case của nhóm: `seeded_regression` C-014@22

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-014@22` (item E-014). Report `src/reports/eval_seeded_regression.json`, run ID `E-seeded_regression-20261117-908a`, eval set `908a31192237` |
| Triệu chứng | Grader Lab 8 FAIL (`proposal_mismatch`). `refund_amount_correct` = False: ledger ghi **90.000**, nhãn 120.000. `quote_kept` = False: proposal 120.000 ≠ quote 150.000. `reply_matches_ledger` = False: reply nói **"đã hoàn 120.000đ"** nhưng ledger chỉ có 90.000. Reply trôi chảy và trùng với nhãn, nên nếu chỉ đọc reply sẽ tưởng đúng |
| Bước sai đầu tiên | **#5 `proposal` `{"amount_vnd": 120000}`**: agent tự lấy quote 150.000 trừ 30.000 đã hoàn trước khi gọi host |
| Vì sao là bước *đầu tiên* | #1 `handoff`: allowlist đúng, `facts` rỗng. #2 `investigator`: đọc đơn O-515 từ DB (`delivered_late`, `damage_verified`). #3 `quote`: 150.000 = max(SHIP-LATE-03, DMG-04), giống hệt `fixed`. #4 `refund_history`: `refunded_vnd` 30.000 là dữ liệu đúng; đọc thêm lịch sử không phải lỗi, vì `honest_refactor` cũng đọc và vẫn đúng. Lỗi bắt đầu khi số đọc được bị dùng để **sửa** quote ở #5. Host trừ 30.000 thêm một lần nữa, nên ledger chỉ còn 90.000 |
| Failure mode | **FM-05 pre_deducted**, kéo theo **FM-08 misreport** ở #6 (reply nói số agent tự tính, không phải số host ghi) |
| Đối chứng | `fixed` C-014@22: #3 `quote` 150.000 rồi gọi host luôn với **full quote**. Không có event `refund_history`/`proposal`. Host tự trừ phần đã hoàn → ledger 120.000 = nhãn. #4 reply "đã hoàn 120.000đ" khớp ledger |
| Regression test | `src/tests/test_lab9_team.py::test_trace_case_c014_seed22_pre_deducted`. Test chạy lại cả hai trial, kiểm event thứ 5 là `proposal` 120.000 và ledger 90.000, rồi assert `QuoteKept`, `RefundAmountCorrect`, `ReplyMatchesLedger` đều **False** trên `seeded_regression` và đều **True** trên `fixed`. Đã chạy: pass (nghĩa là bắt được lỗi trên variant lỗi và không báo nhầm trên `fixed`) |
| Chưa chắc chắn | Trace không ghi kết quả host (`issue_refund` không sinh event). Con số 90.000 phải đọc từ ledger snapshot, không có trong trace. Trace cũng không cho biết agent "định" trừ vì hiểu sai chính sách hay vì cố tình tránh trả hai lần; chỉ chứng minh được hành vi. Chỉ chạy trên MockLLM. Với LLM thật, bước #5 có thể xuất hiện không đều giữa các seed, nên phải theo dõi thêm `pass_hat_5` |

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai. Case này là ví dụ điển hình: reply nói đúng số của nhãn, nhưng tiền thật thiếu 30.000.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
