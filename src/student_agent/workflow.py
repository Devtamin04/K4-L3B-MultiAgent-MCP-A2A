from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .analysis import (
    CENT,
    DELIVERY_TOPICS,
    PAYMENT_TOPICS,
    REFUND_TOPICS,
    STATUS_TOPICS,
    DeliveryFinding,
    PaymentFinding,
    TimelineSlice,
    analyse_delivery,
    analyse_payment,
    build_slices,
    classify_issue,
    select_slice,
    ts,
    unique,
)
from .evidence_cache import ToolNoData
from .report import write_case_report
from .trace import TraceWriter

REPORT_DIR: Path | None = None

ACTOR_TOOLS: dict[str, frozenset[str]] = {
    "coordinator": frozenset(),
    "entity-agent": frozenset({"get_customer_history"}),
    "order-agent": frozenset({"get_order", "get_order_items", "get_product_context"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset({"get_payment_timeline", "get_refund_timeline"}),
    "policy-agent": frozenset({"get_policy"}),
    "conflict-resolver": frozenset(),
    "verifier": frozenset(),
}

CLAIM_TOOLS = {
    "delivery": ("get_customer_history", "get_order", "get_order_items", "get_shipment_summary"),
    "status": ("get_customer_history", "get_order", "get_payment_timeline"),
    "payment": ("get_order_items", "get_payment_timeline"),
    "refund": ("get_payment_timeline", "get_refund_timeline"),
    "unsupported": ("get_customer_history", "get_shipment_summary", "get_payment_timeline"),
    "full_refund": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
}

_BASE_SUPPORT = ("get_customer_history", "get_order", "get_policy")
_ORDER_SUPPORT = (*_BASE_SUPPORT, "get_order_items", "get_product_context")
SUPPORTING_TOOLS = {
    "late_delivery_seller": (*_BASE_SUPPORT, "get_order_items", "get_shipment_summary"),
    "late_delivery_logistics": (*_BASE_SUPPORT, "get_order_items", "get_shipment_summary"),
    "canceled_order_paid": (*_ORDER_SUPPORT, "get_payment_timeline"),
    "unavailable_order_paid": (*_ORDER_SUPPORT, "get_payment_timeline"),
    "valid_split_payment": (*_ORDER_SUPPORT, "get_payment_timeline"),
    "duplicate_charge": (*_ORDER_SUPPORT, "get_payment_timeline"),
    "payment_mismatch": (*_BASE_SUPPORT, "get_product_context", "get_payment_timeline"),
    "refund_pending": (*_BASE_SUPPORT, "get_payment_timeline", "get_refund_timeline"),
    "refund_failed": (*_BASE_SUPPORT, "get_payment_timeline", "get_refund_timeline"),
}


def supporting_refs(ctx: CaseContext, issue: str) -> list[str]:
    """Evidence that actually supports the conclusion; other consulted evidence is not cited."""
    tools = SUPPORTING_TOOLS.get(issue)
    if tools is None:
        return unique(e.ref for e in ctx.evidence.values())
    return ctx.refs(tools)


@dataclass
class Evidence:
    tool: str
    actor: str
    ref: str
    domain: str
    data: Any


@dataclass
class A2AMessage:
    """Envelope for agent-to-agent handoffs, correlated by case_id."""

    case_id: str
    sender: str
    recipient: str
    intent: str
    evidence_refs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


class CaseContext:
    def __init__(self, case: dict[str, Any], gateway: Any, trace: TraceWriter) -> None:
        self.case = case
        self.case_id: str = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.evidence: dict[str, Evidence] = {}
        self.no_data: dict[str, str] = {}
        self.messages: list[A2AMessage] = []
        self.steps: list[tuple[str, str]] = []

    def note(self, actor: str, text: str) -> None:
        self.steps.append((actor, text))

    def assign(self, recipient: str, task: str, why: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=recipient,
            decision_code=task,
        )
        self.note("coordinator", f"Giao việc `{task}` cho **{recipient}** — {why}")

    def handoff(self, message: A2AMessage, summary: str) -> None:
        self.messages.append(message)
        attributes = {
            key: value
            for key, value in message.payload.items()
            if isinstance(value, str | int | float | bool) or value is None
        }
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=message.sender,
            target=message.recipient,
            decision_code=message.intent,
            evidence_refs=message.evidence_refs[:20] or None,
            attributes=dict(list(attributes.items())[:20]) or None,
        )
        self.note(
            message.sender, f"Handoff → **{message.recipient}** (`{message.intent}`): {summary}"
        )

    async def call(self, actor: str, tool: str, why: str, **arguments: str) -> Evidence | None:
        if tool not in ACTOR_TOOLS[actor]:
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        if tool in self.evidence:
            return self.evidence[tool]
        if tool in self.no_data:
            return None
        try:
            raw = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except ToolNoData as exc:
            self.no_data[tool] = str(exc)
            self.note(actor, f"Gọi `{tool}` — {why} → server không có dữ liệu cho scope này.")
            return None
        evidence = Evidence(tool, actor, raw["evidence_ref"], raw["domain"], raw["data"])
        self.evidence[tool] = evidence
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[evidence.ref],
            attributes={"domain": evidence.domain, "warnings": len(raw.get("warnings") or [])},
        )
        self.note(
            actor, f"Gọi `{tool}` — {why} → nhận `{evidence.ref}` (domain `{evidence.domain}`)."
        )
        return evidence

    def data(self, tool: str, default: Any = None) -> Any:
        evidence = self.evidence.get(tool)
        return default if evidence is None else evidence.data

    def refs(self, tools: tuple[str, ...] | list[str]) -> list[str]:
        return unique(self.evidence[t].ref for t in tools if t in self.evidence)


