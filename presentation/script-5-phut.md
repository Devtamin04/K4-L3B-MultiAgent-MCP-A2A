# Script thuyết trình 5 phút — Hệ thống multi-agent L3B

Mở file `presentation/l3b-flow.drawio`. Mỗi phần bên dưới ứng với một trang (tab) của diagram.
Phần lời nói (các đoạn trích dẫn) dài khoảng 1.000 âm tiết, tức khoảng 200 âm tiết/phút, hơi gấp.
Nếu nói chậm thì bỏ bớt phần giải thích ở bước một và bước sáu của trang 2.

---

## Trang 1 · Tổng quan kiến trúc (0:00 – 1:15)

> Chào mọi người. Bài toán của nhóm là điều tra khiếu nại đơn hàng thương mại điện tử: mỗi case
> gồm một số claim của khách, ví dụ "giao trễ" hay "bị trừ tiền hai lần", kèm mã đơn mà khách khai
> và vài mã đơn ứng viên. Nhiệm vụ là kết luận thật sự đã xảy ra chuyện gì, ai chịu trách nhiệm và
> cần hoàn bao nhiêu tiền.
>
> Điểm khó là lời khai của khách **không đáng tin**: có mã đơn giả, có claim sai, và lời nhắn có
> thể chứa cả instruction giả. Vì vậy nhóm chọn một thiết kế **hoàn toàn deterministic, không dùng
> LLM**. Mọi kết luận chỉ được rút ra từ evidence do MCP server của ban tổ chức trả về.
>
> Hệ thống có một **Coordinator** ở trung tâm. Coordinator không có quyền gọi tool, chỉ lập kế
> hoạch và giao việc. Có năm agent chuyên trách, mỗi agent chỉ được gọi đúng những tool của mình:
> Entity, Order, Shipment, Payment và Policy. Nếu gọi sai quyền, hệ thống ném `PermissionError`
> ngay. Thêm hai agent không gọi tool: **Conflict resolver** xử lý mâu thuẫn dữ liệu, và
> **Verifier** kiểm tra lại trước khi xuất kết quả.
>
> Mọi lời gọi tool đi qua một lớp cache gắn với từng lượt chạy, nên rớt mạng thì chạy lại cũng
> không bị gọi trùng. Đầu ra gồm file JSON kết luận, file trace ghi mọi bước giao việc và handoff,
> và một báo cáo markdown giải thích từng case.

## Trang 2 · Luồng xử lý một case (1:15 – 2:45)

> Đây là luồng xử lý một case, gồm bảy bước.
>
> **Bước một**, Coordinator đọc các claim để chọn kế hoạch gọi tool. Ví dụ claim giao hàng thì cần
> dữ liệu vận chuyển, còn claim hoàn tiền thì cần refund timeline.
>
> **Bước hai**, Entity agent tra lịch sử đơn của khách. Ứng viên nào không có trong lịch sử hoặc sai
> định dạng thì bị loại luôn, **không tốn thêm call nào** cho đơn giả.
>
> **Bước ba**, bốn agent Order, Shipment, Payment và Policy chạy **song song** để thu thập evidence.
> Mỗi case dùng đúng **sáu call**, như bảng phía dưới.
>
> **Bước bốn** là phần quan trọng nhất: Conflict resolver. Trong dữ liệu, cùng một mã đơn có thể có
> nhiều lần mua khác nhau, và các nguồn có thể trỏ tới những lần mua khác nhau. Agent này tách
> chúng thành từng timeline, rồi chọn lần mua **trước ngày mở case và đã tới hạn giao**, vì một đơn
> chưa tới hạn thì chưa thể bị khiếu nại. Mọi chênh lệch giữa các nguồn được ghi lại thành
> `data_conflicts`.
>
> **Bước năm**, Coordinator phân loại issue bằng mười luật có thứ tự ưu tiên, chỉ dựa trên timeline
> đã chọn. Nếu phát hiện giao trễ mà chưa có dữ liệu vận chuyển, Coordinator giao thêm một nhiệm vụ
> `VERIFY_DELAY`. Đây là nhánh phụ duy nhất trong luồng.
>
> **Bước sáu**, Policy agent tra rule chính thức: trạng thái case, hành động, số tiền hoàn và bên
> chịu trách nhiệm. **Bước bảy**, Verifier kiểm tra lại rồi mới xuất kết quả.
>
> Mỗi mũi tên là một message A2A có intent riêng và đều được ghi vào trace, nên có thể kiểm toán lại
> toàn bộ quá trình.

## Trang 3 · Ví dụ thật: L3B_CASE_010 (2:45 – 4:00)

> Xem một case thật. Khách khai **giao trễ do seller** và đòi hoàn toàn bộ tiền. Danh sách ứng viên
> có một mã giả là `candidate-010`.
>
> Entity agent loại mã này ngay vì nó không đúng định dạng mã đơn, và xác định được đơn thật với độ
> tin cậy 0.95.
>
> Điểm thú vị nằm ở trục thời gian: cùng mã đơn này có **hai lần mua**. Lần một ngày 29/12/2017, hẹn
> giao 8/1, giao thực tế 12/1. Lần hai tận tháng 5/2018 và đã bị huỷ. Case mở ngày 10/1/2018, nên
> lần hai chưa hề tồn tại lúc đó. Hệ thống chọn **lần một**.
>
> Đáng chú ý là nguồn `get_order` lại trả về lần mua thứ hai. Nếu tin nguồn này, ta sẽ nhìn thấy một
> đơn bị huỷ và kết luận sai hoàn toàn. Hệ thống ưu tiên lịch sử khách và ghi lại bốn trường bị xung
> đột.
>
> Từ timeline đúng: giao sau ngày hẹn, và seller bàn giao cho đơn vị vận chuyển trễ hơn hạn, nên
> kết luận là **giao trễ do seller**. Policy quyết định **hoàn phí vận chuyển 18 BRL**. Claim giao
> trễ được đánh giá là *supported*, còn yêu cầu hoàn toàn bộ chỉ *partially supported*. Verifier đạt
> chín trên chín kiểm tra.

