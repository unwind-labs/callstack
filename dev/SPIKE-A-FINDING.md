# Spike A — `claude --resume` + stream-json tool_result injection

Status: **FAIL** (the RFC's revive plan as written is not viable).
Outcome: **harvest-on-demand DEFERRED**; tree-wide concurrent-process
cap code removed. See `RFC-harvest-on-demand.md` header.
Date: 2026-05-27
Script: [spike_A_tool_result_inject.py](spike_A_tool_result_inject.py)
Claude CLI version: 2.1.152

## What I tested

The RFC-harvest-on-demand "Re-spawn + inject path" assumes that after
SIGKILL'ing a parent claude that has a dangling MCP `tool_use` in its
transcript, we can:

1. Run `claude --resume <session_id> --input-format stream-json
   --output-format stream-json`
2. Send a single user message containing a `tool_result` keyed to the
   dangling `tool_use_id`
3. Have the resumed claude consume that tool_result and continue
   from where it left off

## What actually happens

**On `--resume`, claude does not preserve the dangling assistant
turn. Instead it re-runs the model from the most recent
user→assistant boundary, producing fresh `tool_use` calls with new
`tool_use_id`s. The injected `tool_result` for the original
`tool_use_id` is silently dropped — that id no longer exists in the
resumed conversation.**

Concrete trace (one of three repro runs):

| step | original session | resumed session |
|---|---|---|
| session id | `dd891e33-…3333` | `7faf0654-…7206` (new id) |
| ToolSearch tool_use | `toolu_01GnP…SMD` | `toolu_01FHe…KrH` (new) |
| blocker tool_use | `toolu_01FZe…hrn` | `toolu_01Rnc…ioz` (new) |
| injected tool_result | `toolu_01FZe…hrn` | ignored — id not in session |

Other findings worth noting:

- **Transcript flush is delayed.** A dangling `tool_use` was NOT in
  `~/.claude/projects/<encoded>/<sid>.jsonl` immediately after SIGKILL
  (the kill raced the on-disk write). A 10 s sleep before kill made it
  reliably present. So even if injection had worked, the revive path
  would need a "wait for transcript flush" preamble — not free.
- **`--resume` mints a new session id under stream-json mode.** I did
  not pass `--fork-session`; the documented `--resume <id>` semantics
  ought to continue in-place, but the new init message reports a
  different `session_id`. This may be a recent CLI change; either way,
  it means the resumed session is not the same on-disk session as the
  original. Subsequent harvests would need to find the *new* session
  in the registry, not the original.

## What this means for the RFC

The plan's "inject `tool_result` via stream-json" is dead. We need a
different revive primitive.

### Revised revive primitive: MCP-side short-circuit

Instead of injecting at the CLI boundary, intercept at the **MCP server
boundary**:

1. Parent claude (re-spawned via `--resume`) re-runs from the last
   `user`→`assistant` boundary. The model decides to call
   `mcp__plugin_callstack_call__call` again with the same task list.
2. The MCP server's `call` handler first checks the invoke registry:
   "does this parent_session_id already have a pending or completed
   invoke?"
3. If yes → return the bundled results immediately, no children
   spawned. The parent claude sees a normal MCP tool_result and
   continues.
4. If no → dispatch children as usual.

This is what the user implicitly proposed in this session
("callstack already manages child returning … why not also check if
all children have returned?"). The registry continues to be the
persistent source of truth; the *trigger* moves from a
stream-json injection to a registry lookup inside the MCP server's
own tool handler.

Costs vs. the original plan:
- **+**  No stream-json wire-format gymnastics. Doesn't depend on CLI
  internals.
- **+**  Determinism doesn't matter — the model can paraphrase its
  prompt, ask for different tasks; the registry is keyed on the
  *session* (the resumed parent), not on the exact /call invocation.
- **−** Parent re-runs at least one model turn before reaching the
  short-circuit. Cost ≈ 1 model turn × parent's prompt size, per
  revive. Acceptable vs. RFC's `~3-5 min added wall time at d=6`.
- **−** Need a careful key for "same /call." Simple approach: most
  recent pending invoke for this parent_session_id. Refinement:
  hash the tasks-list and match. Pick during Phase 4 implementation.

### Concrete plan updates

[dev/PLAN-harvest-on-demand.md](PLAN-harvest-on-demand.md) §3 Phase 4
needs these changes:

- Drop "channel._spawn writes injected tool_result" — there is no
  injection.
- Add "MCP `call` handler checks registry for pending/complete
  invoke under parent_session_id; if found, return bundled results
  immediately."
- Add a registry index by `parent_session_id` (cheap: scan
  `<root>/invokes/*.json`, filter on parent_session_id; a few dozen
  files at most).
- The revive flow becomes:
  1. Last child writes its result to registry (`complete_task`).
  2. Last child checks: is this invoke's parent state == Harvested?
  3. If yes → child detaches a respawner that does `claude --resume
     <parent_session_id>` (no stream-json injection — just resume,
     let the model re-run). Crucially, the respawn must include the
     SAME `--mcp-config` so the parent re-encounters our `call`
     tool, which then short-circuits.
  4. If no → parent's MCP server is still alive; it sees the
     registry complete and returns normally.

The empirical risk that remains: does `claude --resume` reliably
reproduce a re-call of the same MCP tool? In my spike, after
`--resume` claude DID re-emit the same tool calls (ToolSearch and
blocker). High confidence this is the model's natural behavior
since the conversation context still ends with "user asked you to
call X" and the assistant's now-discarded turn isn't visible. But
there's no contract guaranteeing the model will make the exact
same /call — the registry lookup needs to tolerate paraphrased
tasks.

## Spike A2 — does `--resume` re-emit the dangling tool_use?

Followup spike: maybe Spike A's "claude re-ran the conversation" was
re-rendering of the existing transcript (new session_id → new
tool_use_ids), not fresh model calls. If so, we could capture the
re-emitted tool_use_id and inject a tool_result for it.

Script: [spike_A2_capture_then_inject.py](spike_A2_capture_then_inject.py)

Verdict: **also FAIL**. Two things confirmed:

1. **`claude --resume` is inert without a user message.** After the
   init handshake + `SessionStart:resume` hook, the process sits idle
   and emits nothing. No re-rendering of the transcript, no fresh
   tool_use, nothing.
2. (From Spike A) **Once a user message is sent, claude treats it as a
   new conversational turn and the dangling tool_use is dropped** —
   the model re-runs from the last user→assistant boundary and emits
   fresh tool calls.

There is no "capture the re-emitted id" path. The dangling tool_use
state is essentially garbage-collected the moment we resume.

## Confirmed revive primitive: registry short-circuit + replay nudge

The protocol that's left:

1. Last child writes its result to the registry (`complete_task`).
2. If the parent state is `Harvested`, the last child's MCP server
   double-forks a respawner.
3. Respawner: `claude --resume <parent_session_id> --mcp-config <same>`.
4. Respawner sends a single user message — call it the **replay
   nudge** — that tells the resumed parent to continue its work
   ("Your previous tool call was interrupted; please retry it.").
5. Resumed parent re-runs, re-emits the MCP `call` tool_use with
   the same task list (high confidence: the model has full
   conversation context except the dangling assistant turn).
6. Our MCP `call` handler checks the registry: is there a
   pending/completed invoke for this parent_session_id whose
   tasks-content matches? Yes → return bundled results immediately.
   No → dispatch new children.

Risks of the replay-nudge approach:
- **Model might paraphrase tasks.** Mitigation: key the registry
  lookup on parent_session_id alone (most recent pending invoke);
  fall back to tasks-content hash only as a tiebreaker.
- **Model might do extra tool calls first** (Spike A showed ToolSearch
  before the real call). Mitigation: short-circuit only triggers
  when the model emits the right MCP tool name; other tool calls
  pass through normally.
- **Model might decide the work is already done** based on its
  conversation context. Mitigation: the nudge message explicitly
  says "your previous tool call was interrupted; retry it now."
- **Resume mints a NEW session_id under stream-json.** The registry
  needs to handle both: the original session id (used for the
  pending invoke entry) and the new session id (the actually-running
  resumed parent). Bookkeeping: add a `resumed_session_ids: [...]`
  field to the registry entry; the MCP `call` handler matches on
  either.

## Spike A3 — promptId / envelope-shape variants

Script: [spike_A3_promptid_variations.py](spike_A3_promptid_variations.py)

Four variants tried against the same dangling-parent transcript:

| Variant | Shape | Verdict |
|---|---|---|
| V0 control | `type=user` + `tool_result` block (Spike A repro) | RECALL |
| V1 promptId | V0 + `promptId: <original>` set to the dangling cycle's id | RECALL |
| V2 top-level | `type=tool_result` envelope (not wrapped in user) | RECALL |
| V3 parent_tool_use_id | V0 + `parent_tool_use_id: <dangling id>` | RECALL |

"RECALL" = resumed claude emitted fresh `ToolSearch` + fresh
`mcp__blocker__block_then_ok` `tool_use`s, ignoring the injection
payload entirely. In every case the model produced the exact same
two-tool-call pattern.

**Conclusion**: `claude --resume` trims the dangling assistant turn
from the in-memory conversation state before passing it to the model.
The transcript file on disk still contains line 10 (the dangling
`tool_use`), but the model doesn't see it. No stream-json input shape
will recover that state — it would require a CLI change or flag.

## Decision

Registry short-circuit + replay nudge is the only viable path.
Proceeding with Phase 4 on that basis (confirmed by amolk).