# --------------------------------------------------------------------------- specialists


@dataclass
class EntityResult:
    status: str
    resolved: list[str]
    rejected: list[str]
    confidence: float
    customer_unique_id: str | None
    history_orders: list[dict[str, Any]]
    related_order_ids: list[str]


def _looks_like_order_id(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    )


async def entity_agent(ctx: CaseContext) -> EntityResult:
    actor = "entity-agent"
    request = ctx.case["customer_request"]
    claimed = request.get("claimed_order_id")
    candidates = unique([claimed, *(ctx.case.get("candidate_order_ids") or [])])
    hint = ctx.case.get("customer_unique_id_hint")
    history = None
    if hint:
        history = await ctx.call(
            actor,
            "get_customer_history",
            "lấy lịch sử đơn authoritative của khách để đối chiếu candidate",
            customer_unique_id=hint,
        )
    orders = list((history.data or {}).get("orders") or []) if history else []
    history_ids = unique(o.get("order_id") for o in orders)
    resolved = [c for c in candidates if c in history_ids]
    rejected = [c for c in candidates if c not in resolved]
    for candidate in rejected:
        reason = (
            "không đúng định dạng order_id"
            if not _looks_like_order_id(candidate)
            else "không có trong lịch sử khách"
        )
        ctx.note(actor, f"Loại candidate `{candidate}` — {reason}.")
    if len(resolved) == 1:
        status, confidence = "resolved", 0.95
    elif len(resolved) > 1:
        status, confidence = "ambiguous", 0.5
    elif _looks_like_order_id(claimed):
        resolved, rejected = [claimed], [c for c in rejected if c != claimed]
        status, confidence = "ambiguous", 0.4
    else:
        status, confidence = "not_found", 0.3
    customer = (history.data or {}).get("customer_unique_id") if history else None
    ctx.note(
        actor,
        f"Kết quả entity resolution: **{status}**, order = {resolved}, "
        f"lịch sử khách `{customer}` có {len(orders)} bản ghi đơn.",
    )
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "coordinator",
            f"ENTITY_{status.upper()}",
            ctx.refs(["get_customer_history"]),
            {"resolved_count": len(resolved), "rejected_count": len(rejected)},
        ),
        f"order đã xác định {resolved}, loại {rejected}",
    )
    return EntityResult(status, resolved, rejected, confidence, customer, orders, history_ids)


