#!/usr/bin/env python3
"""
customer_support_workflow.py — Deeply nested mock workflow for comparing
agent orchestration strategies.

This simulates a real-world customer support scenario with deeply nested flows:

Customer contacts support → agent must:
  1. Authenticate the customer (deep: verify identity → check MFA → validate session)
  2. Look up the order (deep: search orders → validate order state → check eligibility)
  3. Process a refund (deep: calculate refund → apply policy rules → execute refund → send confirmation)

The nesting goes 5 levels deep in places, which is where single-agent context
and sub-agent approaches start to break down.

WORKFLOW TREE:
─────────────────────────────────────────────────────────────────
handle_support_request                          [Level 0]
├── authenticate_customer                       [Level 1]
│   ├── verify_identity                         [Level 2]
│   │   ├── check_email_match                   [Level 3]
│   │   └── check_phone_match                   [Level 3]
│   ├── verify_mfa                              [Level 2]
│   │   ├── send_mfa_code                       [Level 3]
│   │   └── validate_mfa_code                   [Level 3]
│   │       └── check_code_expiry               [Level 4]
│   │           └── validate_totp_window        [Level 5]
│   └── create_auth_session                     [Level 2]
│       └── generate_session_token              [Level 3]
├── lookup_order                                [Level 1]
│   ├── search_orders                           [Level 2]
│   │   └── query_order_database                [Level 3]
│   ├── validate_order_state                    [Level 2]
│   │   ├── check_order_status                  [Level 3]
│   │   └── check_fulfillment_status            [Level 3]
│   │       └── query_shipping_provider         [Level 4]
│   └── check_refund_eligibility                [Level 2]
│       ├── check_return_window                 [Level 3]
│       └── check_item_condition_policy         [Level 3]
│           └── lookup_product_category_rules   [Level 4]
│               └── get_category_exceptions     [Level 5]
└── process_refund                              [Level 1]
    ├── calculate_refund_amount                 [Level 2]
    │   ├── get_original_charges                [Level 3]
    │   ├── apply_restocking_fee                [Level 3]
    │   │   └── get_fee_schedule                [Level 4]
    │   └── apply_promo_clawback                [Level 3]
    │       └── check_promo_terms               [Level 4]
    │           └── evaluate_clawback_rules     [Level 5]
    ├── execute_refund                          [Level 2]
    │   ├── create_refund_transaction           [Level 3]
    │   │   └── call_payment_gateway            [Level 4]
    │   └── update_order_status                 [Level 3]
    └── send_confirmation                       [Level 2]
        ├── generate_email                      [Level 3]
        └── send_email                          [Level 3]
─────────────────────────────────────────────────────────────────

This file serves as both:
1. A runnable mock that simulates the workflow with fake data
2. Documentation of the exact flow for the comparison analysis
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


# ============================================================================
# Domain Models
# ============================================================================

class OrderStatus(Enum):
    PENDING = "pending"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"

class FulfillmentStatus(Enum):
    PROCESSING = "processing"
    SHIPPED = "shipped"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    RETURNED = "returned"

@dataclass
class Customer:
    id: str
    name: str
    email: str
    phone: str
    mfa_enabled: bool = True
    mfa_secret: str = "JBSWY3DPEHPK3PXP"

@dataclass
class OrderItem:
    product_id: str
    product_name: str
    category: str
    quantity: int
    unit_price: float
    promo_code: Optional[str] = None
    promo_discount: float = 0.0

@dataclass
class Order:
    id: str
    customer_id: str
    status: OrderStatus
    fulfillment_status: FulfillmentStatus
    items: list
    shipping_cost: float
    tax: float
    total: float
    ordered_at: str
    delivered_at: Optional[str] = None

@dataclass
class AuthSession:
    token: str
    customer_id: str
    created_at: str
    expires_at: str
    mfa_verified: bool = False

@dataclass
class RefundResult:
    success: bool
    refund_id: Optional[str]
    amount: float
    breakdown: dict
    transaction_id: Optional[str]
    error: Optional[str] = None

@dataclass
class WorkflowTrace:
    """Tracks every step for comparison analysis."""
    steps: list = field(default_factory=list)
    total_context_tokens_estimate: int = 0  # simulated

    def record(self, level: int, function_name: str, input_summary: str,
               output_summary: str, context_needed: str):
        self.steps.append({
            "level": level,
            "function": function_name,
            "input": input_summary,
            "output": output_summary,
            "context_needed": context_needed,
            "indent": "  " * level,
        })

    def print_trace(self):
        print("\n=== WORKFLOW EXECUTION TRACE ===\n")
        for step in self.steps:
            indent = step["indent"]
            print(f"{indent}→ {step['function']}")
            print(f"{indent}  context needed: {step['context_needed']}")
            print(f"{indent}  output: {step['output'][:100]}")
            print()


# ============================================================================
# Mock Data
# ============================================================================

MOCK_CUSTOMER = Customer(
    id="cust_7829",
    name="Sarah Chen",
    email="sarah.chen@example.com",
    phone="+1-555-0142",
    mfa_enabled=True,
)

MOCK_ORDER = Order(
    id="ord_91847",
    customer_id="cust_7829",
    status=OrderStatus.DELIVERED,
    fulfillment_status=FulfillmentStatus.DELIVERED,
    items=[
        OrderItem("prod_001", "Wireless Headphones", "electronics",
                  1, 149.99, "SUMMER20", 30.00),
        OrderItem("prod_002", "Phone Case", "accessories",
                  2, 24.99),
    ],
    shipping_cost=9.99,
    tax=17.10,
    total=216.06,
    ordered_at="2026-03-15T10:30:00Z",
    delivered_at="2026-03-20T14:22:00Z",
)

MOCK_PROMO_TERMS = {
    "SUMMER20": {
        "type": "percentage",
        "discount": 20,
        "clawback_on_return": True,
        "clawback_exceptions": ["partial_return_over_50pct"],
        "min_order_value": 100.00,
    }
}

MOCK_CATEGORY_RULES = {
    "electronics": {
        "return_window_days": 30,
        "restocking_fee_pct": 15,
        "exceptions": ["defective", "wrong_item"],
        "condition_requirements": ["original_packaging", "all_accessories"],
    },
    "accessories": {
        "return_window_days": 60,
        "restocking_fee_pct": 0,
        "exceptions": [],
        "condition_requirements": ["unused"],
    }
}

MOCK_FEE_SCHEDULE = {
    "electronics": {"base_pct": 15, "opened_pct": 20, "damaged_pct": 50},
    "accessories": {"base_pct": 0, "opened_pct": 5, "damaged_pct": 25},
}


# ============================================================================
# Level 5 Functions (deepest)
# ============================================================================

def validate_totp_window(secret: str, code: str, timestamp: float, trace: WorkflowTrace) -> bool:
    """Level 5: Validate TOTP code is within acceptable time window."""
    trace.record(5, "validate_totp_window",
                 f"secret=*****, code={code}",
                 "TOTP valid within 30s window",
                 "MFA secret, current timestamp, code — but also needs to know this is part of "
                 "customer auth for Sarah Chen on order ord_91847 refund request")
    # Mock: always valid
    return True


def get_category_exceptions(category: str, trace: WorkflowTrace) -> list:
    """Level 5: Get special exceptions for a product category's return policy."""
    rules = MOCK_CATEGORY_RULES.get(category, {})
    exceptions = rules.get("exceptions", [])
    trace.record(5, "get_category_exceptions",
                 f"category={category}",
                 f"exceptions={exceptions}",
                 "Product category — but also needs order context to know WHY we're checking "
                 "(refund for Sarah Chen's headphones, ordered with SUMMER20 promo)")
    return exceptions


