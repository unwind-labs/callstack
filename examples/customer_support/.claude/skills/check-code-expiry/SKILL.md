# /check-code-expiry — Collect and Validate MFA Code

Prompt the customer for their MFA code, validate it, and retry once if wrong.

## Inputs

- Customer ID

## Procedure

### 1. Ask for Code

Ask the customer:

> A verification code has been sent to your device. Please enter the 6-digit code.

### 2. Validate

Use the `validate_mfa_code` tool with the customer ID and the code the
customer provided.

- If valid: report success.
- If invalid: proceed to step 3.

### 3. Retry (one attempt)

Ask the customer:

> That code was incorrect. Please check your authenticator app and enter the current 6-digit code.

Use `validate_mfa_code` again with the new code.

- If valid: report success (2 attempts used).
- If invalid: report failure — maximum attempts exhausted.

## Output

Report the validation result:
- Whether the code was accepted
- Number of attempts used (out of 2)
