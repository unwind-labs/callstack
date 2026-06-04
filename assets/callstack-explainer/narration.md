# Explainer narration

This file is the **single source of truth** for the voiceover. `scripts/gen-voiceover.ts`
parses each `**S<id> — ...**` header and the `> ...` line below it, then synthesizes
`public/vo/<id>.mp3` via xAI TTS. Edit the text, then run `bun run scripts/gen-voiceover.ts`.

**S1 — Problem**
> <emphasis>Deeply nested workflows are where agents fall apart.</emphasis> LLMs follow instructions probabilistically — so the deeper the flow, the more likely it is to skip a step, run one out of order, or lose its place. [pause] And the usual fixes don't help: compaction is lossy, subagents don't nest, and a bigger context window just fills and rots. So the agent drifts, until it can't say what it was doing, or what's left.

**S2 — Solution**
> The fix is to stop leaving control flow to the model. Callstack gives your agent a real call stack — the deterministic structure programs use to run deep logic. One command — slash call — forks the session, runs a task in its own clean workspace, and hands back one compact result. If we ask the model to keep track of the stack, it is not reliable, but with a deterministic call stack, calls can nest as deep as the work needs, and each one returns to its caller, which picks up right where it left off.

**S3 — Results**
> We tested agents on workflows from one level deep to seven, where the call tree gets genuinely complex. [pause] The question was control flow: could the agent track which step it was on and return to the right caller? With callstack, every level executed and unwound deterministically — a clean run at all seven depths. Without it, the model held up through depth four, then started forgetting where it was, hallucinating or skipping steps. By depth seven, none of the runs finished correctly.

**S5 — With callstack**
> With callstack, each call forks a new frame. The child inherits the parent's full context and adds only the new turns it generates. When a call returns, its frame collapses into a single result and the stack unwinds deterministically — every answer landing back in the frame that called it, which resumes right where it paused. And because each fork shares the parent's token prefix, it reuses the cache — around ninety percent cheaper — so going deep stays inexpensive.

**S5b — Parallel calls**
> Calls don't have to run one at a time. Say "slash call — apply this correction to each file, in parallel." Callstack spawns one fork per file — each already knows the correction to make from the inherited context. Once the work is complete, the parent continues with just the results.

**S6 — Fork or fresh**
> Callstack gives you both modes. Fork, and the child inherits the parent's context for free — unlike a normal subagent, which you hand-feed every detail, burning tokens to regenerate what the parent already knew. Or go fresh — a fully isolated worker for a self-contained task. Either way, calls nest and the context stays compact.

**S7 — Interaction at any depth**
> Any frame can pause and talk to the user — something a normal subagent can't do at all. [pause] Here, "verify MFA" asks for a code. The user enters it, and the agent calls deeper to check — but it's wrong, so it asks again. This time it's valid. The stack unwinds, and the agent continues right where it left off.

**S7b — Why callstack** *(comparison slide before the outro)*
> Subagents are dynamic, but only one level deep — and they can't talk to you. Graph-based agents nest deep, but only along paths you hard-code in advance. Callstack does all three at once: dynamic, arbitrarily deep, and interactive at any level.

**S8 — Install / outro**
> Callstack is open source and free to use. Available now as a Claude Code plugin.
