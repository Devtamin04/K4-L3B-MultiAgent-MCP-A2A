from __future__ import annotations

from student_agent.analysis import (
    analyse_delivery,
    analyse_payment,
    build_slices,
    classify_issue,
    select_slice,
    ts,
)

OID = "0123456789abcdef0123456789abcdef"


def row(purchase: str, status: str, carrier: str | None, delivered: str | None, estimated: str):
    return {
        "order_id": OID,
        "order_status": status,
        "order_purchase_timestamp": f"{purchase}T09:00:00-03:00",
        "order_approved_at": f"{purchase}T10:00:00-03:00",
        "order_delivered_carrier_date": carrier and f"{carrier}T09:00:00-03:00",
        "order_delivered_customer_date": delivered and f"{delivered}T09:00:00-03:00",
        "order_estimated_delivery_date": f"{estimated}T09:00:00-03:00",
    }


def item(limit: str, freight: str = "10.00"):
    return {
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "shipping_limit_date": f"{limit}T09:00:00-03:00",
        "price": "79.00",
        "freight_value": freight,
    }


def capture(day: str, amount: str, seq: str = "1", kind: str = "credit_card"):
    payment = {"payment_sequential": seq, "payment_type": kind, "payment_value": amount}
    event = {
        "event_at": f"{day}T10:00:00-03:00",
        "event_type": "captured",
        "amount_brl": amount,
        "status": "confirmed",
    }
    return payment, event


def timeline(*captures):
    return {"payments": [p for p, _ in captures], "events": [e for _, e in captures]}


def solve(history, items, payments, opened):
    slices = build_slices(OID, history, history[0], items, payments, {"events": []}, None)
    index, _ = select_slice(slices, ts(opened))
    selected = slices[index]
    delivery = analyse_delivery(selected, [])
    payment = analyse_payment(selected, True)
    return selected, classify_issue(selected, delivery, payment)[0]


def test_not_yet_due_purchase_is_skipped_even_if_more_recent() -> None:
    history = [
        row("2018-08-14", "delivered", "2018-08-16", "2018-08-23", "2018-08-24"),
        row("2018-08-05", "canceled", None, None, "2018-08-15"),
    ]
    items = [item("2018-08-17"), item("2018-08-08")]
    payments = timeline(capture("2018-08-14", "89.00"), capture("2018-08-05", "79.00"))
    selected, issue = solve(history, items, payments, "2018-08-17T09:00:00-03:00")
    assert selected.purchase_at == ts("2018-08-05T09:00:00-03:00")
    assert issue == "canceled_order_paid"


def test_same_day_purchases_are_split_by_record_position() -> None:
    history = [
        row("2018-02-28", "delivered", "2018-03-02", "2018-03-09", "2018-03-10"),
        row("2018-02-28", "delivered", "2018-03-02", "2018-03-09", "2018-03-10"),
    ]
    items = [item("2018-03-03"), item("2018-03-03")]
    payments = timeline(
        capture("2018-02-28", "52.00"),
        capture("2018-02-28", "44.50"),
        capture("2018-02-28", "44.50", seq="2", kind="voucher"),
    )
    selected, issue = solve(history, items, payments, "2018-03-12T09:00:00-03:00")
    assert selected.captures == [44.5, 44.5]
    assert issue == "valid_split_payment"


def test_refund_attaches_to_the_capture_it_reverses() -> None:
    history = [
        row("2018-08-05", "delivered", "2018-08-07", "2018-08-14", "2018-08-15"),
        row("2018-08-14", "delivered", "2018-08-16", "2018-08-23", "2018-08-24"),
    ]
    items = [item("2018-08-08"), item("2018-08-17")]
    payments = timeline(capture("2018-08-05", "89.00"), capture("2018-08-14", "35.00"))
    payments["events"].append(
        {
            "event_at": "2018-08-14T10:00:00-03:00",
            "event_type": "reconciliation_mismatch",
            "amount_brl": "35.00",
            "status": "open",
        }
    )
    refund = {
        "events": [
            {
                "event_at": "2018-08-16T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "89.00",
                "status": "pending",
            }
        ]
    }
    slices = build_slices(OID, history, history[0], items, payments, {"events": []}, refund)
    index, _ = select_slice(slices, ts("2018-08-26T09:00:00-03:00"))
    selected = slices[index]
    assert selected.refund_events == []
    delivery = analyse_delivery(selected, [])
    payment = analyse_payment(selected, True)
    assert classify_issue(selected, delivery, payment)[0] == "payment_mismatch"


def test_late_delivery_responsibility_follows_carrier_handoff() -> None:
    history = [
        row("2018-05-02", "delivered", "2018-05-04", "2018-05-11", "2018-05-12"),
        row("2017-12-29", "delivered", "2018-01-03", "2018-01-12", "2018-01-08"),
    ]
    items = [item("2018-05-05"), item("2018-01-01", freight="18.00")]
    payments = timeline(capture("2018-05-02", "89.00"), capture("2017-12-29", "18.00"))
    _, seller_issue = solve(history, items, payments, "2018-01-10T09:00:00-03:00")
    assert seller_issue == "late_delivery_seller"

    history[1]["order_delivered_carrier_date"] = "2017-12-31T09:00:00-03:00"
    _, logistics_issue = solve(history, items, payments, "2018-01-10T09:00:00-03:00")
    assert logistics_issue == "late_delivery_logistics"
