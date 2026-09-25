"""Deterministic investigation logic shared by the specialist agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
)
PAYMENT_TOPICS = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
REFUND_TOPICS = {"refund_pending", "refund_failed"}
DELIVERY_TOPICS = {"late_delivery_seller", "late_delivery_logistics"}
STATUS_TOPICS = {"canceled_order_paid", "unavailable_order_paid"}
COMPLETED_REFUND_STATUSES = {"completed", "succeeded", "confirmed", "refunded", "settled"}
CENT = 0.005


def ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class TimelineSlice:
    """One order-history row plus the item/payment/shipment/refund records dated inside it."""

    order: dict[str, Any]
    purchase_at: datetime
    items: list[dict[str, Any]] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    payment_events: list[dict[str, Any]] = field(default_factory=list)
    shipment_events: list[dict[str, Any]] = field(default_factory=list)
    refund_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return str(self.order.get("order_status") or "")

    @property
    def captured_total(self) -> float:
        return round(
            sum(
                money(e.get("amount_brl"))
                for e in self.payment_events
                if e.get("event_type") == "captured" and e.get("status") == "confirmed"
            ),
            2,
        )

    @property
    def captures(self) -> list[float]:
        return [
            money(e.get("amount_brl"))
            for e in self.payment_events
            if e.get("event_type") == "captured" and e.get("status") == "confirmed"
        ]

    @property
    def order_total(self) -> float:
        return round(
            sum(money(i.get("price")) + money(i.get("freight_value")) for i in self.items), 2
        )

    @property
    def refunded_total(self) -> float:
        return round(
            sum(
                money(e.get("amount_brl"))
                for e in self.refund_events
                if str(e.get("status")) in COMPLETED_REFUND_STATUSES
            ),
            2,
        )

    @property
    def seller_ids(self) -> list[str]:
        return unique(i.get("seller_id") for i in self.items)

    @property
    def item_ids(self) -> list[str]:
        return unique(i.get("order_item_id") for i in self.items)


def unique(values: Any) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _candidates(moment: datetime | None, slices: list[TimelineSlice]) -> list[int]:
    """Slices whose purchase is the latest one at or before `moment` (several if purchases tie)."""
    if moment is None or not slices:
        return [len(slices) - 1]
    started = [i for i, s in enumerate(slices) if s.purchase_at <= moment]
    if not started:
        return [min(range(len(slices)), key=lambda i: slices[i].purchase_at)]
    latest = max(slices[i].purchase_at for i in started)
    return [i for i in started if slices[i].purchase_at == latest]


def _pick(candidates: list[int], prefer: Any) -> int:
    matching = [i for i in candidates if prefer(i)]
    return (matching or candidates)[-1]


def _payment_groups(payments: list[dict[str, Any]]) -> list[list[int]]:
    """Split payment rows into per-purchase groups; a group starts when the sequence restarts."""
    groups: list[list[int]] = []
    for index, payment in enumerate(payments):
        if not groups or str(payment.get("payment_sequential")) == "1":
            groups.append([])
        groups[-1].append(index)
    return groups


def build_slices(
    order_id: str,
    history_orders: list[dict[str, Any]],
    order_row: dict[str, Any] | None,
    items: list[dict[str, Any]],
    payment_timeline: dict[str, Any] | None,
    shipment: dict[str, Any] | None,
    refund_timeline: dict[str, Any] | None,
) -> list[TimelineSlice]:
    """Split one order's records into per-purchase timelines, in authoritative history order.

    Records are attached by timestamp; when two purchases share a timestamp the record position
    (sources list purchases in the same order) and amount/date matches break the tie.
    """
    rows = [row for row in history_orders if row.get("order_id") == order_id]
    if not rows and order_row:
        rows = [order_row]
    slices = [
        TimelineSlice(order=row, purchase_at=ts(row.get("order_purchase_timestamp")))
        for row in rows
        if ts(row.get("order_purchase_timestamp"))
    ]
    if not slices:
        return []

    positional_items = len(items) == len(slices)
    for index, item in enumerate(items):
        options = _candidates(ts(item.get("shipping_limit_date")), slices)
        slices[_pick(options, lambda i, index=index: positional_items and i == index)].items.append(
            item
        )

    timeline = payment_timeline or {}
    events = list(timeline.get("events") or [])
    payments = list(timeline.get("payments") or [])
    captured = [e for e in events if e.get("event_type") == "captured"]
    groups = _payment_groups(payments)
    payment_slice: dict[int, int] = {}
    if len(groups) == len(slices):
        payment_slice = {p: g for g, members in enumerate(groups) for p in members}
    capture_slice: dict[int, int] = {}
    for index, event in enumerate(captured):
        options = _candidates(ts(event.get("event_at")), slices)
        positional = payment_slice.get(index) if len(captured) == len(payments) else None
        chosen = _pick(options, lambda i, positional=positional: i == positional)
        capture_slice[id(event)] = chosen
        if index < len(payments) and len(captured) == len(payments):
            slices[chosen].payments.append(payments[index])
    if len(captured) != len(payments):
        for index, payment in enumerate(payments):
            slices[payment_slice.get(index, len(slices) - 1)].payments.append(payment)

    for event in events:
        if id(event) in capture_slice:
            slices[capture_slice[id(event)]].payment_events.append(event)
            continue
        amount = money(event.get("amount_brl"))
        options = _candidates(ts(event.get("event_at")), slices)
        target = _pick(
            options,
            lambda i, amount=amount: any(
                capture_slice.get(id(c)) == i and money(c.get("amount_brl")) == amount
                for c in captured
            ),
        )
        slices[target].payment_events.append(event)

    for event in (shipment or {}).get("events") or []:
        moment = ts(event.get("event_at"))
        options = _candidates(moment, slices)
        target = _pick(
            options,
            lambda i, moment=moment: (
                ts(slices[i].order.get("order_delivered_customer_date")) == moment
            ),
        )
        slices[target].shipment_events.append(event)
    for event in (refund_timeline or {}).get("events") or []:
        amount = money(event.get("amount_brl"))
        options = _candidates(ts(event.get("event_at")), slices)
        target = _pick(options, lambda i, amount=amount: amount in slices[i].captures)
        slices[target].refund_events.append(event)
    return slices


def select_slice(slices: list[TimelineSlice], opened_at: datetime) -> tuple[int, str]:
    """Pick the order timeline the complaint can refer to at `opened_at`.

    Eligible: purchased before the case opened AND already due at that time (promised delivery date
    passed, or the order reached canceled/unavailable). An order not yet due cannot be complained
    about. Among eligible timelines the latest purchase wins; ties go to the later history record.
    """
    before = [i for i, s in enumerate(slices) if s.purchase_at <= opened_at]
    due = [
        i
        for i in before
        if slices[i].status in {"canceled", "unavailable"}
        or (
            (estimated := ts(slices[i].order.get("order_estimated_delivery_date"))) is not None
            and estimated <= opened_at
        )
    ]
    if due:
        return max(
            due, key=lambda i: (slices[i].purchase_at, i)
        ), "LATEST_DUE_PURCHASE_BEFORE_OPENED_AT"
    if before:
        return max(
            before, key=lambda i: (slices[i].purchase_at, i)
        ), "LATEST_PURCHASE_BEFORE_OPENED_AT"
    return 0, "NO_PURCHASE_BEFORE_OPENED_AT_EARLIEST_USED"


@dataclass
class DeliveryFinding:
    verdict: str
    late: bool
    late_seller_ids: list[str]
    timeline_complete: bool
    responsibility_conflict: bool
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None


def analyse_delivery(s: TimelineSlice, shipping_limits: list[dict[str, Any]]) -> DeliveryFinding:
    order = s.order
    carrier = ts(order.get("order_delivered_carrier_date"))
    delivered = ts(order.get("order_delivered_customer_date"))
    estimated = ts(order.get("order_estimated_delivery_date"))
    complete = all(
        ts(order.get(key))
        for key in (
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        )
    )
    limits = [(item.get("seller_id"), ts(item.get("shipping_limit_date"))) for item in s.items] or [
        (lim.get("seller_id"), ts(lim.get("shipping_limit_at"))) for lim in shipping_limits
    ]
    late_sellers = unique(
        seller for seller, limit in limits if carrier and limit and carrier > limit
    )
    if s.status in {"canceled", "unavailable"}:
        # Order never reached delivery: no delay is attributable to seller or carrier.
        return DeliveryFinding("on_time", False, [], complete, False, carrier, delivered, estimated)
    if s.status != "delivered" or delivered is None or estimated is None:
        return DeliveryFinding(
            "insufficient_evidence", False, [], complete, False, carrier, delivered, estimated
        )
    if delivered <= estimated:
        return DeliveryFinding("on_time", False, [], complete, False, carrier, delivered, estimated)
    actors = {e.get("actor") for e in s.shipment_events if e.get("event_type") == "delivered_late"}
    by_handoff = "seller" if late_sellers else "logistics_provider"
    conflict = bool(actors) and by_handoff not in actors
    if late_sellers:
        return DeliveryFinding(
            "seller_delay", True, late_sellers, complete, conflict, carrier, delivered, estimated
        )
    return DeliveryFinding(
        "logistics_delay", True, [], complete, conflict, carrier, delivered, estimated
    )


@dataclass
class PaymentFinding:
    verdict: str
    captured_total: float | None
    refunded_total: float | None
    refundable_total: float | None
    order_total: float
    captures: list[float]
    has_mismatch_event: bool
    refund_statuses: list[str]


def analyse_payment(s: TimelineSlice, have_payment: bool) -> PaymentFinding:
    if not have_payment:
        return PaymentFinding(
            "insufficient_evidence", None, None, None, s.order_total, [], False, []
        )
    captures = s.captures
    captured = s.captured_total
    refunded = s.refunded_total
    mismatch = any(e.get("event_type") == "reconciliation_mismatch" for e in s.payment_events)
    refund_statuses = [str(e.get("status")) for e in s.refund_events]
    duplicate = (
        len(captures) >= 2
        and len(set(captures)) < len(captures)
        and captured > s.order_total + CENT
    )
    if "failed" in refund_statuses:
        verdict = "refund_failed"
    elif "pending" in refund_statuses:
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    elif duplicate:
        verdict = "duplicate_capture"
    elif mismatch:
        verdict = "capture_mismatch"
    else:
        verdict = "reconciled"
    return PaymentFinding(
        verdict,
        captured,
        refunded,
        round(max(captured - refunded, 0.0), 2),
        s.order_total,
        captures,
        mismatch,
        refund_statuses,
    )


def classify_issue(
    s: TimelineSlice, delivery: DeliveryFinding, payment: PaymentFinding
) -> tuple[str, str]:
    """Return (primary_issue, rule_code) from the selected timeline only; claims are not trusted."""
    paid = (payment.captured_total or 0.0) > CENT
    if s.status == "canceled" and paid:
        return "canceled_order_paid", "STATUS_CANCELED_WITH_CAPTURE"
    if s.status == "unavailable" and paid:
        return "unavailable_order_paid", "STATUS_UNAVAILABLE_WITH_CAPTURE"
    if "failed" in payment.refund_statuses:
        return "refund_failed", "REFUND_EVENT_FAILED"
    if "pending" in payment.refund_statuses:
        return "refund_pending", "REFUND_EVENT_PENDING"
    if payment.has_mismatch_event:
        return "payment_mismatch", "RECONCILIATION_MISMATCH_EVENT"
    if delivery.verdict == "seller_delay":
        return "late_delivery_seller", "CARRIER_HANDOFF_AFTER_SHIPPING_LIMIT"
    if delivery.verdict == "logistics_delay":
        return "late_delivery_logistics", "DELIVERED_AFTER_ESTIMATE_HANDOFF_ON_TIME"
    if payment.verdict == "duplicate_capture":
        return "duplicate_charge", "REPEATED_CAPTURE_EXCEEDS_ORDER_TOTAL"
    if (
        len(payment.captures) >= 2
        and abs((payment.captured_total or 0) - payment.order_total) <= CENT
    ):
        return "valid_split_payment", "SPLIT_CAPTURES_EQUAL_ORDER_TOTAL"
    return "unsupported_claim", "NO_ISSUE_IN_SELECTED_TIMELINE"
