# Explainer narration

This file is the **single source of truth** for the voiceover. `scripts/gen-voiceover.ts`
parses each `**S<id> — ...**` header and the `> ...` line below it, then synthesizes
`public/vo/<id>.mp3` via xAI TTS. Edit the text, then run `bun run scripts/gen-voiceover.ts`.

**S1 — Problem**
> <emphasis>Complex enterprise workflows break agents. </emphasis> Deeply nested tasks overflow linear context of the agent. [pause] The context rots, detail gets compacted away, and the agent loses the thread.

**S2 — Solution**
> The Callstack plugin gives your agents a real call stack! One command — slash call — forks the session, runs a task, and returns a compact result. Calls can nest arbitrarily deep and yet remain reliable, so you can define complex workflows for your agent to follow.

**S3 — Results**
> We tested agents on workflows from one to seven levels deep. With callstack, agents succeeded at all levels. Without it, accuracy fell apart past depth four, and hit zero by depth seven! - It shows that the agent harness needs to <emphasis>help</emphasis> the LLM keep track of things.

**S4 — Without callstack**
> Without callstack, every task piles into the  <emphasis>same</emphasis> agent context. As the session grows and subtasks nest deeply, early context scrolls out of reach, and the agent can no longer tell where it is. At the same time, because the session gets longer and longer, every interaction becomes more and more expensive.

**S5 — With callstack**
> With callstack, each call forks a new frame. The child inherits the full context and adds only the new turns it generates. Calls nest as deep as the work needs. And when a call returns, its frame collapses into a single result, and the stack unwinds deterministically — every answer landing back in the exact caller that asked for it. The context remains compact and that means fewer tokens to pay for.

**S6 — Fork or fresh**
> For tasks that need the parent's context, your agent can fork a session, or go fresh for an isolated task. Either way, the calls can nest and context remains compact.

**S7 — Interaction at any depth**
> Any frame can pause and talk to the user. [pause]Here, verify MFA asks for a code. The user enters it, and the agent calls deeper to check the code. [pause] but the code is wrong! so the agent asks again. [pause] This time the code is valid. Now the stack unwinds, and the agent continues with its task.

**S7b — Why callstack** *(comparison slide before the outro)*
> dynamically orchestrated workflows, arbitrarily deep nesting, interactive from any level, and reliable execution. Using subagents or using fixed graph based agents cannot give you all of these at the same time. Callstack can!

**S8 — Install / outro**
> Callstack is open source and free to use. Available now for Claude Code.
