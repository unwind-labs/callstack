# Customer Support Refund Workflow — Natural Language Instructions

This document describes a deeply nested customer support workflow for processing
refund requests. It serves as a natural language equivalent of
`customer_support_workflow.py` — suitable for use as agent instructions.

## Scenario

A customer contacts support requesting a refund. The agent must:

1. **Authenticate the customer** (verify identity → check MFA → create session)
2. **Look up the order** (search orders → validate order state → check eligibility)
3. **Process the refund** (calculate amount → apply policy rules → execute refund → send confirmation)

The workflow nests 5 levels deep in places.

---

## Mock Data

Use the following mock data throughout the workflow:

**Customer:**
- ID: `cust_7829`
- Name: Sarah Chen
- Email: sarah.chen@example.com
- Phone: +1-555-0142
- MFA enabled: yes
- MFA secret: `JBSWY3DPEHPK3PXP`

**Order (`ord_91847`):**
- Customer ID: `cust_7829`
- Status: delivered
- Fulfillment status: delivered
- Ordered: 2026-03-15T10:30:00Z
- Delivered: 2026-03-20T14:22:00Z
- Shipping cost: $9.99
- Tax: $17.10
- Total: $216.06
- Items:
  1. Wireless Headphones (product `prod_001`, category: electronics, qty 1, $149.99, promo code `SUMMER20` with $30.00 discount)
  2. Phone Case (product `prod_002`, category: accessories, qty 2, $24.99 each, no promo)

**Promo Terms (`SUMMER20`):**
- Type: percentage (20% off)
- Clawback on return: yes
- Clawback exceptions: partial return over 50% of order value
- Minimum order value: $100.00

**Category Return Rules:**
| Category    | Return Window | Restocking Fee | Exceptions             | Condition Requirements         |
|-------------|---------------|----------------|------------------------|--------------------------------|
| electronics | 30 days       | 15%            | defective, wrong_item  | original_packaging, all_accessories |
| accessories | 60 days       | 0%             | (none)                 | unused                         |

**Fee Schedule (by item condition):**
| Category    | Base | Opened | Damaged |
|-------------|------|--------|---------|
| electronics | 15%  | 20%    | 50%     |
| accessories | 0%   | 5%     | 25%     |

---

## Workflow Tree

```
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
```

---

## Step-by-Step Instructions

### Level 0: Handle Support Request

**Input:** Customer Sarah Chen contacts support requesting a refund for order `ord_91847`. She provides her email (`sarah.chen@example.com`), phone (`+1-555-0142`), and return reason ("Changed my mind").

**Steps:**
1. Authenticate the customer (see Level 1: Authenticate Customer). If authentication fails, return failure with error "Authentication failed".
2. Look up and validate the order (see Level 1: Lookup Order). If lookup/validation fails, return failure with error "Order lookup/validation failed".
3. From the eligibility results, identify which items are eligible for refund. If no items are eligible, return failure with error "No eligible items for refund".
4. Process the refund for all eligible items (see Level 1: Process Refund).
5. Return success with the refund details.

---

### Level 1: Authenticate Customer

**Goal:** Verify the customer's identity and create an authenticated session.

**Steps:**

1. **Verify Identity** (Level 2)
   - **Check email match** (Level 3): Compare the provided email against the customer record (case-insensitive). Must match.
   - **Check phone match** (Level 3): Compare the provided phone against the customer record (ignore dashes and spaces). Must match.
   - Both must pass. If either fails, authentication fails.

2. **Verify MFA** (Level 2) — only if the customer has MFA enabled
   - **Send MFA code** (Level 3): Generate a code (`847291`) and record the issue timestamp. In the mock, the code is sent to the customer.
   - **Validate MFA code** (Level 3): Customer submits the code. Check:
     - **Check code expiry** (Level 4): The code must be less than 300 seconds old (5 minutes).
       - **Validate TOTP window** (Level 5): Verify the code is within the acceptable 30-second TOTP window. (Mock: always valid.)
     - The submitted code must match the expected code.
   - If MFA validation fails, authentication fails.

3. **Create Auth Session** (Level 2)
   - **Generate session token** (Level 3): Create a token in the format `sess_{customer_id}_{timestamp}_a8f3b2`.
   - Build an `AuthSession` with the token, customer ID, creation time, expiry time, and `mfa_verified=true`.

**Output:** An authenticated session, or `null` on failure.

---

### Level 1: Lookup Order

**Goal:** Find the order, validate its state, and check refund eligibility for each item.

**Steps:**

1. **Search Orders** (Level 2)
   - **Query order database** (Level 3): Look up the order by customer ID and order ID. Return the order if found, or `null` if not found.
   - If no order found, lookup fails.

