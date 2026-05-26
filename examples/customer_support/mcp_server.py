"""
MCP server providing customer support backend tools.

Exposes tools for customer lookup, identity verification, MFA,
order management, return policies, and refund processing.
"""

import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("customer-support-backend")

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

CUSTOMERS = {
    "cust_7829": {
        "id": "cust_7829",
        "name": "Sarah Chen",
        "email": "sarah.chen@example.com",
        "phone": "+15550142",
        "mfa_enabled": True,
        "mfa_secret": "JBSWY3DPEHPK3PXP",
    },
}

ORDERS = {
    "ord_91847": {
        "id": "ord_91847",
        "customer_id": "cust_7829",
        "status": "delivered",
        "ordered_at": "2026-03-15T10:30:00Z",
        "delivered_at": "2026-03-20T14:22:00Z",
        "shipping_cost": 9.99,
        "tax": 17.10,
        "total": 216.06,
        "items": [
            {
                "product_id": "prod_001",
                "name": "Wireless Headphones",
                "category": "electronics",
                "quantity": 1,
                "unit_price": 149.99,
                "promo_code": "SUMMER20",
                "promo_discount": 30.00,
            },
            {
                "product_id": "prod_002",
                "name": "Phone Case",
                "category": "accessories",
                "quantity": 2,
                "unit_price": 24.99,
                "promo_code": None,
                "promo_discount": 0.00,
            },
        ],
    },
}

SHIPPING = {
    "track_ord_91847": {
        "tracking_id": "track_ord_91847",
        "status": "delivered",
        "delivered_at": "2026-03-20T14:22:00Z",
        "signed_by": "S. Chen",
    },
}

PROMO_TERMS = {
    "SUMMER20": {
        "code": "SUMMER20",
        "type": "percentage",
        "value": 20,
        "clawback_on_return": True,
        "clawback_exception": "partial return keeps over 50% of order value",
        "minimum_order_value": 100.00,
    },
}

RETURN_POLICIES = {
    "electronics": {
        "category": "electronics",
        "return_window_days": 30,
        "base_restocking_fee_pct": 15,
        "fee_by_condition": {"unopened": 15, "opened": 20, "damaged": 50},
        "exceptions": ["defective", "wrong_item"],
        "condition_requirements": ["original_packaging", "all_accessories"],
    },
    "accessories": {
        "category": "accessories",
        "return_window_days": 60,
        "base_restocking_fee_pct": 0,
        "fee_by_condition": {"unopened": 0, "opened": 5, "damaged": 25},
        "exceptions": [],
        "condition_requirements": ["unused"],
    },
}

# MFA state — persisted to a temp file so state survives across MCP server restarts
# (each `claude --print` session spawns its own MCP server process)
import tempfile  # noqa: E402  -- intentional late import; see comment above

_MFA_STATE_FILE = os.path.join(tempfile.gettempdir(), "mcp_mfa_codes.json")


def _load_mfa_codes() -> dict[str, str]:
    try:
        with open(_MFA_STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_mfa_codes(codes: dict[str, str]) -> None:
    with open(_MFA_STATE_FILE, "w") as f:
        json.dump(codes, f)


# ---------------------------------------------------------------------------
# Customer tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_customer(customer_id: str) -> dict:
    """Look up a customer by ID. Returns customer profile including name, email, phone, and MFA status."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"error": f"Customer {customer_id} not found"}
    # Don't expose the MFA secret directly
    return {
        "id": customer["id"],
        "name": customer["name"],
        "email": customer["email"],
        "phone": customer["phone"],
        "mfa_enabled": customer["mfa_enabled"],
    }


@mcp.tool()
def verify_customer_identity(customer_id: str, email: str, phone: str) -> dict:
    """Verify a customer's identity by checking email and phone against the stored record.
    Phone comparison ignores dashes and spaces. Email comparison is case-insensitive."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"verified": False, "reason": f"Customer {customer_id} not found"}

    # Normalize
    normalized_phone = phone.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    stored_phone = customer["phone"]

    email_match = email.strip().lower() == customer["email"].lower()
    phone_match = normalized_phone == stored_phone

    return {
        "verified": email_match and phone_match,
        "email_match": email_match,
        "phone_match": phone_match,
    }


# ---------------------------------------------------------------------------
# MFA tools
# ---------------------------------------------------------------------------


@mcp.tool()
def send_mfa_code(customer_id: str) -> dict:
    """Send an MFA verification code to the customer's registered device.
    Returns confirmation that the code was sent (the code itself is not returned)."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"error": f"Customer {customer_id} not found"}
    if not customer["mfa_enabled"]:
        return {"error": "MFA is not enabled for this customer"}

    # "Generate" and "send" the code
    code = "847291"
    codes = _load_mfa_codes()
    codes[customer_id] = code
    _save_mfa_codes(codes)

    return {
        "sent": True,
        "delivery_method": "authenticator_app",
        "message": "Verification code sent to customer's registered device.",
    }


@mcp.tool()
def validate_mfa_code(customer_id: str, submitted_code: str) -> dict:
    """Validate an MFA code submitted by the customer.
    Checks the code against the expected value, verifies it hasn't expired,
    and validates the TOTP window."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"valid": False, "reason": f"Customer {customer_id} not found"}

    codes = _load_mfa_codes()
    expected = codes.get(customer_id)
    if not expected:
        return {"valid": False, "reason": "No active MFA code. Call send_mfa_code first."}

    code_match = submitted_code.strip() == expected
    return {
        "valid": code_match,
        "expired": False,
        "totp_window_valid": True,
        "reason": None if code_match else "Code does not match.",
    }


# ---------------------------------------------------------------------------
# Order tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_order(order_id: str, customer_id: str) -> dict:
    """Look up an order by order ID and customer ID. Returns full order details
    including items, prices, promos, and status."""
    order = ORDERS.get(order_id)
    if not order:
        return {"error": f"Order {order_id} not found"}
    if order["customer_id"] != customer_id:
        return {"error": f"Order {order_id} does not belong to customer {customer_id}"}
    return order


@mcp.tool()
def get_shipping_status(order_id: str) -> dict:
    """Get shipping/fulfillment status for an order from the shipping provider."""
    tracking_id = f"track_{order_id}"
    shipment = SHIPPING.get(tracking_id)
    if not shipment:
        return {"error": f"No shipping record for order {order_id}"}
    return shipment


@mcp.tool()
def get_return_policy(category: str) -> dict:
    """Get the return policy for a product category, including return window,
    restocking fees by condition, and condition requirements."""
    policy = RETURN_POLICIES.get(category)
    if not policy:
        return {"error": f"No return policy for category '{category}'"}
    return policy


@mcp.tool()
def check_refund_eligibility(order_id: str, customer_id: str) -> dict:
    """Check refund eligibility for all items in an order.
    Validates order status, delivery confirmation, return windows, and per-item policies.
    Returns eligibility status and applicable restocking fee percentages for each item."""
    order = ORDERS.get(order_id)
    if not order or order["customer_id"] != customer_id:
        return {"error": "Order not found or does not belong to customer"}

    if order["status"] not in ("delivered", "shipped"):
        return {"eligible": False, "reason": f"Order status '{order['status']}' is not refundable"}

    # Check shipping
    tracking_id = f"track_{order_id}"
    shipment = SHIPPING.get(tracking_id)
    if not shipment:
        return {"eligible": False, "reason": "No delivery confirmation found"}

    items_eligibility = []
    for item in order["items"]:
        policy = RETURN_POLICIES.get(item["category"], {})
        items_eligibility.append(
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "category": item["category"],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "eligible": True,
                "return_window_days": policy.get("return_window_days"),
                "within_return_window": True,
                "base_restocking_fee_pct": policy.get("base_restocking_fee_pct"),
            }
        )

    return {
        "order_id": order_id,
        "eligible": True,
        "delivery_confirmed": True,
        "delivered_at": shipment["delivered_at"],
        "signed_by": shipment["signed_by"],
        "items": items_eligibility,
    }


