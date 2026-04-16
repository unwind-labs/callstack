# Deeply Nested Workflow: Three Orchestration Strategies Compared

## The Workflow

A customer support refund request that goes **5 levels deep** with **46 total steps**:

```
handle_support_request                          [L0]
├── authenticate_customer                       [L1]  — 15 tool-call turns
│   ├── verify_identity                         [L2]
│   │   ├── check_email_match                   [L3]
│   │   └── check_phone_match                   [L3]
│   ├── verify_mfa                              [L2]
│   │   ├── send_mfa_code                       [L3]
│   │   └── validate_mfa_code                   [L3]
│   │       └── check_code_expiry               [L4]
│   │           └── validate_totp_window        [L5]
│   └── create_auth_session                     [L2]
├── lookup_order                                [L1]  — 18 tool-call turns
│   ├── search_orders → query_order_database    [L2→L3]
│   ├── validate_order_state                    [L2]
│   │   └── check_fulfillment_status → query_shipping_provider [L3→L4]
│   └── check_refund_eligibility                [L2]
│       └── check_item_condition_policy → lookup_product_category_rules → get_category_exceptions [L3→L4→L5]
└── process_refund                              [L1]  — 13 tool-call turns
    ├── calculate_refund_amount                 [L2]
    │   ├── apply_restocking_fee → get_fee_schedule  [L3→L4]
    │   └── apply_promo_clawback → check_promo_terms → evaluate_clawback_rules [L3→L4→L5]
    ├── execute_refund → create_refund_transaction → call_payment_gateway [L2→L3→L4]
    └── send_confirmation → generate_email + send_email [L2→L3]
```

**Key challenge:** At Level 5, `evaluate_clawback_rules` needs to know: (1) the customer is authenticated, (2) the order was validated, (3) items are eligible, (4) restocking fees were already calculated. Without this context, it can't make correct decisions.

---

## Token Usage Model

Before comparing strategies, we must be precise about how LLM inference actually works:

1. **Every turn, the model re-reads the ENTIRE conversation history as input.** Turn 30 re-reads turns 1–29 before generating turn 30's output.
2. **Input tokens per turn = system prompt + all prior messages** (user, assistant, tool calls, tool results).
3. **Prompt caching** means tokens matching a previously-seen prefix are served at ~90% cost discount — but **they still count as input tokens**. Caching affects **price**, not **volume**.
4. We assume ~1K tokens added per turn (tool call + result + assistant reasoning), and ~5K system prompt.

The total input tokens for an N-turn conversation is:

```
Total input = Σ(t=1..N) [C₀ + t × Δ]  =  N × C₀ + Δ × N(N+1)/2
```

This is **quadratic in N** — the fundamental reason long conversations get expensive.

---

## Strategy 1: Single Agent Context

**How it works:** One agent runs all 46 steps sequentially. Every step's reasoning and tool output accumulates in context.

### Token Math

`C₀ = 5K` (system prompt), `Δ = 1K` per turn, `N = 46` turns.

```
Total input = 46 × 5K  +  1K × 46 × 47 / 2
            = 230K      +  1,081K
            = 1,311K input tokens
```

Output: `46 turns × ~500 tokens = 23K output tokens`

**Prompt caching:** The 5K system prompt prefix is cached across all 46 turns → `45 × 5K = 225K` cache hits. The growing conversation tail changes each turn so is NOT cacheable across turns.

### Execution Profile

```
Turn  1:   6K input    ← clean start
Turn 10:  15K input    ← still manageable
Turn 20:  25K input    ← context growing
Turn 30:  35K input    ← significant accumulation
Turn 46:  51K input    ← full context, attention diluted
```

### Analysis

| Dimension | Assessment |
|-----------|-----------|
| **Context at Level 5** | ✅ Perfect — agent has seen everything. `evaluate_clawback_rules` knows the full chain. |
| **Correctness** | ✅ High — no information loss. |
| **Context pollution** | ❌ Severe — by turn 46, context contains every intermediate tool result, failed reasoning path, and raw API response from all 46 steps. |
| **Quality degradation** | ❌ Real — by turn 40+, attention is diluted across 40+ turns of irrelevant intermediate work. The agent may make errors in refund calculation because it's attending to MFA validation details and shipping provider responses. |
| **Context window risk** | ❌ High — 51K tokens at turn 46. With more complex orders (20 items, complex policies), easily exceeds 200K window. |
| **Total input tokens** | **1,311K** |
| **Cacheable tokens** | ~225K (system prompt only) |