async def order_agent(ctx: CaseContext, order_id: str, include_product: bool) -> None:
    actor = "order-agent"
    calls = [
        ctx.call(
            actor, "get_order", "lấy order row để kiểm tra trạng thái/timeline", order_id=order_id
        ),
        ctx.call(
            actor,
            "get_order_items",
            "lấy item, seller, giá, freight, shipping limit",
            order_id=order_id,
        ),
    ]
    if include_product:
        calls.append(
            ctx.call(
                actor,
                "get_product_context",
                "claim về đơn/thanh toán: xác nhận sản phẩm của đơn (scope product)",
                order_id=order_id,
            )
        )
    await asyncio.gather(*calls)
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "conflict-resolver",
            "ORDER_FACTS_READY",
            ctx.refs(["get_order", "get_order_items", "get_product_context"]),
            {"item_rows": len(ctx.data("get_order_items", []) or [])},
        ),
        "order row + items" + (" + product context" if include_product else ""),
    )


async def shipment_agent(ctx: CaseContext, order_id: str, why: str) -> None:
    actor = "shipment-agent"
    await ctx.call(actor, "get_shipment_summary", why, order_id=order_id)
    summary = ctx.data("get_shipment_summary", {}) or {}
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "conflict-resolver",
            "SHIPMENT_FACTS_READY",
            ctx.refs(["get_shipment_summary"]),
            {"shipment_events": len(summary.get("events") or [])},
        ),
        f"{len(summary.get('events') or [])} shipment event, "
        f"{len(summary.get('shipping_limits') or [])} shipping limit",
    )


async def payment_agent(ctx: CaseContext, order_id: str, include_refunds: bool) -> None:
    actor = "payment-agent"
    calls = [
        ctx.call(actor, "get_payment_timeline", "đối soát capture authoritative", order_id=order_id)
    ]
    if include_refunds:
        calls.append(
            ctx.call(actor, "get_refund_timeline", "claim liên quan refund", order_id=order_id)
        )
    await asyncio.gather(*calls)
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "conflict-resolver",
            "PAYMENT_FACTS_READY",
            ctx.refs(["get_payment_timeline", "get_refund_timeline"]),
            {"refund_timeline": "get_refund_timeline" in ctx.evidence},
        ),
        "payment timeline" + (" + refund timeline" if include_refunds else ""),
    )


async def policy_agent_fetch(ctx: CaseContext) -> None:
    await ctx.call(
        "policy-agent",
        "get_policy",
        f"lấy rule của {ctx.case.get('policy_version')}",
        policy_version=str(ctx.case.get("policy_version")),
    )


@dataclass
class ConflictResult:
    slices: list[TimelineSlice]
    selected: TimelineSlice | None
    selection_code: str
    conflicts: list[dict[str, Any]]


def conflict_resolver(
    ctx: CaseContext, order_id: str | None, entity: EntityResult
) -> ConflictResult:
    actor = "conflict-resolver"
    opened_at = ts(ctx.case.get("opened_at"))
    slices = build_slices(
        order_id or "",
        entity.history_orders,
        ctx.data("get_order"),
        ctx.data("get_order_items", []) or [],
        ctx.data("get_payment_timeline"),
        ctx.data("get_shipment_summary"),
        ctx.data("get_refund_timeline"),
    )
    if not slices or opened_at is None:
        ctx.note(actor, "Không dựng được timeline — thiếu order history.")
        return ConflictResult(slices, None, "NO_TIMELINE", [])
    index, code = select_slice(slices, opened_at)
    selected = slices[index]
    dates = ", ".join(s.purchase_at.date().isoformat() for s in slices)
    ctx.note(
        actor,
        f"Order có {len(slices)} timeline (ngày mua: {dates}); case mở {opened_at.date()}. "
        f"Chọn timeline mua {selected.purchase_at.date()} (`{code}`): đơn bị khiếu nại phải được "
        "mua trước khi case mở và đã tới hạn (quá ngày hẹn giao hoặc canceled/unavailable) tại "
        "thời điểm mở case; đơn chưa tới hạn thì chưa thể phát sinh khiếu nại.",
    )
    conflicts: list[dict[str, Any]] = []
    order_row = ctx.data("get_order") or {}
    if order_row and order_row != selected.order:
        for field_name in (
            "order_purchase_timestamp",
            "order_status",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ):
            if order_row.get(field_name) != selected.order.get(field_name):
                conflicts.append(
                    {
                        "field": field_name,
                        "sources": ["get_order", "get_customer_history"],
                        "selected_source": "get_customer_history",
                        "resolution_code": code,
                    }
                )
        ctx.note(
            actor,
            f"Xung đột nguồn: `get_order` trả timeline mua "
            f"{str(order_row.get('order_purchase_timestamp'))[:10]} (status "
            f"`{order_row.get('order_status')}`) ≠ timeline được chọn → ưu tiên customer history.",
        )
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "coordinator",
            "TIMELINE_SELECTED",
            ctx.refs(["get_customer_history", "get_order"]),
            {
                "selection_code": code,
                "timelines": len(slices),
                "selected_purchase_at": selected.purchase_at.isoformat(),
                "conflicts": len(conflicts),
            },
        ),
        f"timeline {selected.purchase_at.date()}, {len(conflicts)} conflict",
    )
    return ConflictResult(slices, selected, code, conflicts)


