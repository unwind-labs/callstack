"""Session discovery and resolution for Claude Code's project layout.

Claude Code stores each session as `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`
where `<encoded-cwd>` is the project's working directory with `/` replaced by
`-`. This module hides that layout behind a single `SessionLocator` class.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Shape validation for session ids before any filesystem probe (SEC-003).
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Env vars that may identify the current session, in priority order.
#
# CALLSTACK_OWN_SESSION is stamped by `ClaudeChannel._spawn()` itself,
# alongside `--session-id <uuid>` on the child's claude argv, so the
# spawned process's MCP server reads back exactly the UUID claude was
# told to use — deterministic and unaffected by what claude does or
# doesn't propagate into its own MCP children's env.
#
# CLAUDE_CODE_SESSION_ID is what Claude Code exports per process. We
# read it as a fallback for processes the plugin didn't spawn (chiefly
# the user's top-level interactive `claude` session, where the user
# launched claude themselves and CALLSTACK_OWN_SESSION can't have been
# set by us).
_ENV_OWN_SESSION = "CALLSTACK_OWN_SESSION"       # UUID stamped by callstack at spawn
_ENV_PARENT_UUID = "CLAUDE_CODE_SESSION_ID"      # UUID set by Claude Code per process
# When set, we're running inside a callstack-managed nested invocation.
# Used to decide whether the mtime fallback is safe: under concurrent
# sibling /calls the project dir's most-recently-touched .jsonl races,
# so we MUST refuse it rather than silently produce a wrong fork.
_ENV_ROOT_INVOKE_ID = "CALLSTACK_ROOT_INVOKE_ID"


def encode_project_dir(cwd: str) -> str:
    """Mirror Claude Code's encoding of a working directory into its
    `~/.claude/projects/<slug>/` dir name. Both `/` and `_` become `-`."""
    return cwd.replace("/", "-").replace("_", "-")


@dataclass(frozen=True)
class SessionRef:
    """A reference to a Claude Code session on disk."""
    session_id: str
    file: Path

    @property
    def cwd(self) -> Optional[str]:
        """Best-effort: extract the cwd recorded in the session's first message
        that has one. Returns None if not derivable."""
        return _extract_cwd(self.file)


class SessionLocator:
    """Find session files on disk. Three strategies, tried in order."""

    def __init__(self, projects_dir: Optional[Path] = None):
        # Read the module global at construction time, not as a default arg.
        # Python binds default args at function-definition time, so a
        # monkeypatch of session.PROJECTS_DIR (used in tests for an isolated
        # fake project tree) would otherwise never reach this locator.
        self._projects_dir = projects_dir if projects_dir is not None else PROJECTS_DIR
        # Per-instance cache for _most_recent. Key: cwd string (or "" for
        # None). Value: (project_dir_mtime_ns, SessionRef-or-None). Stale
        # when project_dir_mtime advances (new/removed session JSONL).
        self._mru_cache: dict[str, tuple[int, Optional[SessionRef]]] = {}

    def locate(
        self,
        explicit: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> SessionRef:
        """Discover the active session.

        Priority: explicit → CALLSTACK_OWN_SESSION (callstack-stamped) →
        CLAUDE_CODE_SESSION_ID (Claude Code per-process) → mtime heuristic.

        The mtime heuristic is REFUSED when we're running inside a
        callstack-managed nested invocation (CALLSTACK_ROOT_INVOKE_ID is
        set): under concurrent sibling /calls the project dir's
        most-recently-touched .jsonl races, so silently picking it
        produces cross-forks (each sibling resolves a different sibling
        as its parent). Mtime stays available for top-level use only,
        where the cwd-matching dir has at most the user's one active
        session.

        Raises RuntimeError if nothing is found.
        """
        if explicit:
            return self._from_explicit(explicit, cwd)

        # The legacy CALLSTACK_PARENT_SESSION env (an inherited path that
        # got "stuck" pointing at the root through arbitrary nesting depth)
        # was removed: it was a known cross-fork trigger and is no longer
        # propagated by `__init__.py:_driver_for`. Reading it back would
        # only resurrect that bug; we drop it from the priority chain
        # entirely.
        for env_var in (_ENV_OWN_SESSION, _ENV_PARENT_UUID):
            value = os.environ.get(env_var)
            if not value:
                continue
            ref = self._from_value(value, cwd)
            if ref:
                print(f"[callstack] Found session via {env_var}", file=sys.stderr)
                return ref

        # Nested-invocation mtime guard. Without this, three sibling
        # /calls running concurrently each fall through to mtime and
        # race on whichever sibling's jsonl was most-recently touched at
        # millisecond resolution — producing cross-forks like the
        # interior_1 → interior_2's-session bug. Fail loud so the
        # contract breach is visible at first /call instead of cascading
        # into a malformed deeper tree.
        if os.environ.get(_ENV_ROOT_INVOKE_ID):
            raise RuntimeError(
                "callstack: cannot identify own session inside a nested "
                "invocation. Neither "
                f"{_ENV_OWN_SESSION} (stamped by the spawning parent) "
                f"nor {_ENV_PARENT_UUID} (Claude Code per-process) was "
                "set. The mtime fallback is unsafe under concurrent "
                "sibling /calls — refusing to guess. This usually means "
                "the spawning parent is running an older callstack "
                "build; rebuild the plugin or check the spawn env."
            )

        print("[callstack] Falling back to mtime heuristic", file=sys.stderr)
        ref = self._most_recent(cwd)
        if ref:
            return ref

        raise RuntimeError(
            f"Could not discover active Claude Code session. Searched {self._projects_dir}.\n"
            f"Set {_ENV_PARENT_UUID} or pass an explicit session_id."
        )

    def resolve(self, session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
        """Find the .jsonl file for a known session UUID. Returns None if missing.

        Strategy (in order):
          1. cwd-matching project dir.
          2. Lazy index at ``PROJECTS_DIR/.session_index.json``
             (``{session_id: project_dir_name}``). Verified before returning.
          3. Full project-dir scan, populating the index with every
             ``session_id`` discovered before returning. The index is
             persisted atomically. Skipped when `cwd` is provided — a
             caller-scoped lookup must not probe other projects.

        SEC-003: session_id is shape-validated as a UUID before any
        filesystem probe; malformed input returns None immediately.
        """
        if not _UUID_RE.fullmatch(session_id):
            return None
        project_dir = self._project_dir_for(cwd)
        if project_dir:
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
        if not self._projects_dir.is_dir():
            return None
        # Index lookup
        idx = _load_session_index(self._projects_dir)
        recorded = idx.get(session_id)
        if recorded:
            cand = self._projects_dir / recorded / f"{session_id}.jsonl"
            if cand.is_file():
                return cand
        # Caller-scoped lookup: don't cross project boundaries via full scan.
        if cwd is not None:
            return None
        # Fallback scan; populate index with everything we see.
        found: Optional[Path] = None
        discovered: dict[str, str] = {}
        for d in self._projects_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        if not entry.name.endswith(".jsonl"):
                            continue
                        sid = entry.name[:-len(".jsonl")]
                        discovered[sid] = d.name
                        if sid == session_id and found is None:
                            found = Path(entry.path)
            except OSError:
                continue
        if discovered:
            _save_session_index(self._projects_dir, {**idx, **discovered})
        return found

    # ---- private ----

    def _from_explicit(self, value: str, cwd: Optional[str]) -> SessionRef:
        ref = self._from_value(value, cwd)
        if ref:
            return ref
        raise RuntimeError(
            f"Explicit session '{value}' not found in {self._projects_dir}"
        )

    def _from_value(self, value: str, cwd: Optional[str]) -> Optional[SessionRef]:
        """Accept a UUID. Path-form values are no longer supported via env
        (the legacy `CALLSTACK_PARENT_SESSION` path env was removed); a
        UUID-or-path is still accepted from the `explicit=` caller arg,
        since that path enters under the caller's authority, not via an
        inherited env that could be stale or hostile."""
        p = Path(value)
        if p.is_file():
            return SessionRef(session_id=p.stem, file=p)
        found = self.resolve(value, cwd)
        if found:
            return SessionRef(session_id=value, file=found)
        return None

    def _project_dir_for(self, cwd: Optional[str]) -> Optional[Path]:
        """Project directory for `cwd` (or current process cwd if None)."""
        target = cwd or os.getcwd()
        encoded = encode_project_dir(target)
        candidate = self._projects_dir / encoded
        if candidate.is_dir():
            return candidate
        if self._projects_dir.is_dir():
            for d in self._projects_dir.iterdir():
                if d.is_dir() and d.name == encoded:
                    return d
        return None

    def _most_recent(self, cwd: Optional[str]) -> Optional[SessionRef]:
        """Most recently modified .jsonl in the cwd-matching project dir.

        Scoped to one project: cross-project guessing is unsafe — the
        most-recently-touched session in some unrelated project dir is
        never "the caller", and conflating them produces a wrong
        parent_session in the resulting report.

        Uses os.scandir (one syscall per entry, vs glob+stat) and an
        instance-level cache invalidated when the project_dir's mtime
        advances (a new session was added or removed)."""
        primary = self._project_dir_for(cwd)
        if not primary:
            return None
        key = cwd or ""
        try:
            dir_mtime_ns = primary.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._mru_cache.get(key)
        if cached is not None and cached[0] == dir_mtime_ns:
            return cached[1]
        best_path: Optional[str] = None
        best_mtime_ns: int = 0
        try:
            with os.scandir(primary) as it:
                for entry in it:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if st.st_mtime_ns > best_mtime_ns:
                        best_mtime_ns = st.st_mtime_ns
                        best_path = entry.path
        except OSError:
            return None
        if best_path is None:
            self._mru_cache[key] = (dir_mtime_ns, None)
            return None
        ref = SessionRef(session_id=Path(best_path).stem, file=Path(best_path))
        self._mru_cache[key] = (dir_mtime_ns, ref)
        return ref


def _extract_cwd(session_file: Path) -> Optional[str]:
    """Read the cwd from the first message in a session JSONL that has one."""
    try:
        with open(session_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd and os.path.isdir(cwd):
                    return cwd
    except OSError:
        pass
    return None


_SESSION_INDEX_FILENAME = ".session_index.json"


def _load_session_index(projects_dir: Path) -> dict[str, str]:
    """Best-effort load of the lazy session-id → project-dir-name index.
    Returns empty dict on any error (corrupt, missing, perm denied).

    TODO: index grows monotonically. Add a maintenance pass when it
    exceeds some cap (e.g. 100 KB) to prune session_ids whose .jsonl
    no longer exists. Skipped for now — corrupt entries are detected
    at lookup time via the `is_file()` check, so staleness is a
    correctness no-op."""
    path = projects_dir / _SESSION_INDEX_FILENAME
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)}


def _save_session_index(projects_dir: Path, idx: dict[str, str]) -> None:
    """Atomic write of the session index. Silently best-effort on error."""
    import tempfile
    path = projects_dir / _SESSION_INDEX_FILENAME
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(projects_dir), prefix=path.name + ".", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(idx, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try: os.unlink(tmp_name)
            except OSError: pass
            raise
    except OSError:
        return


def count_lines(path: Path) -> int:
    """Cheap line counter; returns 0 on read error.

    Binary chunked read — 10–50× faster than text-mode iteration on
    multi-MB session JSONLs.
    """
    try:
        total = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                total += chunk.count(b"\n")
        return total
    except OSError:
        return 0