def evaluate_clawback_rules(promo_code: str, return_items: list, order_total: float,
                            trace: WorkflowTrace) -> dict:
    """Level 5: Evaluate whether a promo discount should be clawed back on return."""
    terms = MOCK_PROMO_TERMS.get(promo_code, {})
    remaining_value = sum(i.unit_price * i.quantity for i in return_items
                         if i.promo_code != promo_code)

    clawback = terms.get("clawback_on_return", False)
    if remaining_value > order_total * 0.5:
        clawback = False  # Exception: partial return over 50% value

    result = {
        "clawback_applies": clawback,
        "clawback_amount": 30.00 if clawback else 0.00,
        "reason": "Full return with promo — clawback applies" if clawback
                  else "Partial return exception — no clawback",
    }

    trace.record(5, "evaluate_clawback_rules",
                 f"promo={promo_code}, items={len(return_items)}, total={order_total}",
                 f"clawback={'yes' if clawback else 'no'}, amount={result['clawback_amount']}",
                 "Promo terms, return items, order total — but critically needs the FULL chain: "
                 "customer identity verified, order validated, eligibility confirmed, "
                 "restocking fee already calculated")
    return result


# ============================================================================
# Level 4 Functions
# ============================================================================

def check_code_expiry(code: str, issued_at: float, trace: WorkflowTrace) -> bool:
    """Level 4: Check if MFA code has expired."""
    valid = (time.time() - issued_at) < 300  # 5 min validity
    trace.record(4, "check_code_expiry",
                 f"code={code}, age={time.time() - issued_at:.0f}s",
                 f"expired={not valid}",
                 "Code and issue timestamp — needs auth flow context")
    if valid:
        return validate_totp_window("secret", code, time.time(), trace)
    return False