2. **Validate Order State** (Level 2)
   - **Check order status** (Level 3): The order must be in status `delivered` or `shipped` to be refundable.
   - **Check fulfillment status** (Level 3):
     - **Query shipping provider** (Level 4): Call the external shipping provider with tracking ID `track_{order_id}`. Returns status `delivered`, delivered at `2026-03-20T14:22:00Z`, signed by `S. Chen`.
   - If the order is not refundable, lookup fails.

3. **Check Refund Eligibility** (Level 2) — for each item in the order:
   - **Check return window** (Level 3): Look up the category's `return_window_days` (electronics = 30 days, accessories = 60 days). Determine if we're within the window from delivery date. (Mock: always within window.)
   - **Check item condition policy** (Level 3):
     - **Lookup product category rules** (Level 4): Get the full rule set for the item's category.
       - **Get category exceptions** (Level 5): Get the special exceptions list for the category (e.g., electronics: `["defective", "wrong_item"]`).
     - Return eligibility status, condition requirements, and restocking fee percentage.
   - An item is overall eligible if it's within the return window AND condition-eligible.

**Output:** The order, its state, and per-item eligibility map. For the mock data, both items are eligible.

---

### Level 1: Process Refund

**Goal:** Calculate the refund amount, execute the payment, and send confirmation.

**Steps:**

1. **Calculate Refund Amount** (Level 2)
   - **Get original charges** (Level 3): Sum up the order's charges:
     - Items total: sum of (unit_price × quantity) for all items = $199.97
     - Shipping: $9.99
     - Tax: $17.10
     - Promo discounts: sum of promo_discount for all items = $30.00
     - Order total: $216.06
   - **Apply restocking fee** (Level 3) — for each return item:
     - **Get fee schedule** (Level 4): Look up the fee percentage for the item's category and condition (assume "opened"). Electronics opened = 20%, accessories opened = 5%.
     - Calculate fee: `unit_price × quantity × (fee_pct / 100)`
       - Headphones: $149.99 × 1 × 0.20 = $30.00
       - Phone Case: $24.99 × 2 × 0.05 = $2.50
     - Total restocking fees: $32.50
   - **Apply promo clawback** (Level 3) — for each item that used a promo:
     - **Check promo terms** (Level 4) for `SUMMER20`:
       - **Evaluate clawback rules** (Level 5): Check if clawback applies. Clawback is enabled for `SUMMER20`. However, if the remaining order value (items NOT using this promo) exceeds 50% of the order total, the clawback is waived. Phone Cases ($49.98) do not exceed 50% of $216.06 ($108.03), so clawback applies.
       - Clawback amount: $30.00 (the original promo discount)
     - Total promo clawback: $30.00
   - **Final refund amount:** item_total ($199.97) − restocking ($32.50) − clawback ($30.00) = **$137.47**

2. **Execute Refund** (Level 2)
   - **Create refund transaction** (Level 3):
     - **Call payment gateway** (Level 4): Submit refund of $137.47 referencing original transaction `orig_txn_ord_91847`. Returns transaction ID `txn_ref_88291`, status `completed`.
   - **Update order status** (Level 3): Set the order status to `refunded`.

3. **Send Confirmation** (Level 2)
   - **Generate email** (Level 3): Compose email to customer:
     ```
     Dear Sarah Chen,

     Your refund of $137.47 for order ord_91847 has been processed.
     Transaction ID: txn_ref_88291

     You should see the refund in 3-5 business days.
     ```
   - **Send email** (Level 3): Send the email to sarah.chen@example.com.

**Output:** A `RefundResult` with:
- success: true
- refund_id: `ref_ord_91847_{timestamp}`
- amount: $137.47
- breakdown: { item_total: $199.97, restocking_fees: $32.50, promo_clawback: $30.00, refund_amount: $137.47 }
- transaction_id: `txn_ref_88291`

---

## Expected Final Result

```
Success: true
Refund Amount: $137.47
Transaction ID: txn_ref_88291
Total Workflow Steps: ~30 (across levels 0-5)
Max Depth: 5
```

---

## Context Propagation Notes

This workflow illustrates a key challenge for agent orchestration: **deep functions need context from far up the call chain.** For example:

- `validate_totp_window` (Level 5) only needs the MFA secret and code, but understanding *why* it's running requires knowing this is part of a refund request for Sarah Chen's order `ord_91847`.
- `evaluate_clawback_rules` (Level 5) needs the promo terms and return items, but also requires the full chain to be verified: customer authenticated → order validated → eligibility confirmed → restocking fee already calculated.
- `call_payment_gateway` (Level 4) needs the refund amount and original transaction, but audit compliance requires the full chain of how that amount was derived.

This is what makes the workflow interesting for comparing single-agent, sub-agent, and call-agent orchestration strategies.
