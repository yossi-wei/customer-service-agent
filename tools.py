"""Context retrieval tools the agent can call.

These stand in for real integrations (an OMS like Shopify, a help-center KB). They are
deliberately simple so a reviewer can read and trust them in seconds.
"""
import json
import re
from pathlib import Path

DATA = Path(__file__).parent / "data"

ORDER_RE = re.compile(r"\bBR-\d{4,6}\b", re.IGNORECASE)


def _orders():
    return json.loads((DATA / "orders.json").read_text())["orders"]


def find_order(email: str, body: str):
    """Look up an order by ID mentioned in the email, falling back to sender address.

    Returns the order dict, or None if nothing confidently matches.
    """
    orders = _orders()
    m = ORDER_RE.search(body or "")
    if m:
        oid = m.group(0).upper()
        for o in orders:
            if o["order_id"].upper() == oid:
                return o
    # Fall back to sender email, but only if it's unambiguous (exactly one order).
    by_email = [o for o in orders if o["email"].lower() == (email or "").lower()]
    if len(by_email) == 1:
        return by_email[0]
    return None


def load_policies() -> str:
    return (DATA / "policies.md").read_text()
