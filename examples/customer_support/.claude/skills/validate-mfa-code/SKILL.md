# /validate-mfa-code — Validate MFA Code

Collect an MFA code from the customer and validate it.

## Inputs

- Customer ID

## Procedure

### 1. Collect Code

Collect and check the MFA code for customer {customer_id}. Prompt the
customer for their 6-digit code, validate it, and retry once if incorrect.

### 2. Return Result

Report the validation result — whether the code was accepted and how many
attempts were used.
