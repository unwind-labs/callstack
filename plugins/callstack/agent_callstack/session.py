"""Session discovery and resolution for Claude Code's project layout.

Claude Code stores each session as `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`
where `<encoded-cwd>` is the project's working directory with `/` replaced by
`-`. This module hides that layout behind a single `SessionLocator` class.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Env vars that may contain the parent session, in priority order.
_ENV_PARENT_PATH = "CALLSTACK_PARENT_SESSION"   # absolute file path
_ENV_PARENT_UUID = "CLAUDE_SESSION_ID"           # UUID set by Claude Code


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

    def __init__(self, projects_dir: Path = PROJECTS_DIR):
        self._projects_dir = projects_dir

    def locate(
        self,
        explicit: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> SessionRef:
        """Discover the active session.

        Priority: explicit (UUID or file path) → env vars → mtime heuristic.
        Raises RuntimeError if nothing is found.
        """
        if explicit:
            return self._from_explicit(explicit, cwd)

        for env_var in (_ENV_PARENT_PATH, _ENV_PARENT_UUID):
            value = os.environ.get(env_var)
            if not value:
                continue
            # SEC-002: when the env supplies an absolute file path, require
            # it to resolve under PROJECTS_DIR. An attacker who controls the
            # environment (CI leak, shared box) could otherwise point us at
            # arbitrary readable files, whose recorded `cwd` then propagates
            # into subprocess.Popen(cwd=...). UUID-form values fall through
            # to `resolve()` which already scans only PROJECTS_DIR.
            if env_var == _ENV_PARENT_PATH and not self._env_path_under_projects(value):
                print(
                    f"[callstack] {env_var} rejected: not under "
                    f"{self._projects_dir}",
                    file=sys.stderr,
                )
                continue
            ref = self._from_value(value, cwd)
            if ref:
                print(f"[callstack] Found session via {env_var}", file=sys.stderr)
                return ref

        print("[callstack] Falling back to mtime heuristic", file=sys.stderr)
        ref = self._most_recent(cwd)
        if ref:
            return ref

        raise RuntimeError(
            f"Could not discover active Claude Code session. Searched {self._projects_dir}.\n"
            "Pass an explicit session_id or set CALLSTACK_PARENT_SESSION."
        )

    def resolve(self, session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
        """Find the .jsonl file for a known session UUID. Returns None if missing."""
        # Look in the cwd-matching project dir first, then any project dir.
        project_dir = self._project_dir_for(cwd)
        if project_dir:
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
        if self._projects_dir.is_dir():
            for d in self._projects_dir.iterdir():
                if d.is_dir():
                    candidate = d / f"{session_id}.jsonl"
                    if candidate.is_file():
                        return candidate
        return None

    # ---- private ----

    def _from_explicit(self, value: str, cwd: Optional[str]) -> SessionRef:
        ref = self._from_value(value, cwd)
        if ref:
            return ref
        raise RuntimeError(
            f"Explicit session '{value}' not found in {self._projects_dir}"
        )

    def _env_path_under_projects(self, value: str) -> bool:
        """True iff `value` is a file path that resolves under PROJECTS_DIR.

        Non-path values (e.g. bare UUIDs) return True — they don't escape
        the projects dir on their own (`resolve()` only scans inside it)."""
        p = Path(value)
        if not p.is_absolute() and "/" not in value:
            return True  # bare UUID-like string, no path to validate
        try:
            resolved = p.resolve(strict=True)
            projects = self._projects_dir.resolve()
        except (OSError, RuntimeError):
            return False
        try:
            return resolved.is_relative_to(projects)
        except AttributeError:  # Python < 3.9 fallback (unused here, defensive)
            return str(resolved).startswith(str(projects) + os.sep)

    def _from_value(self, value: str, cwd: Optional[str]) -> Optional[SessionRef]:
        """Accept either a file path or a UUID."""
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
        parent_session in the resulting report."""
        primary = self._project_dir_for(cwd)
        if not primary:
            return None
        best: Optional[Path] = None
        best_mtime: float = 0.0
        for f in primary.glob("*.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best_mtime, best = m, f
        if best is None:
            return None
        return SessionRef(session_id=best.stem, file=best)


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


def count_lines(path: Path) -> int:
    """Cheap line counter; returns 0 on read error."""
    try:
        with open(path, "r") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0