---

## Strategy 2: Sub-Agents (Claude Code `Task` tool)

**How it works:** Parent delegates Level 1 tasks to **isolated sub-agents**. Each sub-agent starts fresh with only a **briefing prompt** (~1K tokens). Sub-agent traces return in full to the parent.

### Token Math

**Parent agent** (6 turns: initial intake, dispatch 3 tasks, handle results, wrap up):
```
C₀ = 5K, N = 6
Total = 6 × 5K + 1K × 6 × 7/2 = 30K + 21K = 51K
```

**Sub-agent 1 — authenticate** (15 turns, fresh session):
```
C₀ = 6K (5K sys + 1K briefing), N = 15
Total = 15 × 6K + 1K × 15 × 16/2 = 90K + 120K = 210K
```

**Sub-agent 2 — lookup_order** (18 turns, fresh session):
```
C₀ = 6K, N = 18
Total = 18 × 6K + 1K × 18 × 19/2 = 108K + 171K = 279K
```

**Sub-agent 3 — process_refund** (13 turns, fresh session):
```
C₀ = 6K, N = 13
Total = 13 × 6K + 1K × 13 × 14/2 = 78K + 91K = 169K
```

```
Grand total input = 51K + 210K + 279K + 169K = 709K
```

Output: `52 turns × ~500 = 26K output tokens`

**Prompt caching:** Each sub-agent is a **fresh session** — no prefix sharing with the parent or siblings. Each can only cache its own ~5K system prompt across its own turns. Total cacheable: `5K × (5 + 14 + 17 + 12) = 240K`. Worse cache efficiency than single-agent despite fewer total tokens.

### Why fewer tokens than Strategy 1?

Breaking the quadratic curve. Three smaller quadratics sum to less than one big one:
```
Single:      46² / 2 = 1,058  (quadratic term)
Sub-agents:  15²/2 + 18²/2 + 13²/2 = 112 + 162 + 84 = 358  (quadratic terms)
```

The quadratic term drops **3x** by splitting.

### The Information Loss Problem

Sub-agent 3 (`process_refund`) receives this briefing:

> *"Process refund for order ord_91847. Items: Wireless Headphones ($149.99), Phone Case ×2 ($24.99 each). Promo SUMMER20 applied to headphones. Customer is authenticated. Items are eligible."*