def query_shipping_provider(tracking_id: str, trace: WorkflowTrace) -> dict:
    """Level 4: Query external shipping provider for delivery status."""
    result = {"status": "delivered", "delivered_at": "2026-03-20T14:22:00Z",
              "signed_by": "S. Chen"}
    trace.record(4, "query_shipping_provider",
                 f"tracking={tracking_id}",
                 f"status={result['status']}",
                 "Tracking ID — needs order context to validate response makes sense")
    return result


def lookup_product_category_rules(category: str, trace: WorkflowTrace) -> dict:
    """Level 4: Look up return/refund rules for a product category."""
    rules = MOCK_CATEGORY_RULES.get(category, {})
    exceptions = get_category_exceptions(category, trace)
    rules["exceptions"] = exceptions
    trace.record(4, "lookup_product_category_rules",
                 f"category={category}",
                 f"window={rules.get('return_window_days')}d, fee={rules.get('restocking_fee_pct')}%",
                 "Category — needs order+item context for why")
    return rules


def get_fee_schedule(category: str, condition: str, trace: WorkflowTrace) -> float:
    """Level 4: Get the restocking fee percentage for item condition."""
    schedule = MOCK_FEE_SCHEDULE.get(category, {})
    fee_pct = schedule.get(f"{condition}_pct", schedule.get("base_pct", 0))
    trace.record(4, "get_fee_schedule",
                 f"category={category}, condition={condition}",
                 f"fee={fee_pct}%",
                 "Category and condition — needs refund calculation context")
    return fee_pct


def check_promo_terms(promo_code: str, return_items: list, order: Order,
                      trace: WorkflowTrace) -> dict:
    """Level 4: Check promotional terms for clawback implications."""
    clawback = evaluate_clawback_rules(promo_code, return_items, order.total, trace)
    trace.record(4, "check_promo_terms",
                 f"promo={promo_code}",
                 f"clawback={clawback['clawback_applies']}",
                 "Promo code, items, order — needs full refund context")
    return clawback


def call_payment_gateway(amount: float, original_txn: str, trace: WorkflowTrace) -> dict:
    """Level 4: Call payment gateway to execute the refund."""
    result = {"transaction_id": "txn_ref_88291", "status": "completed",
              "amount": amount, "currency": "USD"}
    trace.record(4, "call_payment_gateway",
                 f"amount=${amount:.2f}, original_txn={original_txn}",
                 f"txn_id={result['transaction_id']}",
                 "Refund amount and original transaction — needs full chain to audit")
    return result


# ============================================================================
# Level 3 Functions
# ============================================================================