# ---------------------------------------------------------------------------
# Promo tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_promo_terms(promo_code: str) -> dict:
    """Get the terms and conditions for a promo code, including clawback rules."""
    promo = PROMO_TERMS.get(promo_code)
    if not promo:
        return {"error": f"Promo code '{promo_code}' not found"}
    return promo


# ---------------------------------------------------------------------------
# Refund tools
# ---------------------------------------------------------------------------


@mcp.tool()
def calculate_refund(order_id: str, item_condition: str) -> dict:
    """Calculate the refund amount for all eligible items in an order.
    Applies restocking fees based on item condition and promo clawback rules.

    item_condition: one of 'unopened', 'opened', or 'damaged'
    """
    order = ORDERS.get(order_id)
    if not order:
        return {"error": f"Order {order_id} not found"}

    items_total = 0.0
    total_restocking = 0.0
    total_clawback = 0.0
    item_breakdown = []

    for item in order["items"]:
        subtotal = item["unit_price"] * item["quantity"]
        items_total += subtotal

        # Restocking fee
        policy = RETURN_POLICIES.get(item["category"], {})
        fee_schedule = policy.get("fee_by_condition", {})
        fee_pct = fee_schedule.get(item_condition, policy.get("base_restocking_fee_pct", 0))
        restocking_fee = round(subtotal * fee_pct / 100, 2)
        total_restocking += restocking_fee

        # Promo clawback
        clawback = 0.0
        if item.get("promo_code"):
            promo = PROMO_TERMS.get(item["promo_code"], {})
            if promo.get("clawback_on_return"):
                # Check exception: remaining order value > 50% of total
                remaining_value = sum(
                    i["unit_price"] * i["quantity"] for i in order["items"] if i["product_id"] != item["product_id"]
                )
                threshold = order["total"] * 0.5
                if remaining_value < threshold:
                    clawback = item.get("promo_discount", 0.0)
                    total_clawback += clawback

        item_breakdown.append(
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "subtotal": subtotal,
                "restocking_fee": restocking_fee,
                "restocking_fee_pct": fee_pct,
                "promo_clawback": clawback,
            }
        )

    net_refund = round(items_total - total_restocking - total_clawback, 2)

    return {
        "order_id": order_id,
        "items_total": items_total,
        "total_restocking_fees": total_restocking,
        "total_promo_clawback": total_clawback,
        "net_refund_amount": net_refund,
        "item_breakdown": item_breakdown,
    }


@mcp.tool()
def process_refund_payment(order_id: str, amount: float) -> dict:
    """Submit a refund to the payment gateway and update the order status.
    Returns the transaction ID and confirmation."""
    order = ORDERS.get(order_id)
    if not order:
        return {"error": f"Order {order_id} not found"}

    # "Process" the payment
    return {
        "success": True,
        "transaction_id": "txn_ref_88291",
        "amount": amount,
        "original_transaction": f"orig_txn_{order_id}",
        "status": "completed",
        "order_status_updated": "refunded",
        "estimated_days": "3-5 business days",
    }


@mcp.tool()
def send_confirmation_email(customer_id: str, subject: str, body: str) -> dict:
    """Send a confirmation email to the customer."""
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"error": f"Customer {customer_id} not found"}
    return {
        "sent": True,
        "to": customer["email"],
        "subject": subject,
    }


if __name__ == "__main__":
    mcp.run()
