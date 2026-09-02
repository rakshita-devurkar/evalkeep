"""The shipped agent, with the bug the example traces recorded.

Asked to refund the latest order it lists the orders and then refunds the
*oldest* one. This is the behaviour the regression tests were generated from, so
a run against this target is expected to fail them.

Self-contained on purpose: the runner executes this file in its own worker, and
an example that depends on import paths is an example that breaks on someone
else's machine.
"""

ORDERS = [
    {"order_id": "order-A", "placed_at": "2026-06-01", "total": "24.00"},
    {"order_id": "order-B", "placed_at": "2026-07-15", "total": "61.50"},
    {"order_id": "order-C", "placed_at": "2026-08-12", "total": "18.99"},
]


def _respond(text, tool_calls):
    """The response shape every Evalsmith target is normalized to."""
    return {"output": {"text": text, "toolCalls": tool_calls}}


def call_api(prompt, options=None, context=None):
    lowered = str(prompt).lower()
    if "refund" in lowered:
        target = min(ORDERS, key=lambda order: order["placed_at"])  # the bug
        return _respond(
            "I've refunded order {} for ${}.".format(target["order_id"], target["total"]),
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