def check_email_match(provided_email: str, customer: Customer, trace: WorkflowTrace) -> bool:
    """Level 3: Verify provided email matches customer record."""
    match = provided_email.lower() == customer.email.lower()
    trace.record(3, "check_email_match",
                 f"provided={provided_email}",
                 f"match={match}",
                 "Customer record — needs to know this is part of support request auth")
    return match


def check_phone_match(provided_phone: str, customer: Customer, trace: WorkflowTrace) -> bool:
    """Level 3: Verify provided phone matches customer record."""
    match = provided_phone.replace("-", "").replace(" ", "") == customer.phone.replace("-", "").replace(" ", "")
    trace.record(3, "check_phone_match",
                 f"provided={provided_phone}",
                 f"match={match}",
                 "Customer record")
    return match


def send_mfa_code(customer: Customer, trace: WorkflowTrace) -> tuple:
    """Level 3: Send MFA code to customer."""
    code = "847291"
    issued_at = time.time()
    trace.record(3, "send_mfa_code",
                 f"customer={customer.id}",
                 f"code_sent=True",
                 "Customer record with MFA config")
    return code, issued_at


def validate_mfa_code(code: str, expected_code: str, issued_at: float,
                      trace: WorkflowTrace) -> bool:
    """Level 3: Validate the MFA code the customer provided."""
    valid = check_code_expiry(code, issued_at, trace)
    code_match = code == expected_code
    result = valid and code_match
    trace.record(3, "validate_mfa_code",
                 f"code={code}",
                 f"valid={result}",
                 "Expected code and issue time — needs auth flow context")
    return result


def generate_session_token(customer: Customer, trace: WorkflowTrace) -> str:
    """Level 3: Generate a secure session token."""
    token = f"sess_{customer.id}_{int(time.time())}_a8f3b2"
    trace.record(3, "generate_session_token",
                 f"customer={customer.id}",
                 f"token={token[:20]}...",
                 "Customer ID — needs to know MFA was verified")
    return token


def query_order_database(customer_id: str, order_id: str, trace: WorkflowTrace) -> Optional[Order]:
    """Level 3: Query the order database."""
    if order_id == MOCK_ORDER.id and customer_id == MOCK_ORDER.customer_id:
        trace.record(3, "query_order_database",
                     f"customer={customer_id}, order={order_id}",
                     f"found=True, status={MOCK_ORDER.status.value}",
                     "Customer ID and order ID — needs auth context")
        return MOCK_ORDER
    trace.record(3, "query_order_database",
                 f"customer={customer_id}, order={order_id}",
                 "found=False", "Customer ID and order ID")
    return None


def check_order_status(order: Order, trace: WorkflowTrace) -> bool:
    """Level 3: Check if order status allows refund."""
    refundable = order.status in (OrderStatus.DELIVERED, OrderStatus.SHIPPED)
    trace.record(3, "check_order_status",
                 f"order={order.id}, status={order.status.value}",
                 f"refundable={refundable}",
                 "Order details — needs auth+lookup context")
    return refundable


def check_fulfillment_status(order: Order, trace: WorkflowTrace) -> dict:
    """Level 3: Check fulfillment/shipping status."""
    shipping_info = query_shipping_provider(f"track_{order.id}", trace)
    trace.record(3, "check_fulfillment_status",
                 f"order={order.id}",
                 f"fulfillment={shipping_info['status']}",
                 "Order details — needs full chain context")
    return shipping_info


def check_return_window(order: Order, category: str, trace: WorkflowTrace) -> bool:
    """Level 3: Check if we're within the return window."""
    rules = MOCK_CATEGORY_RULES.get(category, {})
    window_days = rules.get("return_window_days", 30)
    # Mock: always within window
    within_window = True
    trace.record(3, "check_return_window",
                 f"order={order.id}, category={category}, window={window_days}d",
                 f"within_window={within_window}",
                 "Order date, category rules — needs full request context")
    return within_window


