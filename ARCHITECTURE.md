# L3B Architecture Record

Hệ thống điều tra khiếu nại được cài đặt hoàn toàn **deterministic** (không dùng LLM): mỗi agent là một
module Python có quyền tool riêng, trao đổi với nhau bằng message A2A và để lại trace quan sát được.
Nhờ vậy nội dung khiếu nại (kể cả instruction chèn vào message) không thể điều khiển quyết định.

Code: [`src/student_agent/workflow.py`](src/student_agent/workflow.py) (agents + coordinator),
[`analysis.py`](src/student_agent/analysis.py) (logic thuần), [`evidence_cache.py`](src/student_agent/evidence_cache.py),
[`report.py`](src/student_agent/report.py).

## 1. System overview

```text
case_received
   │
   ▼
Coordinator ──task_assigned──► Entity agent ── get_customer_history ──► resolve order, reject candidates
   │                                   └──handoff ENTITY_RESOLVED──► Coordinator
   │
   ├─task_assigned─► Order agent     (get_order, get_order_items, get_product_context) ─┐
   ├─task_assigned─► Shipment agent  (get_shipment_summary)   [chỉ khi claim cần]        ├─ chạy song song
   ├─task_assigned─► Payment agent   (get_payment_timeline [+ get_refund_timeline])     │  handoff *_FACTS_READY
   └─task_assigned─► Policy agent    (get_policy)                                        ┘  → Conflict resolver
   │
   ▼
Conflict resolver: dựng các timeline của order, chọn timeline đúng theo opened_at, ghi data_conflicts
   │ handoff TIMELINE_SELECTED
   ▼
Coordinator: phân tích giao nhận + đối soát tiền → primary_issue (không dựa vào lời khai)
   │   (nếu phát hiện giao trễ mà chưa có shipment evidence → giao thêm VERIFY_DELAY cho shipment agent)
   ▼
Policy agent: policy_decided (case_status, action, refund, responsible parties)
   ▼
Verifier: kiểm tra độc lập từ raw evidence → verification_completed → handoff READY_TO_FINALIZE
   ▼
Output JSON + case_finalized      (+ reports/<case_id>.md giải thích từng bước, chỉ lưu local)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | claimed order, candidates, customer hint | Resolve order thuộc lịch sử khách, reject candidate sai/định dạng lỗi | `get_customer_history` | `ENTITY_RESOLVED/AMBIGUOUS/NOT_FOUND` → coordinator |
| Coordinator | case input + các handoff | Lập kế hoạch gọi tool theo claim, phân loại issue, lắp output | không gọi tool | `task_assigned` cho từng agent |
| Order/product | resolved order | Order row, item/seller/giá/freight/shipping limit, product context | `get_order`, `get_order_items`, `get_product_context` | `ORDER_FACTS_READY` → conflict resolver |
| Shipment | resolved order | Timeline giao nhận, shipping limit, shipment events | `get_shipment_summary` | `SHIPMENT_FACTS_READY` → conflict resolver |
| Payment/refund | resolved order | Capture, reconciliation event, refund lifecycle | `get_payment_timeline`, `get_refund_timeline` | `PAYMENT_FACTS_READY` → conflict resolver |
| Policy | policy_version, primary issue | Tra rule → status/action/refund/bên chịu trách nhiệm | `get_policy` | `policy_decided`, `REMEDY_DECIDED` → coordinator |
| Conflict resolver | evidence của các specialist | Tách timeline, chọn timeline theo `opened_at`, ghi xung đột nguồn | không | `TIMELINE_SELECTED` → coordinator |
| Verifier | output nháp + raw evidence | Kiểm tra bất biến, sửa lỗi deterministic | không | `verification_completed`, `READY_TO_FINALIZE` |

Least privilege được enforce trong `CaseContext.call`: actor gọi tool ngoài `ACTOR_TOOLS` → `PermissionError`.

## 3. Entity resolution và A2A protocol

- Candidate = `claimed_order_id` ∪ `candidate_order_ids`. Candidate được **resolve** khi xuất hiện trong
  `get_customer_history(customer_unique_id_hint)`; còn lại bị **reject** (lý do: không đúng định dạng
  order id 32-hex hoặc không thuộc lịch sử khách). Không gọi `get_order` cho candidate giả → tiết kiệm call.
- 1 candidate khớp → `resolved` (conf 0.95); >1 → `ambiguous` (0.5); 0 nhưng claimed đúng định dạng →
  `ambiguous` (0.4); còn lại `not_found`.
- Message envelope `A2AMessage(case_id, sender, recipient, intent, evidence_refs, payload)`; mỗi handoff
  được emit thành trace event `handoff` với `decision_code=intent` và evidence refs của agent gửi.
  Correlation duy nhất theo `case_id`; luồng là DAG cố định (không có vòng lặp), follow-up duy nhất là
  `VERIFY_DELAY` (tối đa 1 lần).

## 4. Evidence và conflict lifecycle

- Mọi MCP response được `Contracts.validate_evidence` kiểm tra schema, lưu `evidence_ref` nguyên văn
  (không sửa, không tự tạo). Mỗi lần dùng → `tool_result_consumed` với actor, tool và ref.
- **Tách timeline**: một order có thể trả về nhiều bản ghi (history/payment/shipment/refund) thuộc các
  lần mua khác nhau. Mỗi bản ghi được gán vào timeline có ngày mua gần nhất ≤ thời điểm bản ghi. Khi hai
  lần mua trùng timestamp: item ghép theo vị trí, payment ghép theo nhóm `payment_sequential` (reset về 1
  = lần mua mới), reconciliation/refund event ghép theo số tiền capture, shipment event theo ngày giao.
- **Chọn timeline** (`LATEST_DUE_PURCHASE_BEFORE_OPENED_AT`): đơn bị khiếu nại phải (1) được mua trước
  `opened_at` và (2) **đã tới hạn** tại `opened_at` — quá ngày hẹn giao hoặc đã `canceled`/`unavailable`.
  Đơn chưa tới hạn chưa thể phát sinh khiếu nại. Trong các timeline đủ điều kiện, chọn lần mua gần nhất;
  trùng thì lấy bản ghi sau trong history. Fallback: lần mua gần nhất trước `opened_at`.
- **Source precedence**: `get_customer_history` + timeline authoritative (payment/refund) > `get_order`
  row / top-level `get_shipment_summary` khi chúng trỏ tới timeline khác. Mỗi trường khác biệt được ghi
  vào `data_conflicts` (`order_purchase_timestamp`, `order_status`, `order_delivered_customer_date`,
  `order_estimated_delivery_date`, `delay_responsibility`) với `selected_source` và `resolution_code`.
- Trách nhiệm giao trễ: carrier handoff > shipping limit ⇒ seller; ngược lại logistics. Nếu actor của
  shipment event mâu thuẫn → ưu tiên timestamp, ghi conflict, giảm confidence.
- Evidence chỉ sống trong `CaseContext` của một case; cache lưu theo file `<case_id>.json` nên không thể
  dùng chéo case.

### Phân loại issue (chỉ từ timeline được chọn)

| Thứ tự | Điều kiện | Issue |
| ---: | --- | --- |
| 1 | status `canceled` và có capture | `canceled_order_paid` |
| 2 | status `unavailable` và có capture | `unavailable_order_paid` |
| 3 | refund event `failed` | `refund_failed` |
| 4 | refund event `pending` | `refund_pending` |
| 5 | payment event `reconciliation_mismatch` | `payment_mismatch` |
| 6 | giao sau ngày hẹn, seller bàn giao trễ | `late_delivery_seller` |
| 7 | giao sau ngày hẹn, seller bàn giao đúng hạn | `late_delivery_logistics` |
| 8 | capture lặp, tổng capture > tổng đơn | `duplicate_charge` |
| 9 | ≥2 capture, tổng = tổng đơn | `valid_split_payment` |
| 10 | không phát hiện vấn đề | `unsupported_claim` |

Refund, action, status và bên chịu trách nhiệm lấy từ `get_policy`; `party_id` seller được thay bằng
seller thật của đơn (policy chỉ có id mẫu).

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi mạng | 1 | raise, dừng run (không bịa dữ liệu) | — |
| Tool trả "Error executing tool" (vd không có refund) | 0 | coi là không có dữ liệu, cache lại | không emit `tool_result_consumed` |
| Entity not found/ambiguous | 0 | dùng claimed nếu hợp lệ, confidence ≤ 0.4 | `ENTITY_AMBIGUOUS` / `ENTITY_NOT_FOUND` |
| Source conflict | 0 | chọn theo precedence ở mục 4 | `TIMELINE_SELECTED`, `data_conflicts` |
| Invalid specialist result | 0 | verifier sửa refund_lines, báo lệch capture | `VERIFIED_WITH_FIXES` |

Query budget: 6–7 call/case. Shipment chỉ gọi cho claim giao nhận/trạng thái/unsupported; refund timeline
chỉ gọi cho claim refund; không gọi `get_sellers`, `get_order_payments` (trùng thông tin). Các agent
chạy song song bằng `asyncio.gather`. Cache per-case gắn với run (`DAY09_RUN_EXPIRES_AT`) → chạy lại
không phát sinh call mới; hết hạn run thì cache tự bị bỏ qua.

## 6. Verification invariants

Verifier tính lại độc lập từ raw evidence trước khi finalize:

1. `ENTITY_IN_CUSTOMER_HISTORY` — order được resolve thuộc lịch sử khách.
2. `REJECTED_DISJOINT` — rejected ∩ resolved = ∅.
3. `EVIDENCE_OWNED_BY_CASE` — mọi evidence ref (output + claim) do chính case gọi.
4. `TIMELINE_BEFORE_OPENED_AT` — timeline được chọn mua trước ngày mở case.
5. `CAPTURED_TOTAL_MATCHES_PAYMENT_ROWS` — tổng payment rows của timeline khớp tổng capture events.
6. `DELIVERY_VERDICT_MATCHES_DATES` — verdict giao nhận khớp ngày giao/ngày hẹn.
7. `REFUND_LINES_SUM` — tổng refund_lines = recommended_refund (tự sửa nếu lệch).
8. `STATUS_REFUND_CONSISTENT` — `no_action` ⇒ refund 0.
9. `SELLER_RESPONSIBILITY_IN_SCOPE` — seller chịu trách nhiệm/late seller thuộc đơn.

Sau đó CLI validate JSON Schema cho từng output và trace event.

## 7. Reproducibility

- Không dùng model/LLM, không random → cùng evidence cho cùng output.
- Python ≥ 3.11, dependency pin trong `pyproject.toml` (`mcp>=2,<3`, …). Starter gateway được sửa để
  tương thích `mcp` 2.x (`CallToolResult.is_error`).
- Concurrency: tuần tự theo case, song song trong case (≤ 4 agent).
- Lệnh:
  ```bash
  day09 validate-inputs
  day09 run              # gọi MCP, ghi outputs/, traces/trace.jsonl, reports/
  day09 run --offline    # replay từ cache của run hiện tại, không gọi MCP
  day09 validate
  day09 package --output dist/submission.zip
  ```
- Input phải lấy từ run đang active (`/api/v2/runs/active/l3b/inputs`); `.env` cần
  `DAY09_RUN_EXPIRES_AT` của run đó để bật cache. Không ghi API key vào tài liệu/trace/report.