# --------------------------------------------------------------------------- policy + output


@dataclass
class Decision:
    issue: str
    rule_code: str
    case_status: str
    action: str
    refund: float
    parties: list[dict[str, Any]]
    confidence: float


def claim_family(topic: str) -> str:
    if topic in DELIVERY_TOPICS:
        return "delivery"
    if topic in STATUS_TOPICS:
        return "status"
    if topic in PAYMENT_TOPICS:
        return "payment"
    if topic in REFUND_TOPICS:
        return "refund"
    if topic == "requested_full_refund":
        return "full_refund"
    return "unsupported"


def policy_decide(
    ctx: CaseContext,
    issue: str,
    rule_code: str,
    selected: TimelineSlice | None,
    delivery: DeliveryFinding | None,
    confidence: float,
) -> Decision:
    """Remedy from the policy rule; a seller party is bound to the order's own (late) seller."""
    actor = "policy-agent"
    rules = ((ctx.data("get_policy") or {}).get("rules")) or {}
    rule = rules.get(issue)
    if rule is None:
        decision = Decision(
            issue,
            rule_code,
            "needs_investigation",
            "escalate_manual_review",
            0.0,
            [{"party_type": "unknown", "party_id": None}],
            min(confidence, 0.4),
        )
    else:
        parties = []
        for party in rule.get("responsible_parties") or []:
            if party.get("party_type") == "seller" and selected is not None:
                sellers = (
                    delivery.late_seller_ids if delivery and delivery.late_seller_ids else None
                ) or selected.seller_ids
                parties.extend({"party_type": "seller", "party_id": s} for s in sellers)
            else:
                parties.append(
                    {"party_type": party.get("party_type"), "party_id": party.get("party_id")}
                )
        decision = Decision(
            issue,
            rule_code,
            rule.get("case_status", "needs_investigation"),
            rule.get("recommended_action", "escalate_manual_review"),
            round(float(rule.get("refund_brl") or 0.0), 2),
            parties[:5],
            confidence,
        )
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor=actor,
        decision_code=decision.action,
        evidence_refs=ctx.refs(["get_policy"]) or None,
        attributes={
            "primary_issue": issue,
            "case_status": decision.case_status,
            "refund_brl": decision.refund,
            "rule_code": rule_code,
        },
    )
    parties = ", ".join(
        p["party_type"] + (f":{p['party_id']}" if p["party_id"] else "") for p in decision.parties
    )
    ctx.note(
        actor,
        f"Áp policy `{ctx.case.get('policy_version')}` cho issue `{issue}` → status "
        f"`{decision.case_status}`, action `{decision.action}`, "
        f"refund **{decision.refund:.2f} BRL**, bên chịu trách nhiệm: {parties}.",
    )
    return decision


def _claim_assessments(
    ctx: CaseContext, decision: Decision, cited: list[str]
) -> list[dict[str, Any]]:
    results = []
    for claim in ctx.case["customer_request"].get("claims") or []:
        topic = claim.get("topic", "")
        family = claim_family(topic)
        refs = [ref for ref in ctx.refs(CLAIM_TOOLS[family]) if ref in cited]
        if topic == "requested_full_refund":
            if decision.refund <= CENT:
                verdict = "unsupported"
            elif decision.action == "issue_refund":
                verdict = "supported"
            else:
                verdict = "partially_supported"
            confidence = 0.85
        elif topic == "unsupported_claim":
            verdict = "unsupported" if decision.issue == "unsupported_claim" else "supported"
            confidence = decision.confidence
        elif topic == decision.issue:
            verdict, confidence = "supported", decision.confidence
        elif topic in DELIVERY_TOPICS and decision.issue in DELIVERY_TOPICS:
            verdict, confidence = "partially_supported", 0.7
        else:
            verdict, confidence = "unsupported", 0.75
        if not refs:
            verdict, confidence = "insufficient_evidence", 0.3
        results.append(
            {
                "claim_id": claim.get("claim_id", "claim")[:64],
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": refs[:30],
            }
        )
        ctx.note("coordinator", f"Claim `{claim.get('claim_id')}` ({topic}) → **{verdict}**.")
    return results[:5]


