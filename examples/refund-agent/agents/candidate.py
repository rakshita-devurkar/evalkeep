"""The fixed agent: refunds the newest order, exactly once.

A run against this target passes the same tests the baseline fails, which is
what makes the comparison in guide 8J meaningful.
"""

ORDERS = [
    {"order_id": "order-A", "placed_at": "2026-06-01", "total": "24.00"},
    {"order_id": "order-B", "placed_at": "2026-07-15", "total": "61.50"},
    {"order_id": "order-C", "placed_at": "2026-08-12", "total": "18.99"},
]


def _respond(text, tool_calls):
    """The response shape every Evalkeep target is normalized to."""
    return {"output": {"text": text, "toolCalls": tool_calls}}


def call_api(prompt, options=None, context=None):
    lowered = str(prompt).lower()
    if "refund" in lowered:
        target = max(ORDERS, key=lambda order: order["placed_at"])  # the fix
        return _respond(
            "I've refunded your most recent order {} for ${}.".format(
                target["order_id"], target["total"]
            ),
            [
                {"tool": "list_orders", "arguments": {"customer_id": "cust-77"}},
                {"tool": "refund_order", "arguments": {"order_id": target["order_id"]}},
            ],
        )
    if "status" in lowered or "where is" in lowered:
        return _respond(
            "Order order-C shipped on 2026-08-13.",
            [{"tool": "get_order", "arguments": {"order_id": "order-C"}}],
        )
    return _respond("I can help with orders and refunds.", [])