def check_item_condition_policy(item: OrderItem, trace: WorkflowTrace) -> dict:
    """Level 3: Check item condition requirements for return."""
    rules = lookup_product_category_rules(item.category, trace)
    result = {
        "eligible": True,
        "requirements": rules.get("condition_requirements", []),
        "restocking_fee_pct": rules.get("restocking_fee_pct", 0),
    }
    trace.record(3, "check_item_condition_policy",
                 f"item={item.product_name}, category={item.category}",
                 f"eligible={result['eligible']}, fee={result['restocking_fee_pct']}%",
                 "Item details — needs order+auth context chain")
    return result


def get_original_charges(order: Order, trace: WorkflowTrace) -> dict:
    """Level 3: Get breakdown of original charges."""
    charges = {
        "items": sum(i.unit_price * i.quantity for i in order.items),
        "shipping": order.shipping_cost,
        "tax": order.tax,
        "promos": sum(i.promo_discount for i in order.items),
        "total": order.total,
    }
    trace.record(3, "get_original_charges",
                 f"order={order.id}",
                 f"total=${charges['total']:.2f}",
                 "Order details — needs full chain for audit trail")
    return charges


def apply_restocking_fee(item: OrderItem, condition: str, trace: WorkflowTrace) -> float:
    """Level 3: Calculate restocking fee for an item."""
    fee_pct = get_fee_schedule(item.category, condition, trace)
    fee = item.unit_price * item.quantity * (fee_pct / 100)
    trace.record(3, "apply_restocking_fee",
                 f"item={item.product_name}, condition={condition}",
                 f"fee=${fee:.2f} ({fee_pct}%)",
                 "Item details, condition — needs refund context")
    return fee


def apply_promo_clawback(order: Order, return_items: list, trace: WorkflowTrace) -> float:
    """Level 3: Calculate promo discount clawback."""
    total_clawback = 0
    for item in return_items:
        if item.promo_code:
            terms = check_promo_terms(item.promo_code, return_items, order, trace)
            if terms["clawback_applies"]:
                total_clawback += terms["clawback_amount"]
    trace.record(3, "apply_promo_clawback",
                 f"order={order.id}, items={len(return_items)}",
                 f"clawback=${total_clawback:.2f}",
                 "Order, items, promo terms — needs full refund context")
    return total_clawback


def create_refund_transaction(amount: float, order: Order, trace: WorkflowTrace) -> dict:
    """Level 3: Create the refund transaction in payment system."""
    result = call_payment_gateway(amount, f"orig_txn_{order.id}", trace)
    trace.record(3, "create_refund_transaction",
                 f"amount=${amount:.2f}, order={order.id}",
                 f"txn={result['transaction_id']}",
                 "Refund amount, order — needs full chain for audit")
    return result


def update_order_status(order: Order, new_status: OrderStatus, trace: WorkflowTrace) -> bool:
    """Level 3: Update order status after refund."""
    order.status = new_status
    trace.record(3, "update_order_status",
                 f"order={order.id}, new_status={new_status.value}",
                 "updated=True",
                 "Order — needs refund context to set correct status")
    return True


def generate_email(customer: Customer, order: Order, refund: dict, trace: WorkflowTrace) -> str:
    """Level 3: Generate confirmation email content."""
    email = (f"Dear {customer.name},\n\n"
             f"Your refund of ${refund['amount']:.2f} for order {order.id} "
             f"has been processed.\nTransaction ID: {refund.get('transaction_id', 'N/A')}\n\n"
             f"You should see the refund in 3-5 business days.")
    trace.record(3, "generate_email",
                 f"customer={customer.name}, order={order.id}",
                 "email_generated=True",
                 "Customer, order, refund details — needs everything")
    return email


def send_email(email: str, to: str, trace: WorkflowTrace) -> bool:
    """Level 3: Send the email."""
    trace.record(3, "send_email",
                 f"to={to}",
                 "sent=True",
                 "Email content and address")
    return True


# ============================================================================
# Level 2 Functions
# ============================================================================

def verify_identity(customer: Customer, provided_email: str, provided_phone: str,
                    trace: WorkflowTrace) -> bool:
    """Level 2: Verify customer identity via email + phone."""
    email_ok = check_email_match(provided_email, customer, trace)
    phone_ok = check_phone_match(provided_phone, customer, trace)
    result = email_ok and phone_ok
    trace.record(2, "verify_identity",
                 f"customer={customer.id}",
                 f"verified={result} (email={email_ok}, phone={phone_ok})",
                 "Customer record, provided credentials")
    return result


