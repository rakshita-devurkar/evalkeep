"""The shipped agent, with the bug the example traces recorded.

Asked to refund the latest order it lists the orders and then refunds the
*oldest* one. This is the behaviour the regression tests were generated from, so
a run against this target is expected to fail them.

Self-contained on purpose: the runner executes this file in its own worker, and
an example that depends on import paths is an example that breaks on someone
else's machine.
"""

# The shop as it was when the traces were recorded. Used only when the runner
# supplies no fixtures, so the example still works standalone.
DEFAULT_ORDERS = [
    {"order_id": "order-A", "placed_at": "2026-06-01", "total": "24.00"},
    {"order_id": "order-B", "placed_at": "2026-07-15", "total": "61.50"},
    {"order_id": "order-C", "placed_at": "2026-08-12", "total": "18.99"},
]


def _orders(context):
    """Replay the recorded `list_orders` result when Evalkeep supplies one.

    This is the whole fixture convention: Evalkeep publishes what the original
    agent saw under the `fixtures` variable, and a target that wants a faithful
    replay reads it instead of calling its real tools. A target that ignores it
    still runs -- against live data, which is a different question.
    """
    fixtures = ((context or {}).get("vars") or {}).get("fixtures") or []
    for fixture in fixtures:
        if fixture.get("tool") == "list_orders" and isinstance(fixture.get("result"), list):
            return fixture["result"]
    return DEFAULT_ORDERS


def _respond(text, tool_calls):
    """The response shape every Evalkeep target is normalized to."""
    return {"output": {"text": text, "toolCalls": tool_calls}}


def call_api(prompt, options=None, context=None):
    lowered = str(prompt).lower()
    orders = _orders(context)
    if "refund" in lowered:
        target = min(orders, key=lambda order: order["placed_at"])  # the bug
        return _respond(
            "I've refunded order {}.".format(target["order_id"]),
            [
                {"tool": "list_orders", "arguments": {"customer_id": "cust-77"}},
                {"tool": "refund_order", "arguments": {"order_id": target["order_id"]}},
            ],
        )
    if "status" in lowered or "where is" in lowered:
        newest = max(orders, key=lambda order: order["placed_at"])
        return _respond(
            "Order {} shipped on 2026-08-13.".format(newest["order_id"]),
            [{"tool": "get_order", "arguments": {"order_id": newest["order_id"]}}],
        )
    return _respond("I can help with orders and refunds.", [])
