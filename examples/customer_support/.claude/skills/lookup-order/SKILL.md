# /lookup-order — Look Up Order and Check Refund Eligibility

Find an order, verify its state, and determine which items are eligible for
refund.

## Inputs

- Order ID
- Customer ID

## Procedure

### 1. Retrieve Order

Use the `get_order` tool to look up the order. If not found or doesn't belong
to the customer, report the error.

### 2. Verify Fulfillment

Use `get_shipping_status` to confirm delivery status. Report the delivery
date and signature if available.

### 3. Check Refund Eligibility

Use `check_refund_eligibility` to evaluate each item against return policies.
For each item, report:
- Product name and category
- Whether it's within the return window
- Applicable restocking fee percentage

Also use `get_return_policy` for each item's category to include full policy
details in the response.

## Output

Report:
- Order status and fulfillment details
- Per-item eligibility with return window and restocking fee info