def build_output(
    ctx: CaseContext,
    entity: EntityResult,
    conflict: ConflictResult,
    delivery: DeliveryFinding | None,
    payment: PaymentFinding | None,
    decision: Decision,
) -> dict[str, Any]:
    selected = conflict.selected
    order_ids = entity.resolved
    order_id = order_ids[0] if order_ids else None
    refund_lines = (
        [{"reason_code": decision.issue, "amount_brl": decision.refund, "entity_id": order_id}]
        if decision.refund > CENT
        else []
    )
    cited = supporting_refs(ctx, decision.issue)[:30]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision.issue,
            "secondary_issues": [],
            "case_status": decision.case_status,
            "confidence": round(decision.confidence, 2),
        },
        "affected_entities": {
            "order_ids": order_ids[:20],
            "item_ids": (selected.item_ids if selected else [])[:20],
            "seller_ids": (selected.seller_ids if selected else [])[:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": _claim_assessments(ctx, decision, cited),
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": entity.resolved[:20],
            "rejected_candidates": entity.rejected[:20],
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.related_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": delivery.verdict if delivery else "insufficient_evidence",
            "late_seller_ids": (delivery.late_seller_ids if delivery else [])[:20],
            "timeline_complete": bool(delivery and delivery.timeline_complete),
        },
        "payment_analysis": {
            "verdict": payment.verdict if payment else "insufficient_evidence",
            "captured_total_brl": payment.captured_total if payment else None,
            "refunded_total_brl": payment.refunded_total if payment else None,
            "refundable_total_brl": decision.refund if payment else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": decision.issue.upper(), "rank": 1}],
            "responsible_parties": decision.parties,
        },
        "evidence_refs": cited,
        "data_conflicts": conflict.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": decision.refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision.action],
    }


# --------------------------------------------------------------------------- verifier


