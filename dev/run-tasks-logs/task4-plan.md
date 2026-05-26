# Task 4 — Consolidate session-on-disk knowledge

## Problem
Knowledge of `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` layout + the
mtime-based "which session is the caller" scan is spread across modules:

- `session.py` — owns the layout (`SessionLocator`, `PROJECTS_DIR`,
  `encode_project_dir`, `_most_recent` private method, JSONL record helpers).
- `frames.py` — has its **own** module-level `_SHARED_LOCATOR` + public-ish
  `_most_recent_session(cwd)` that reaches into `SessionLocator._most_recent`
  (a private method) from another module, and second-guesses
  `session.PROJECTS_DIR` identity to invalidate its cached locator.
- `invocation.py` — imports `_most_recent_session` from `frames` (a layout
  concern leaking through the reporting module).
- `channel.py` — builds `--session-id <uuid>` argv + stamps
  `CALLSTACK_OWN_SESSION`.

The smell: `frames` (a *reporting* module) is the public home of a *session
layout* lookup, and it reaches into `session`'s private method to do it.
`invocation` then depends on `frames` purely to get at session layout.

## channel.py audit — CONCLUSION: leave it
`channel.py` references no `PROJECTS_DIR`, no `.jsonl` path, no
`encode_project_dir`. Its only session-*format* knowledge is `_UUID_RE`, which
it **already imports from `session.py`** (single-sourced — `--session-id` argv
validation and on-disk lookups share one regex). Everything else is pure CLI
flag construction (`--session-id <uuid>`) and env stamping
(`CALLSTACK_OWN_SESSION`). That is subprocess-argv concern, not layout
knowledge. Per the brief ("if it's purely stamping a UUID and CLI flags, leave
it"), no change.

## Design — session.py owns the mtime lookup
Add a public `most_recent_session(cwd: str) -> Optional[str]` to `session.py`,
moving the shared-locator + cache-invalidation dance out of `frames.py`. Now
the function lives in the same module as `SessionLocator`, so its use of
`_most_recent` is ordinary intra-module access, not a cross-module reach-in.

```python
# session.py
_SHARED_LOCATOR: Optional[SessionLocator] = None

def most_recent_session(cwd: str) -> Optional[str]:
    """Session-id stem of the most recently modified .jsonl in cwd's project
    dir, or None. Top-level use only (mtime is unsafe under nested invocations
    — see SessionLocator.locate). Reuses a module-level locator so the MRU
    cache survives across calls; recreated if PROJECTS_DIR is monkeypatched."""
```

## Migration
1. `session.py`: add `most_recent_session` + module-level `_SHARED_LOCATOR`.
2. `frames.py`: delete `_most_recent_session` + its `_SHARED_LOCATOR`; fix the
   module docstring line that referenced it. (No back-compat shim — the only
   importer is `invocation.py`; tests reference the name only in comments.)
3. `invocation.py`: import `most_recent_session` from `session` instead of
   `_most_recent_session` from `frames`. `_ROOT_FRAME_KEY` still comes from
   `frames` (genuinely a reporting concern).
4. `tests/test_session.py`: add direct boundary tests for `most_recent_session`
   (empty dir → None; picks newest by mtime; honors monkeypatched PROJECTS_DIR;
   cache survives + invalidates on dir mtime bump).

## Dependency category
**Local-substitutable** (filesystem; tests build a fake `~/.claude/projects`
tree under `tmp_path` and monkeypatch `session.PROJECTS_DIR`, as existing
test_session.py already does).

## Success criteria
- `most_recent_session` lives in `session.py`; `frames.py` no longer owns any
  session-layout lookup.
- No module reaches into `SessionLocator._most_recent` from outside `session`.
- Clean-env suite ≥346 passing; new boundary tests added.