## Trang 4 · Độ tin cậy & kết quả (4:00 – 5:00)

> Về độ tin cậy: Verifier kiểm tra độc lập chín bất biến, ví dụ đơn phải thuộc lịch sử khách, mọi
> evidence phải do chính case đó gọi, và tổng tiền hoàn phải khớp. Các lỗi như timeout, mất kết nối
> hay tool không có dữ liệu đều có cách xử lý rõ ràng.
>
> Kết quả: **100 trên 100 case** có output hợp lệ, trung bình **đúng 6 call mỗi case**, điểm public
> **93.29**, so với mức trần chung khoảng 93.9. Nhóm đi từ 90.92 lên 93.29 qua nhiều vòng tinh chỉnh.
> Bước tăng mạnh nhất đến từ hai thay đổi: chỉ trích dẫn evidence thật sự hỗ trợ kết luận, và xử lý
> đúng shipment verdict cho đơn đã huỷ.
>
> Hạn chế hiện tại: kế hoạch tool đang viết cứng trong code, và message A2A mới dùng để ghi trace
> chứ chưa dùng để truyền dữ liệu. Hướng tiếp theo là tách kế hoạch ra thành cấu hình, và nếu cần
> thì chỉ dùng LLM để *giải thích* kết quả, không để *ra quyết định*.
>
> Cảm ơn mọi người đã lắng nghe.

---

## Hướng dẫn chỉ tay trang 1 (theo diagram)

Mắt người nghe đi theo hình chữ Z: trái sang giữa, sang phải, rồi xuống dưới. Chỉ tay đúng thứ tự
này để lời nói luôn khớp với chỗ đang được nhìn.

| # | Thời gian | Chỉ vào | Nói gì |
| ---: | --- | --- | --- |
| ① | 0:00 – 0:10 | Dòng phụ đề, cụm **"không dùng LLM"** | Hệ multi-agent viết bằng Python thuần, deterministic: cùng evidence thì cùng kết quả |
| ② | 0:10 – 0:25 | Ô xanh dương "Case khiếu nại", **dừng ở dòng chữ đỏ ⚠** | Input gồm claims, mã đơn khai + ứng viên, opened_at; lời nhắn có thể chứa instruction giả ⇒ không tin lời khai |
| ③ | 0:25 – 0:30 | Ô "CLI" → mũi tên `solve_case()` | CLI đọc 100 case, đưa từng case vào hệ thống |
| ④ | 0:30 – 0:40 | Ô vàng "Coordinator" | Lập kế hoạch, giao việc, lắp kết quả, nhưng không có quyền gọi tool |
| ⑤ | 0:40 – 0:55 | 5 ô xanh lá (lướt trái → phải), rồi **dòng 🔒** ở đáy khung | Đọc tên agent, không đọc tên tool; mỗi agent chỉ gọi tool của mình, gọi sai ⇒ lỗi ngay (least privilege) |
| ⑥ | 0:55 – 1:05 | 2 ô tím | Conflict resolver xử lý nhiều lần mua / nguồn mâu thuẫn; Verifier kiểm 9 điều kiện trước khi xuất |
| ⑦ | 1:05 – 1:10 | `tool call` → Evidence cache → MCP server | Mọi tool call qua cache rồi mới tới MCP server; rớt mạng chạy lại không gọi trùng |
| ⑧ | 1:10 – 1:15 | Hàng xanh dương dưới cùng, dừng ở `submission.zip` | JSON kết luận, trace để kiểm toán, report giải thích; validate schema rồi đóng gói |
| ⑨ | vài giây | Ô "4 nguyên tắc thiết kế" | Chốt 4 nguyên tắc, chuyển sang trang 2: "một case chạy qua hệ thống như thế nào" |

- Bảng chú thích màu: không đọc, chỉ nói một câu lúc đầu: *"Vàng là điều phối, xanh lá có tool,
  tím không có tool."*
- Không giải thích nhãn `task_assigned / handoff` ở trang này, để dành cho trang 2.
- Trình chiếu bằng drawio: bật **View → Presentation mode** hoặc zoom vào từng vùng theo thứ tự ① → ⑧.

---

## Mẹo khi trình bày

- Trang 3 là trang "ăn điểm" nhất: chỉ tay theo trục thời gian, dừng lại ở vạch đỏ "Case mở".
- Nếu bị hỏi "sao không dùng LLM?": trả lời là để cùng evidence luôn cho cùng kết quả, không bị
  prompt injection, và không tốn chi phí gọi model.
- Nếu bị hỏi "A2A ở đâu?": mỗi handoff là một `A2AMessage` có sender, recipient, intent và
  evidence_refs, được ghi thành event `handoff` trong `traces/trace.jsonl`.
- Nếu thiếu thời gian, rút gọn trang 4 còn một câu về kết quả (100/100 case, 6 call/case, 93.29).
