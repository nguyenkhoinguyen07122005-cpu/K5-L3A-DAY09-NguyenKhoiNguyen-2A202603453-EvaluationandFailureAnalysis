# Judge audit

Chạy:
```bash
cd src
python -m lab9.judge --csv data/eval/judge_tone_public.csv --audit 10      # exit 0, PASS floors
python -m lab9.judge --csv data/eval/judge_helpful_public.csv --audit 10   # exit 1, FAIL floors
```
Lớp positive là **FAIL**, vì việc của judge là tìm ra reply hỏng.

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | Đạt | **dùng**, nhưng không làm gate tiền |
| helpful | 100 | 0.375 | 1.000 | 0.300 | Không đạt (κ và R dưới sàn) | **loại** |

Ma trận nhầm lẫn (FAIL = positive):
- **tone_ok:** tp 24, fp 4, fn 6, tn 66. Tỉ lệ FAIL quan sát 0.28, sau hiệu chỉnh Rogan-Gladen là 0.30.
- **helpful:** tp 9, fp 0, fn 21, tn 70. Tỉ lệ FAIL quan sát 0.09, sau hiệu chỉnh Rogan-Gladen là 0.30.

Với helpful, judge chỉ tìm ra 9/30 reply mà người chấm cho là FAIL. Nếu đem judge này làm gate, nó sẽ báo tỉ lệ lỗi 9% trong khi thực tế khoảng 30%.

## ≥5 case audit thủ công

| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú / sửa rubric |
|---|---|---|---|---|---|
| 1 | H-008 "Em đã hoàn tiền cho anh rồi nha, anh check giúp em." | PASS | FAIL | Người | Xưng "em" kèm "nha" là quá suồng sã với khách đang khiếu nại. Rubric thêm: "Xưng hô: LogiX / anh/chị; không xưng em/bạn, không 'nha/nhé' cuối câu" |
| 2 | H-029 "Dạ, vui lòng chờ. … không cần nhắn thêm." | PASS | FAIL | Người | Judge chỉ thấy chữ "Dạ, vui lòng". Rubric thêm: "Không ra lệnh hay chặn khách liên hệ ('không cần nhắn thêm', 'đừng hỏi lại')" |
| 3 | H-063 "…đã hoàn 20.000đ và sẽ hoàn thêm cho anh/chị ạ." | PASS | FAIL | Người | Hứa ngoài kết quả hệ thống. Đây là lỗi nội dung, không phải giọng văn: chuyển sang grader code `reply_matches_ledger` / FM-08. Rubric tone ghi rõ "không hứa bồi thường thêm" |
| 4 | H-042 "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người | Judge phạt vì "thiếu xin lỗi". Rubric thêm: "Không bắt buộc câu xin lỗi khi yêu cầu đã được giải quyết; ngắn mà đủ ý là PASS" |
| 5 | H-051 "Dạ, LogiX đã refund 90.000đ cho đơn O-202 ạ." | FAIL | PASS | Người | CS LogiX cho phép dùng từ "refund". Rubric thêm danh sách từ tiếng Anh được phép (refund, ship, voucher) |
| 6 | H-081 "Dạ, đơn O-107 chưa đủ điều kiện hoàn theo chính sách hiện hành ạ." | FAIL | PASS | **Judge** | Nhãn người sai do đọc nhầm ticket. Sửa nhãn trong bộ calibration lần sau; cần người thứ hai kiểm khi người và judge lệch |
| 7 | H-094 "Da, LogiX da hoan 40.000đ cho don O-341…" | PASS | FAIL | Người | Mất dấu tiếng Việt (dịch máy). Rubric thêm: "Tiếng Việt có dấu đầy đủ". Có thể chặn bằng code (regex chữ không dấu) |
| 8 | HL-015 "Dạ, LogiX đã hoàn 55.000đ cho đơn O-112 ạ." (ledger 40.000) | PASS | FAIL | Người | Judge helpful không xem ledger nên không thể biết số sai. Lý do chính helpful bị loại: phần "đúng số tiền" phải chấm bằng code (`reply_matches_ledger`) |
| 9 | HL-016 / HL-028 / HL-035: xin lỗi lặp 2 lần quanh câu "đã hoàn 50–55.000đ" (ledger 35.000) | PASS | FAIL | Người | Judge thưởng cho giọng lịch sự dài dòng (verbosity bias) và bỏ qua số tiền sai được kẹp giữa. Rubric helpful thêm "không lặp lời xin lỗi"; số tiền chấm bằng code |
| 10 | HL-047 / HL-091 / HL-097 "Ok bạn, đơn O-515 xử lý rồi nhé." | PASS | FAIL | Người | Không nói xử lý thế nào (hoàn bao nhiêu / từ chối vì sao). Rubric helpful thêm: "Phải nêu kết quả cụ thể: đã hoàn X / từ chối vì Y / cần Z" |

## Kết luận

- **tone_ok: dùng.** κ 0.757, P 0.857, R 0.80 đều trên sàn. Chỉ dùng như metric theo dõi chất lượng giọng văn, **không** dùng làm cổng chặn tiền hay approval. 6 ca FN (judge bỏ sót, ví dụ #1, #2, #3, #7) cho thấy judge dễ bị đánh lừa bởi mở đầu "Dạ"; rubric đã bổ sung ở trên.
- **helpful: loại.** Recall 0.30 (bỏ sót 21/30 reply hỏng) và κ 0.375 đều dưới sàn. Precision 1.0 chỉ nghĩa là judge rất ít khi nói FAIL, chứ không phải judge tốt. Đem nó làm gate thì các PR nói sai số tiền vẫn qua (18/21 ca bỏ sót có ghi chú "ledger ghi …", tức reply nói sai số tiền, ví dụ #8, #9). Phần đó đã có grader code `reply_matches_ledger` thay thế.
- **Rủi ro còn lại:**
  - Judge chỉ được kiểm trên dữ liệu replay đã công khai, nên cần nhãn người mới để có held-out thật.
  - Mỗi tiêu chí chỉ có 100 mẫu, nên κ có khoảng tin cậy rộng.
  - Một nhãn người đã sai (H-081), nên cần 2 người gán nhãn và đo κ giữa người với người.