def verifier(
    ctx: CaseContext, output: dict[str, Any], conflict: ConflictResult
) -> list[tuple[str, bool, str]]:
    """Independent re-check from raw evidence; fixes deterministic inconsistencies in place."""
    actor = "verifier"
    checks: list[tuple[str, bool, str]] = []
    resolution = output["entity_resolution"]
    history_ids = {
        o.get("order_id") for o in ((ctx.data("get_customer_history") or {}).get("orders") or [])
    }
    checks.append(
        (
            "ENTITY_IN_CUSTOMER_HISTORY",
            all(o in history_ids for o in resolution["resolved_order_ids"]),
            "order được resolve phải thuộc lịch sử khách",
        )
    )
    checks.append(
        (
            "REJECTED_DISJOINT",
            not set(resolution["resolved_order_ids"]) & set(resolution["rejected_candidates"]),
            "candidate bị loại không trùng order được chọn",
        )
    )
    owned = {e.ref for e in ctx.evidence.values()}
    all_refs = set(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        all_refs |= set(claim["evidence_refs"])
    checks.append(
        ("EVIDENCE_OWNED_BY_CASE", all_refs <= owned, "mọi evidence_ref đều do case này gọi")
    )

    selected = conflict.selected
    opened_at = ts(ctx.case.get("opened_at"))
    if selected is not None and opened_at is not None:
        checks.append(
            (
                "TIMELINE_BEFORE_OPENED_AT",
                selected.purchase_at <= opened_at or conflict.selection_code.startswith("NO_"),
                "timeline được chọn mua trước ngày mở case",
            )
        )
        stated = output["payment_analysis"]["captured_total_brl"]
        if stated is not None:
            from_rows = round(sum(float(p.get("payment_value") or 0) for p in selected.payments), 2)
            ok = abs(from_rows - stated) <= CENT
            checks.append(
                (
                    "CAPTURED_TOTAL_MATCHES_PAYMENT_ROWS",
                    ok,
                    f"tổng payment rows {from_rows:.2f} vs capture events {stated:.2f}",
                )
            )
        delivered = ts(selected.order.get("order_delivered_customer_date"))
        estimated = ts(selected.order.get("order_estimated_delivery_date"))
        late = bool(delivered and estimated and delivered > estimated)
        verdict = output["shipment_analysis"]["verdict"]
        checks.append(
            (
                "DELIVERY_VERDICT_MATCHES_DATES",
                late == (verdict in {"seller_delay", "logistics_delay"}),
                "verdict giao hàng khớp ngày giao vs ngày hẹn",
            )
        )

    status = output["assessment"]["case_status"]
    refund = output["financial_resolution"]["recommended_refund_brl"]
    lines = output["financial_resolution"]["refund_lines"]
    line_total = round(sum(line["amount_brl"] for line in lines), 2)
    if abs(line_total - refund) > CENT:
        output["financial_resolution"]["refund_lines"] = (
            [
                {
                    "reason_code": output["assessment"]["primary_issue"],
                    "amount_brl": refund,
                    "entity_id": (output["entity_resolution"]["resolved_order_ids"] or [None])[0],
                }
            ]
            if refund > CENT
            else []
        )
        checks.append(("REFUND_LINES_SUM", False, "đã sửa refund_lines cho khớp tổng"))
    else:
        checks.append(("REFUND_LINES_SUM", True, "tổng refund_lines = recommended_refund"))
    checks.append(
        (
            "STATUS_REFUND_CONSISTENT",
            not (status == "no_action" and refund > CENT),
            "no_action thì không refund",
        )
    )
    sellers = set(output["affected_entities"]["seller_ids"])
    responsible = {
        p["party_id"]
        for p in output["root_cause_analysis"]["responsible_parties"]
        if p["party_type"] == "seller"
    }
    checks.append(
        (
            "SELLER_RESPONSIBILITY_IN_SCOPE",
            responsible <= sellers
            and set(output["shipment_analysis"]["late_seller_ids"]) <= sellers,
            "seller chịu trách nhiệm thuộc đơn hàng",
        )
    )
    passed = all(ok for _, ok, _ in checks)
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=actor,
        decision_code="VERIFIED" if passed else "VERIFIED_WITH_FIXES",
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"checks": len(checks), "failed": sum(1 for _, ok, _ in checks if not ok)},
    )
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            actor,
            "coordinator",
            "READY_TO_FINALIZE",
            payload={"checks_passed": passed},
        ),
        f"{sum(ok for _, ok, _ in checks)}/{len(checks)} kiểm tra đạt",
    )
    return checks


# --------------------------------------------------------------------------- coordinator


