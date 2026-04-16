# /process-refund — Calculate and Process Refund

Calculate the refund amount (applying restocking fees and promo clawback),
process the payment, and send a confirmation email.

## Inputs

- Order ID
- Which items are eligible for refund
- Return reason
- Item condition (if not provided, ask the customer)

## Procedure

### 1. Get Item Condition

If item condition was not provided in the task, ask the customer:

> What is the condition of the items being returned? (unopened, opened, or damaged)

### 2. Calculate Refund

Use the `calculate_refund` tool with the order ID and item condition. Review
the breakdown:
- Items total
- Restocking fees per item
- Promo clawback amounts
- Net refund amount

### 3. Process Payment

Use `process_refund_payment` with the order ID and the calculated net refund
amount. Confirm the transaction ID and status.

### 4. Send Confirmation

Use `send_confirmation_email` to notify the customer of the refund. Include
the refund amount, transaction ID, and expected timeline.

## Output

Report the full refund result:
- Net refund amount and breakdown
- Transaction ID
- Estimated refund timeline
- Confirmation email status
