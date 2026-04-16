# /authenticate-customer — Verify Customer Identity and MFA

Authenticate a customer before processing any account actions.

## Inputs

- Customer ID
- Customer email
- Customer phone number

## Procedure

### 1. Verify Identity

Use the `verify_customer_identity` tool with the provided customer ID, email,
and phone number. Both email and phone must match. If either fails, return an
authentication failure with details on which check failed.

### 2. MFA Verification

Look up the customer with `get_customer` to check if MFA is enabled.

If MFA is enabled, verify MFA for customer {customer_id}. This may require
interacting with the customer to collect their verification code.

If MFA verification fails, return authentication failure.

### 3. Create Session

On success, generate a session:
- Token format: `sess_{customer_id}_{timestamp}_a8f3b2`
- Expiry: 1 hour from creation
- MFA verified: true

## Output

Report the authentication result:
- Customer name and ID
- Session token
- MFA verification status
- Identity check results (email and phone)