def verify_mfa(customer: Customer, trace: WorkflowTrace) -> bool:
    """Level 2: Complete MFA verification flow."""
    code, issued_at = send_mfa_code(customer, trace)
    # Simulate customer entering the code
    customer_code = code  # Mock: correct code
    valid = validate_mfa_code(customer_code, code, issued_at, trace)
    trace.record(2, "verify_mfa",
                 f"customer={customer.id}",
                 f"mfa_verified={valid}",
                 "Customer record, MFA config — needs identity verification context")
    return valid


def create_auth_session(customer: Customer, trace: WorkflowTrace) -> AuthSession:
    """Level 2: Create authenticated session after verification."""
    token = generate_session_token(customer, trace)
    session = AuthSession(
        token=token,
        customer_id=customer.id,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),  # simplified
        mfa_verified=True,
    )
    trace.record(2, "create_auth_session",
                 f"customer={customer.id}",
                 f"session_created=True, token={token[:20]}...",
                 "Customer ID, verification status — needs full auth chain")
    return session


def search_orders(customer_id: str, order_id: str, trace: WorkflowTrace) -> Optional[Order]:
    """Level 2: Search for the customer's order."""
    order = query_order_database(customer_id, order_id, trace)
    trace.record(2, "search_orders",
                 f"customer={customer_id}, order={order_id}",
                 f"found={order is not None}",
                 "Auth session — needs to verify customer is authenticated")
    return order


def validate_order_state(order: Order, trace: WorkflowTrace) -> dict:
    """Level 2: Validate order is in a refundable state."""
    status_ok = check_order_status(order, trace)
    fulfillment = check_fulfillment_status(order, trace)
    result = {
        "refundable": status_ok,
        "fulfillment_status": fulfillment["status"],
        "delivered": fulfillment["status"] == "delivered",
    }
    trace.record(2, "validate_order_state",
                 f"order={order.id}",
                 f"refundable={result['refundable']}, delivered={result['delivered']}",
                 "Order — needs auth context to ensure authorized access")
    return result


def check_refund_eligibility(order: Order, trace: WorkflowTrace) -> dict:
    """Level 2: Check if items are eligible for refund."""
    eligibility = {}
    for item in order.items:
        in_window = check_return_window(order, item.category, trace)
        condition = check_item_condition_policy(item, trace)
        eligibility[item.product_id] = {
            "product_name": item.product_name,
            "in_return_window": in_window,
            "condition_eligible": condition["eligible"],
            "restocking_fee_pct": condition["restocking_fee_pct"],
            "overall_eligible": in_window and condition["eligible"],
        }
    trace.record(2, "check_refund_eligibility",
                 f"order={order.id}, items={len(order.items)}",
                 f"all_eligible={all(e['overall_eligible'] for e in eligibility.values())}",
                 "Order + items — needs auth+order state validation context")
    return eligibility


def calculate_refund_amount(order: Order, return_items: list, trace: WorkflowTrace) -> dict:
    """Level 2: Calculate the final refund amount."""
    charges = get_original_charges(order, trace)

    item_total = sum(i.unit_price * i.quantity for i in return_items)
    restocking = sum(apply_restocking_fee(i, "opened", trace) for i in return_items)
    clawback = apply_promo_clawback(order, return_items, trace)

    refund_amount = item_total - restocking - clawback

    breakdown = {
        "item_total": item_total,
        "restocking_fees": restocking,
        "promo_clawback": clawback,
        "refund_amount": round(refund_amount, 2),
    }

    trace.record(2, "calculate_refund_amount",
                 f"order={order.id}, items={len(return_items)}",
                 f"refund=${breakdown['refund_amount']:.2f} "
                 f"(items=${item_total:.2f} - fees=${restocking:.2f} - clawback=${clawback:.2f})",
                 "Order, items, all policy rules — needs full eligibility context")
    return breakdown