What it does **NOT** know:
- That `get_category_exceptions` found `["defective", "wrong_item"]` as exceptions for electronics
- That electronics have a 30-day return window (we're on day 24)
- That the shipping provider confirmed delivery signed by "S. Chen"
- That the partial-return exception for SUMMER20 requires remaining value > 50%
- The exact eligibility details per item

At Level 5, `evaluate_clawback_rules` must decide whether the SUMMER20 promo clawback applies. The correct answer depends on the partial-return exception — but the sub-agent was never told about it. The developer writing the briefing would need to anticipate every piece of context that every function 4 levels down might need. This is fragile and error-prone.

```
Fidelity at each delegation:  parent → 85% → sub-agent
At Level 5 (3 delegations):  85% × 85% × 85% ≈ 61% fidelity
```

### Analysis

| Dimension | Assessment |
|-----------|-----------|
| **Context at Level 5** | ❌ Briefing only — lossy compression of parent's knowledge. |
| **Correctness** | ⚠️ Degraded — depends entirely on briefing quality. Miss one detail and Level 5 makes wrong decisions. |
| **Context pollution** | ⚠️ Moderate — each sub-agent's **full trace** returns to the parent. After 3 sub-agents, parent has ~46K tokens of returned traces. |
| **Information loss** | ❌ High — each briefing is a lossy compression. Compounds over nesting levels. |
| **Developer burden** | ❌ High — developer must manually craft briefings, anticipating what deeply nested functions might need. |
| **Total input tokens** | **709K** |
| **Cacheable tokens** | ~240K (per-agent system prompts only) |

---

## Strategy 3: Call Agents (Session Cloning)

**How it works:** Parent delegates Level 1 tasks to **Call Agents** that inherit the parent's **full session** via session cloning. Each returns only a **compacted result** (~100–150 tokens).

### Token Math

**Parent context at each dispatch point:**
- Before CallAgent 1: parent has done 2 turns → `C₀ = 7K`
- Before CallAgent 2: parent has 7K + CA1 result (~0.1K) + 1 turn → `C₀ ≈ 8K`
- Before CallAgent 3: 8K + CA2 result (~0.15K) + 1 turn → `C₀ ≈ 9K`

**Parent agent** (6 turns):
```
C₀ = 5K, N = 6
Total = 30K + 21K = 51K
```

**Call Agent 1 — authenticate** (15 turns, inherits parent's 7K):
```
C₀ = 7K, N = 15
Total = 15 × 7K + 1K × 15 × 16/2 = 105K + 120K = 225K
```

**Call Agent 2 — lookup_order** (18 turns, inherits parent's 8K):
```
C₀ = 8K, N = 18
Total = 18 × 8K + 1K × 18 × 19/2 = 144K + 171K = 315K
```

**Call Agent 3 — process_refund** (13 turns, inherits parent's 9K):
```
C₀ = 9K, N = 13
Total = 13 × 9K + 1K × 13 × 14/2 = 117K + 91K = 208K
```

```
Grand total input = 51K + 225K + 315K + 208K = 799K
```

Output: `52 turns × ~500 = 26K output tokens`

### Why more tokens than Strategy 2?

Each Call Agent carries the inherited prefix (~7–9K) instead of just a briefing (~1K). This extra **2–4K per turn** accumulates:

```
Extra inherited context re-read across all CA turns:
  CA1: 1K extra × 15 turns =  15K
  CA2: 2K extra × 18 turns =  36K
  CA3: 3K extra × 13 turns =  39K
  Total overhead:             ~90K extra vs sub-agents
```

### But: Prompt Cache Changes the Cost Picture

The inherited prefix is an **exact** prefix match with the parent's session. This means Anthropic's prompt caching can reuse cached tokens for the entire inherited portion, every turn:

```
Call Agent cache hits:
  CA1: 7K prefix × 14 subsequent turns =  98K cacheable
  CA2: 8K prefix × 17 subsequent turns = 136K cacheable
  CA3: 9K prefix × 12 subsequent turns = 108K cacheable
  Total cache-eligible:                   342K tokens
```

At 90% cache discount, these 342K tokens cost the equivalent of ~34K tokens.

Compare to sub-agents' 240K cacheable (only system prompts) → equivalent of ~24K.

**Net effective cost** (non-cached + 10% of cached):

```
Strategy 2: (709K - 240K) + 240K × 0.1 = 469K + 24K = 493K effective
Strategy 3: (799K - 342K) + 342K × 0.1 = 457K + 34K = 491K effective
```

Nearly identical effective cost — but Strategy 3 has **full context fidelity**.

### Analysis

| Dimension | Assessment |
|-----------|-----------|
| **Context at Level 5** | ✅ Perfect — Call Agent inherited full session. Identical context to single-agent approach. |
| **Correctness** | ✅ High — no information loss at any level. `evaluate_clawback_rules` knows the entire chain. |
| **Context pollution** | ✅ Minimal — parent absorbs ~370 tokens of compacted results instead of ~46K of raw traces. |
| **Information loss** | ✅ None — session cloning is lossless. |
| **Developer burden** | ✅ Low — no briefings to write. Just describe the task. |
| **Total input tokens** | **799K** |
| **Cacheable tokens** | ~342K (inherited prefix — best cache reuse) |

---

## Side-by-Side Comparison

### Raw Numbers

| Metric | Single Agent | Sub-Agents | Call Agents |
|--------|:-----------:|:----------:|:-----------:|
| Total turns | 46 | 52 (6+15+18+13) | 52 (6+15+18+13) |
| **Total input tokens** | **1,311K** | **709K** | **799K** |
| Total output tokens | 23K | 26K | 26K |
| Cacheable input | 225K | 240K | **342K** |
| **Effective cost** (non-cached + 10% cached) | **~1,109K** | **~493K** | **~491K** |

### Qualitative

| Dimension | Single Agent | Sub-Agents | Call Agents |
|-----------|:---:|:---:|:---:|
| Context at Level 5 | ✅ Full | ❌ Briefing only | ✅ Full (cloned) |
| Correctness at depth | ✅ High | ⚠️ Degraded | ✅ High |
| Context pollution on parent | ❌ Severe | ⚠️ Moderate | ✅ Minimal |
| Information loss | None | ❌ High (lossy) | None |
| Quality at turn 40+ | ❌ Degrades | ⚠️ Varies | ✅ Stable |
| Developer burden | Low | ❌ High (briefings) | Low |
| Composability (nesting) | None | Shallow | ✅ Deep |
| Context window risk | ❌ High | ⚠️ Medium | ✅ Low |

### The Tradeoff Matrix

```
                    Context Fidelity
                    Low ◄─────────────────► High
                    │                         │
  Token        Low  │   Sub-Agents            │  ← impossible without
  Efficiency        │   (709K, lossy)         │     session cloning
                    │                         │
                    │               Call Agents│
               High │              (799K,     │
                    │               lossless)  │
                    │                         │
                    │         Single Agent     │
                    │         (1,311K,         │
                    │          lossless)       │
                    └─────────────────────────┘

Call Agents occupy the previously impossible quadrant:
high fidelity + reasonable efficiency.
```

---

## The Critical Difference: Level 5 Decisions

### `evaluate_clawback_rules` at Level 5

**Single Agent:** Has everything in context. Knows the full chain. Makes correct decision. But context is bloated with 34 turns of prior work, risking attention dilution.

**Sub-Agent:** Was briefed "Process refund for order ord_91847 with items X, Y. Promo SUMMER20 applied." But:
- Does it know the partial-return exception from `get_category_exceptions`? Only if the developer included it in the briefing.
- Does it know the restocking fees already calculated? Only if passed explicitly.
- Does it know the order was delivered (not just shipped)? Only if mentioned.
- **In practice:** Developers forget. The sub-agent gets ~60-85% of the context and makes correspondingly degraded decisions.

**Call Agent:** Has everything in context identically to single-agent. Session was cloned, not briefed. Zero information loss. Makes correct decision. And parent's context stays clean.

### The Telephone Game

```
Sub-agents (each briefing is lossy):
  Parent → 85% → SubAgent1
  Parent → 85% → SubAgent2
  Parent → 85% → SubAgent3
    SubAgent3 at Level 5: working with 85% of what it needs

Call agents (each clone is lossless):
  Parent → 100% → CallAgent1
  Parent → 100% → CallAgent2  (+ CA1's compacted result)
  Parent → 100% → CallAgent3  (+ CA1 & CA2's compacted results)
    CallAgent3 at Level 5: working with 100% of what it needs
```

If Call Agents themselves nest further (CA3 calls CA3a calls CA3b), the parent context is still inherited at each level. Sub-agents nesting would be: `85% × 85% × 85% ≈ 61%` fidelity.

---

## When to Use Each Strategy

| Strategy | Best For | Avoid When |
|----------|----------|-----------|
| **Single Agent** | Simple tasks (< 15 steps). Quick scripts. One-shot generation. | > 20 steps. Deep nesting. Long-running workflows. |
| **Sub-Agents** | Truly independent tasks that don't need parent context. Embarrassingly parallel work. Tasks with self-contained inputs (e.g., "format this CSV"). | Deep nesting. Tasks where correctness depends on prior chain. Anything where you'd struggle to write the briefing. |
| **Call Agents** | Deep workflows (3+ levels). Tasks requiring full context fidelity. Long-running orchestrations where parent context must stay clean. Cost-sensitive apps (cache reuse). | Trivially simple tasks (overhead not worth it). Truly independent tasks that don't need context. |

---

## Summary

| | Single Agent | Sub-Agents | Call Agents |
|---|---|---|---|
| **Total input tokens** | 1,311K | 709K | 799K |
| **Effective cost** | ~1,109K equiv | ~493K equiv | ~491K equiv |
| **Context fidelity** | 100% (but polluted) | ~61-85% | 100% (and clean) |
| **Parent context after workflow** | ~51K tokens (polluted) | ~46K (sub-agent traces) | ~370 tokens (compacted) |

**The core insight:** Sub-agents and Call Agents have nearly identical effective cost (~491-493K equivalent tokens). But sub-agents achieve this by sacrificing information — each briefing loses context. Call Agents achieve it by leveraging prompt caching — the inherited prefix is expensive in raw tokens but cheap in actual cost.

**Call Agents don't win on raw token count.** They win because they make the previously impossible tradeoff possible: full context fidelity at depth, without context pollution on the parent, at the same effective cost as lossy sub-agents.