async def solve_case(case: dict[str, Any], gateway: Any, trace: TraceWriter) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    topics = [c.get("topic", "") for c in case["customer_request"].get("claims") or []]
    main_topic = next((t for t in topics if t != "requested_full_refund"), "unsupported_claim")
    ctx.note(
        "coordinator",
        f"Nhận case: claim chính `{main_topic}`, claimed order "
        f"`{case['customer_request'].get('claimed_order_id')}`, candidates "
        f"{case.get('candidate_order_ids')}. Không tin lời khai — mọi kết luận phải từ evidence.",
    )

    ctx.assign("entity-agent", "RESOLVE_ENTITY", "xác định đúng order/khách trước khi điều tra")
    entity = await entity_agent(ctx)
    order_id = entity.resolved[0] if entity.resolved else None

    need_shipment = not any(t in PAYMENT_TOPICS | REFUND_TOPICS | STATUS_TOPICS for t in topics)
    need_refunds = any(t in REFUND_TOPICS for t in topics)
    need_product = any(t in PAYMENT_TOPICS | STATUS_TOPICS for t in topics)
    jobs = []
    if order_id:
        ctx.assign(
            "order-agent",
            "COLLECT_ORDER_FACTS",
            "order row, item/seller" + (", product context" if need_product else ""),
        )
        jobs.append(order_agent(ctx, order_id, need_product))
        if need_shipment:
            ctx.assign("shipment-agent", "COLLECT_SHIPMENT_FACTS", "claim cần kiểm chứng giao nhận")
            jobs.append(
                shipment_agent(ctx, order_id, "kiểm tra timeline giao nhận & shipping limit")
            )
        ctx.assign(
            "payment-agent",
            "COLLECT_PAYMENT_FACTS",
            "đối soát tiền" + (" + refund timeline" if need_refunds else ""),
        )
        jobs.append(payment_agent(ctx, order_id, need_refunds))
    ctx.assign("policy-agent", "LOAD_POLICY", "cần rule chính thức để quyết định")
    jobs.append(policy_agent_fetch(ctx))
    await asyncio.gather(*jobs)

    ctx.assign("conflict-resolver", "RESOLVE_SOURCES", "các nguồn có nhiều timeline, cần chọn đúng")
    conflict = conflict_resolver(ctx, order_id, entity)
    selected = conflict.selected

    delivery = payment = None
    if selected is not None:
        limits = (ctx.data("get_shipment_summary") or {}).get("shipping_limits") or []
        delivery = analyse_delivery(selected, limits)
        payment = analyse_payment(selected, "get_payment_timeline" in ctx.evidence)
        issue, rule_code = classify_issue(selected, delivery, payment)
        if issue in DELIVERY_TOPICS and "get_shipment_summary" not in ctx.evidence:
            ctx.assign(
                "shipment-agent", "VERIFY_DELAY", "dữ liệu cho thấy giao trễ, cần evidence shipment"
            )
            await shipment_agent(ctx, order_id or "", "xác minh giao trễ phát hiện từ history")
            delivery = analyse_delivery(
                selected, (ctx.data("get_shipment_summary") or {}).get("shipping_limits") or []
            )
        delivered = delivery.delivered_at.date() if delivery.delivered_at else "—"
        estimated = delivery.estimated_at.date() if delivery.estimated_at else "—"
        ctx.note(
            "coordinator",
            f"Timeline được chọn: status `{selected.status}`, giao {delivered} / hẹn {estimated} "
            f"(shipment verdict `{delivery.verdict}`); capture {payment.captured_total} BRL "
            f"vs tổng đơn {payment.order_total} BRL (payment verdict `{payment.verdict}`).",
        )
        ctx.note("coordinator", f"Suy ra issue **`{issue}`** theo rule `{rule_code}`.")
    else:
        issue, rule_code = "insufficient_evidence", "NO_TIMELINE"

    confidence = 0.95 if issue == main_topic else 0.65
    if conflict.selection_code.startswith("NO_"):
        confidence -= 0.1
    if delivery is not None and delivery.responsibility_conflict:
        confidence -= 0.05
        conflict.conflicts.append(
            {
                "field": "delay_responsibility",
                "sources": ["get_shipment_summary.events", "shipping_limit_vs_carrier_handoff"],
                "selected_source": "shipping_limit_vs_carrier_handoff",
                "resolution_code": "HANDOFF_TIMESTAMPS_OVER_EVENT_ACTOR",
            }
        )
    if entity.status != "resolved":
        confidence = min(confidence, entity.confidence)
    if issue != main_topic:
        ctx.note("coordinator", f"Lời khai `{main_topic}` KHÔNG khớp evidence → dùng `{issue}`.")

    ctx.assign("policy-agent", "DECIDE_REMEDY", f"áp policy cho issue `{issue}`")
    decision = policy_decide(ctx, issue, rule_code, selected, delivery, confidence)
    ctx.handoff(
        A2AMessage(
            ctx.case_id,
            "policy-agent",
            "coordinator",
            "REMEDY_DECIDED",
            ctx.refs(["get_policy"]),
            {"action": decision.action, "refund_brl": decision.refund},
        ),
        f"{decision.action}, {decision.refund:.2f} BRL",
    )
    output = build_output(ctx, entity, conflict, delivery, payment, decision)
    ctx.assign("verifier", "VERIFY_OUTPUT", "kiểm tra độc lập trước khi finalize")
    checks = verifier(ctx, output, conflict)

    if REPORT_DIR is not None:
        write_case_report(REPORT_DIR, ctx, output, checks)
    return output