def execute_refund(amount: float, order: Order, trace: WorkflowTrace) -> dict:
    """Level 2: Execute the refund transaction."""
    txn = create_refund_transaction(amount, order, trace)
    update_order_status(order, OrderStatus.REFUNDED, trace)
    trace.record(2, "execute_refund",
                 f"amount=${amount:.2f}, order={order.id}",
                 f"success=True, txn={txn['transaction_id']}",
                 "Refund amount, order — needs full chain for compliance")
    return txn


def send_confirmation(customer: Customer, order: Order, refund_txn: dict,
                      breakdown: dict, trace: WorkflowTrace) -> bool:
    """Level 2: Send refund confirmation to customer."""
    refund_info = {**breakdown, "transaction_id": refund_txn["transaction_id"],
                   "amount": breakdown.get("refund_amount", 0)}
    email = generate_email(customer, order, refund_info, trace)
    sent = send_email(email, customer.email, trace)
    trace.record(2, "send_confirmation",
                 f"customer={customer.name}, order={order.id}",
                 f"sent={sent}",
                 "Customer, order, refund details — needs everything for correct email")
    return sent


# ============================================================================
# Level 1 Functions
# ============================================================================

def authenticate_customer(customer: Customer, provided_email: str, provided_phone: str,
                          trace: WorkflowTrace) -> Optional[AuthSession]:
    """Level 1: Full authentication flow."""
    identity_ok = verify_identity(customer, provided_email, provided_phone, trace)
    if not identity_ok:
        trace.record(1, "authenticate_customer", f"customer={customer.id}",
                     "FAILED: identity verification", "Support request context")
        return None

    if customer.mfa_enabled:
        mfa_ok = verify_mfa(customer, trace)
        if not mfa_ok:
            trace.record(1, "authenticate_customer", f"customer={customer.id}",
                         "FAILED: MFA verification", "Identity verified, MFA required")
            return None

    session = create_auth_session(customer, trace)
    trace.record(1, "authenticate_customer",
                 f"customer={customer.id}",
                 f"authenticated=True, session={session.token[:20]}...",
                 "Customer record, credentials — starting point of the chain")
    return session


def lookup_order(auth_session: AuthSession, order_id: str,
                 trace: WorkflowTrace) -> Optional[dict]:
    """Level 1: Full order lookup and validation flow."""
    order = search_orders(auth_session.customer_id, order_id, trace)
    if order is None:
        trace.record(1, "lookup_order", f"order={order_id}", "FAILED: not found",
                     "Auth session — customer authenticated")
        return None

    state = validate_order_state(order, trace)
    if not state["refundable"]:
        trace.record(1, "lookup_order", f"order={order_id}", "FAILED: not refundable",
                     "Auth session, order found")
        return None

    eligibility = check_refund_eligibility(order, trace)

    result = {
        "order": order,
        "state": state,
        "eligibility": eligibility,
    }

    trace.record(1, "lookup_order",
                 f"order={order_id}",
                 f"found=True, refundable={state['refundable']}, "
                 f"items_eligible={sum(1 for e in eligibility.values() if e['overall_eligible'])}/"
                 f"{len(eligibility)}",
                 "Auth session — needs authenticated customer context")
    return result


def process_refund(auth_session: AuthSession, customer: Customer, order: Order,
                   return_items: list, eligibility: dict,
                   trace: WorkflowTrace) -> RefundResult:
    """Level 1: Full refund processing flow."""
    # Calculate
    breakdown = calculate_refund_amount(order, return_items, trace)

    # Execute
    txn = execute_refund(breakdown["refund_amount"], order, trace)

    # Confirm
    send_confirmation(customer, order, txn, breakdown, trace)

    result = RefundResult(
        success=True,
        refund_id=f"ref_{order.id}_{int(time.time())}",
        amount=breakdown["refund_amount"],
        breakdown=breakdown,
        transaction_id=txn["transaction_id"],
    )

    trace.record(1, "process_refund",
                 f"order={order.id}, items={len(return_items)}",
                 f"success=True, refund=${result.amount:.2f}, txn={result.transaction_id}",
                 "Auth session, order, eligibility — needs EVERYTHING from prior steps")
    return result


