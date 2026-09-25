"""Human-readable investigation reports (local only, never packaged into the submission)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workflow import CaseContext


def write_case_report(
    directory: Path, ctx: CaseContext, output: dict[str, Any], checks: list[tuple[str, bool, str]]
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    request = ctx.case["customer_request"]
    assessment = output["assessment"]
    entity = output["entity_resolution"]
    shipment = output["shipment_analysis"]
    payment = output["payment_analysis"]
    refund = output["financial_resolution"]["recommended_refund_brl"]
    claims = ", ".join(f"{c['claim_id']}=`{c['topic']}`" for c in request.get("claims", []))
    lines = [
        f"# {ctx.case_id}",
        "",
        f"- Mở case: `{ctx.case.get('opened_at')}` — policy `{ctx.case.get('policy_version')}`",
        f"- Lời nhắn: _{request.get('message')}_",
        f"- Claims: {claims}",
        f"- Claimed order: `{request.get('claimed_order_id')}` — candidates: "
        f"{ctx.case.get('candidate_order_ids')}",
        "",
        "## Diễn biến điều tra",
        "",
        *(f"{i}. **{actor}** — {text}" for i, (actor, text) in enumerate(ctx.steps, 1)),
        "",
        "## Kết luận",
        "",
        "| Mục | Giá trị |",
        "| --- | --- |",
        f"| Primary issue | `{assessment['primary_issue']}` "
        f"(confidence {assessment['confidence']}) |",
        f"| Case status | `{assessment['case_status']}` |",
        f"| Entity | {entity['status']} → {entity['resolved_order_ids']} |",
        f"| Shipment | `{shipment['verdict']}` late sellers {shipment['late_seller_ids']} |",
        f"| Payment | `{payment['verdict']}` captured {payment['captured_total_brl']} BRL |",
        f"| Refund đề xuất | **{refund:.2f} BRL** |",
        f"| Action | {', '.join(output['resolution_actions'])} |",
        f"| Data conflicts | {len(output['data_conflicts'])} |",
        f"| MCP tool dùng | {len(ctx.evidence)} evidence + {len(ctx.no_data)} không có dữ liệu |",
        "",
        "## Verification",
        "",
        *(f"- {'✅' if ok else '⚠️'} `{code}` — {text}" for code, ok, text in checks),
        "",
        "## Output JSON",
        "",
        "```json",
        json.dumps(output, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    (directory / f"{ctx.case_id}.md").write_text("\n".join(lines), encoding="utf-8")


def write_summary(
    directory: Path,
    cases: dict[str, dict[str, Any]],
    outputs: dict[str, dict[str, Any]],
    tool_calls: dict[str, int],
    network_calls: int,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    agree = 0
    issues: Counter[str] = Counter()
    for case_id, output in outputs.items():
        claims = cases[case_id]["customer_request"].get("claims", [])
        topic = next((c["topic"] for c in claims if c["topic"] != "requested_full_refund"), "-")
        issue = output["assessment"]["primary_issue"]
        issues[issue] += 1
        agree += topic == issue
        cells = [
            f"[{case_id}]({case_id}.md)",
            f"`{topic}`",
            f"`{issue}`" + ("" if topic == issue else " ⚠️"),
            output["assessment"]["case_status"],
            f"{output['financial_resolution']['recommended_refund_brl']:.2f}",
            output["shipment_analysis"]["verdict"],
            output["payment_analysis"]["verdict"],
            str(len(output["data_conflicts"])),
            str(tool_calls.get(case_id, 0)),
            str(output["assessment"]["confidence"]),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    total_calls = sum(tool_calls.values())
    average = total_calls / max(len(outputs), 1)
    distribution = ", ".join(f"`{k}` {v}" for k, v in sorted(issues.items()))
    header = ["Case", "Claim", "Primary issue", "Status", "Refund BRL", "Shipment", "Payment"]
    header += ["Conflicts", "Tools", "Conf"]
    lines = [
        "# Tổng hợp run L3B",
        "",
        f"- Số case: **{len(outputs)}**",
        f"- Issue suy ra từ evidence khớp claim: **{agree}/{len(outputs)}** "
        "(case lệch là claim bị evidence bác bỏ)",
        f"- MCP evidence dùng: **{total_calls}** (trung bình {average:.2f}/case); "
        f"network call thực tế lần chạy này: {network_calls}",
        f"- Phân bố primary issue: {distribution}",
        "",
        "| " + " | ".join(header) + " |",
        "| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: |",
        *rows,
        "",
    ]
    path = directory / "SUMMARY.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
