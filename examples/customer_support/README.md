# Customer Support Refund Agent

This is a working example of [agent-callstack](../../README.md) — the `/call` skill that gives LLM agents function-call semantics (full context down, compact results up).

## Run it

```bash
cd examples/customer_support
claude
```

Then say:

```
Process a refund for Sarah Chen (cust_7829), order ord_91847.
Email: sarah.chen@example.com, Phone: +1-555-0142.
```

The agent authenticates the customer (with MFA), looks up the order, checks return policies and promo clawback rules, calculates the refund, processes payment, and sends a confirmation email.

When asked for the MFA code, enter `847291`. When asked about item condition, say `damaged` (or `opened` or `unopened` — each produces different restocking fees).

## What happens

```
Orchestrator
  │
  ├─ /call authenticate-customer              16.6s
  │    ├── verify_customer_identity              (inline)
  │    ├── get_customer                          (inline)
  │    ├── send_mfa_code                         (inline)
  │    └── op: yield   "Enter MFA code"
  │         user: "847291"
  │    └── validate_mfa_code                     (inline)
  │    └── op: return  "Authenticated, session created"
  │
  ├─ /call lookup-order                        27.0s
  │    ├── get_order, get_shipping_status         (inline)
  │    ├── check_refund_eligibility               (inline)
  │    └── op: return  "2 items eligible, within return window"
  │
  └─ /call process-refund                      9.2s + 24.6s
       └── op: yield   "Item condition?"
            user: "damaged"
       ├── calculate_refund                       (inline)
       ├── process_refund_payment                 (inline)
       ├── send_confirmation_email                (inline)
       └── op: return  "Refund $82.48, txn_ref_88291"
```

Three `/call` invocations. Each one forks the parent's full session context (so the child knows the entire conversation history), does its work, and returns a compact result. The parent never sees the 46 intermediate tool calls.

## The order

| Item | Category | Qty | Price | Promo |
|------|----------|-----|-------|-------|
| Wireless Headphones | electronics | 1 | $149.99 | SUMMER20 (−$30) |
| Phone Case | accessories | 2 | $24.99 ea | — |
| | | | **Total:** | **$216.06** (incl. $9.99 shipping, $17.10 tax) |

Return policies: electronics have a 30-day window with restocking fees (15% base, 20% opened, 50% damaged). Accessories have a 60-day window, no restocking fee unless damaged (25%). SUMMER20 promo has a clawback rule — the $30 discount gets clawed back unless the remaining order exceeds 50% of the original total.

The refund calculation is non-trivial. The agent has to get it right — and it does, because each child agent inherits the full conversation context, not a lossy briefing.

## Files

```
.
├── .mcp.json                          # MCP server config
├── mcp_server.py                      # Backend tools (FastMCP) — 12 tools, mock data
├── data/mock_data.md                  # Test data reference
├── .claude/
│   ├── CLAUDE.md                      # Orchestrator instructions (main)
│   └── skills/
│       ├── authenticate-customer/     # Identity verification + MFA
│       ├── verify-mfa/                # MFA flow (send, collect, validate)
│       ├── check-code-expiry/         # MFA code collection with retry
│       ├── validate-mfa-code/         # MFA code validation
│       ├── lookup-order/              # Order retrieval + eligibility check
│       └── process-refund/            # Refund calculation + payment + email
```

Skills are short. `/authenticate-customer` is ~40 lines of markdown. It doesn't teach the model how to verify identity — the model knows how. The skill specifies *which tools to call, what the retry policy is, and what to tell the customer*. It's the delta between "model does it from scratch" and "model does it the way we want."

## The MCP server

`mcp_server.py` is a FastMCP server that simulates a customer support backend. 12 tools:

| Tool | What it does |
|------|-------------|
| `get_customer` | Look up customer profile |
| `verify_customer_identity` | Match email + phone |
| `send_mfa_code` | Send MFA code (mock: always 847291) |
| `validate_mfa_code` | Validate submitted code |
| `get_order` | Order details with items, prices, promos |
| `get_shipping_status` | Delivery confirmation + signature |
| `check_refund_eligibility` | Per-item eligibility against return policies |
| `get_return_policy` | Category-specific return rules |
| `get_promo_terms` | Promo clawback rules |
| `calculate_refund` | Net refund after fees and clawback |
| `process_refund_payment` | Execute refund, return transaction ID |
| `send_confirmation_email` | Email customer with refund details |

MFA state is persisted to `/tmp/mcp_mfa_codes.json` so it survives across the multiple agent processes that touch it during a single workflow.
