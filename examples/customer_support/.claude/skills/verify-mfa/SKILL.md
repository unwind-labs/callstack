# /verify-mfa — MFA Verification Flow

Handle the full MFA verification process for a customer.

## Inputs

- Customer ID
- Whether MFA is enabled

## Procedure

### 1. Check MFA Requirement

If MFA is not enabled for this customer, return success immediately — no
verification needed.

### 2. Send Code

Use the `send_mfa_code` tool to send a verification code to the customer's
registered device. Inform the caller that a code has been sent.

### 3. Collect and Validate Code

Collect and validate the MFA code for customer {customer_id}. This will
require asking the customer for their code.

### 4. Return Result

Report whether MFA verification succeeded or failed based on the validation
agent's result.