# ============================================================================
# Level 0: Top-level orchestrator
# ============================================================================

def handle_support_request(
    customer: Customer,
    provided_email: str,
    provided_phone: str,
    order_id: str,
    return_reason: str = "Changed my mind",
    trace: Optional[WorkflowTrace] = None,
) -> dict:
    """
    Level 0: Handle a complete customer support refund request.

    This is the entry point. In agent orchestration terms:
    - Single agent: runs this entire tree in one context
    - Sub-agents: delegates Level 1 functions to sub-agents with briefings
    - Call agents: delegates Level 1 functions to call agents with full context
    """
    if trace is None:
        trace = WorkflowTrace()

    trace.record(0, "handle_support_request",
                 f"customer={customer.name}, order={order_id}, reason={return_reason}",
                 "Starting...",
                 "Initial support request — no prior context needed")

    # Step 1: Authenticate
    auth_session = authenticate_customer(customer, provided_email, provided_phone, trace)
    if auth_session is None:
        return {"success": False, "error": "Authentication failed", "trace": trace}

    # Step 2: Look up and validate order
    order_info = lookup_order(auth_session, order_id, trace)
    if order_info is None:
        return {"success": False, "error": "Order lookup/validation failed", "trace": trace}

    order = order_info["order"]
    eligibility = order_info["eligibility"]

    # Step 3: Process refund
    eligible_items = [item for item in order.items
                      if eligibility.get(item.product_id, {}).get("overall_eligible", False)]

    if not eligible_items:
        return {"success": False, "error": "No eligible items for refund", "trace": trace}

    refund = process_refund(auth_session, customer, order, eligible_items, eligibility, trace)

    trace.record(0, "handle_support_request",
                 f"customer={customer.name}, order={order_id}",
                 f"COMPLETED: refund=${refund.amount:.2f}, txn={refund.transaction_id}",
                 "Full workflow context accumulated")

    return {
        "success": True,
        "refund": asdict(refund),
        "trace": trace,
    }


# ============================================================================
# Run the mock workflow
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("CUSTOMER SUPPORT WORKFLOW — Mock Execution")
    print("=" * 70)
    print()
    print(f"Customer: {MOCK_CUSTOMER.name} ({MOCK_CUSTOMER.email})")
    print(f"Order:    {MOCK_ORDER.id} (${MOCK_ORDER.total:.2f})")
    print(f"Request:  Refund — 'Changed my mind'")
    print()

    trace = WorkflowTrace()
    result = handle_support_request(
        customer=MOCK_CUSTOMER,
        provided_email="sarah.chen@example.com",
        provided_phone="+1-555-0142",
        order_id="ord_91847",
        return_reason="Changed my mind",
        trace=trace,
    )

    print("=" * 70)
    print("RESULT")
    print("=" * 70)

    if result["success"]:
        refund = result["refund"]
        print(f"✓ Refund processed successfully")
        print(f"  Amount:         ${refund['amount']:.2f}")
        print(f"  Transaction:    {refund['transaction_id']}")
        print(f"  Refund ID:      {refund['refund_id']}")
        print(f"\n  Breakdown:")
        for k, v in refund['breakdown'].items():
            print(f"    {k}: ${v:.2f}" if isinstance(v, float) else f"    {k}: {v}")
    else:
        print(f"✗ Failed: {result['error']}")

    trace.print_trace()

    # Print stats
    print(f"\n{'=' * 70}")
    print(f"STATS")
    print(f"{'=' * 70}")
    print(f"Total workflow steps: {len(trace.steps)}")

    depth_counts = {}
    for step in trace.steps:
        d = step["level"]
        depth_counts[d] = depth_counts.get(d, 0) + 1

    for d in sorted(depth_counts.keys()):
        print(f"  Level {d}: {depth_counts[d]} steps")

    print(f"\nMax depth reached: {max(s['level'] for s in trace.steps)}")
