# Mock Data — Customer Support Refund Workflow

Use this data for all lookups and decisions throughout the workflow. Do NOT call external APIs — use these values directly.

## Customer

- ID: `cust_7829`
- Name: Sarah Chen
- Email: sarah.chen@example.com
- Phone: +1-555-0142
- MFA enabled: yes
- MFA secret: `JBSWY3DPEHPK3PXP`

## Order (`ord_91847`)

- Customer ID: `cust_7829`
- Status: delivered
- Fulfillment status: delivered
- Ordered: 2026-03-15T10:30:00Z
- Delivered: 2026-03-20T14:22:00Z
- Shipping cost: $9.99
- Tax: $17.10
- Total: $216.06
- Items:
  1. **Wireless Headphones** — product `prod_001`, category: electronics, qty 1, $149.99, promo code `SUMMER20` with $30.00 discount
  2. **Phone Case** — product `prod_002`, category: accessories, qty 2, $24.99 each, no promo

## Promo Terms (`SUMMER20`)

- Type: percentage (20% off)
- Clawback on return: yes
- Clawback exceptions: partial return over 50% of order value
- Minimum order value: $100.00

## Category Return Rules

| Category    | Return Window | Restocking Fee | Exceptions             | Condition Requirements             |
|-------------|---------------|----------------|------------------------|------------------------------------|
| electronics | 30 days       | 15%            | defective, wrong_item  | original_packaging, all_accessories |
| accessories | 60 days       | 0%             | (none)                 | unused                             |

## Fee Schedule (by item condition)

| Category    | Base | Opened | Damaged |
|-------------|------|--------|---------|
| electronics | 15%  | 20%    | 50%     |
| accessories | 0%   | 5%     | 25%     |

## Shipping Provider Response

For tracking ID `track_ord_91847`:
- Status: delivered
- Delivered at: 2026-03-20T14:22:00Z
- Signed by: S. Chen

## MFA Code

- Generated code: `847291`
- Always valid (mock TOTP window check passes)

## Payment Gateway Response

For refund of $137.47 against `orig_txn_ord_91847`:
- Transaction ID: `txn_ref_88291`
- Status: completed
