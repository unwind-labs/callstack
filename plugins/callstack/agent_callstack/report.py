"""The merged invocation report — one public boundary over a cluster.

`InvocationReport` is the single interface for everything an invocation does
with its on-disk artifacts (`_frames/*.yaml`, `report.yaml`, `progress.log`):
where they live, how frames are written, loaded, merged, reconciled, and how
the run is sealed at the end.

It is a *deep* facade: a handful of methods hiding a large, security- and
concurrency-hardened implementation that physically lives in three internal
modules —

    invocation_ctx.py   the path/identity value object (`_InvocationContext`)
    frames.py           frame load + orphan reconciliation + merge/graft
    reporter.py         the live writer (`_LiveReporter`), atomic writes,
                        the cross-process merge lock, boundary finalize

Callers and tests import only this module. The three internals keep their
existing names so they can still be unit-tested in isolation where an
optimization (cache, hash-skip) has no honest behavioral expression, but the
*structural* shape of "make a report, drive it, read the merged doc" now lives
behind one type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from .env import read_finalize_wait_seconds
from .frames import (
    _ROOT_FRAME_KEY,
    _build_merged_report,
    _load_frames,
)
from .invocation_ctx import _InvocationContext, _utc_now_iso
from .reporter import (
    _atomic_yaml_write,
    _finalize_own_frames,
    _LiveReporter,
)
from .terminal_wait import wait_for_terminal_signals

# Public spelling of the root frame sentinel. The leading-underscore alias
# stays in `frames` for the internals; callers use this.
ROOT_FRAME_KEY = _ROOT_FRAME_KEY


class InvocationReport:
    """The on-disk merged report for a single invocation.

    Construct one from the invocation's identity (where its artifacts go and
    which caller node its frame grafts under), then use it to obtain the live
    reporter, seal the run, or read/merge the frames.

        report = InvocationReport(invoke_id=id, log_dir=dir, cwd=cwd)
        reporter = report.reporter(kind="call", tasks=tasks, started_at=ts)
        driver.on_progress = reporter
        tree = driver.run(...)
        report.seal(reporter, tree)          # wait for terminal signals + finalize
        doc = report.merged_document(ended_at=ts)   # read it back
    """

    def __init__(
        self,
        *,
        invoke_id: str = "",
        log_dir: Path = Path("."),
        cwd: str = "",
        frame_key: str = ROOT_FRAME_KEY,
        is_nested: bool = False,
        instance_id: str = "",
        ctx: Optional[_InvocationContext] = None,
    ):
        # Single construction path so any future init state runs for both
        # the component form and the `from_context` form (which would
        # silently skip __init__ under the old __new__ idiom). When `ctx`
        # is supplied it is authoritative; otherwise build one from parts.
        self._ctx = (
            ctx
            if ctx is not None
            else _InvocationContext(
                invoke_id=invoke_id,
                log_dir=Path(log_dir),
                cwd=cwd,
                frame_key=frame_key,
                is_nested=is_nested,
                instance_id=instance_id,
            )
        )

    @classmethod
    def from_context(cls, ctx: _InvocationContext) -> "InvocationReport":
        """Wrap an already-resolved `_InvocationContext`. Used by callers that
        resolve nested-vs-root identity separately (e.g. the Caller's
        invocation-context resolution) and then want the report boundary."""
        return cls(ctx=ctx)

    # ---- identity / paths ----

    @property
    def context(self) -> _InvocationContext:
        """Escape hatch for components that need the raw path/identity value
        (the Driver's `TraceWriter` destination, the trace store). Prefer the
        named properties below; this exists so wiring code that predates the
        facade doesn't have to be rewritten in one step."""
        return self._ctx

    @property
    def invoke_id(self) -> str:
        return self._ctx.invoke_id

    @property
    def cwd(self) -> str:
        return self._ctx.cwd

    @property
    def frame_key(self) -> str:
        return self._ctx.frame_key

    @property
    def is_nested(self) -> bool:
        return self._ctx.is_nested

    @property
    def invocation_dir(self) -> Path:
        return self._ctx.invocation_dir

    @property
    def frames_dir(self) -> Path:
        return self._ctx.frames_dir

    @property
    def report_path(self) -> Path:
        return self._ctx.report_path

    @property
    def log_path(self) -> Path:
        return self._ctx.log_path

    def frame_path(self, key: Optional[str] = None) -> Path:
        return self._ctx.frame_path(key)

    def prefix(self, kind: str) -> str:
        return self._ctx.prefix(kind)

    # ---- live writing ----

    def reporter(self, *, kind: str, tasks: Sequence[str], started_at: str) -> _LiveReporter:
        """Return this invocation's `Driver.on_progress` callback. Each tick
        writes a per-frame snapshot and coalesces the merged-report rewrite."""
        return _LiveReporter(
            ctx=self._ctx,
            kind=kind,
            tasks=list(tasks),
            started_at=started_at,
        )

    def seal(self, reporter: _LiveReporter, tree, *, finalize_wait_seconds: Optional[float] = None) -> None:
        """End-of-run finalize. Gives late `op:return`/`op:yield` envelopes a
        chance to land (so a node that just missed the window becomes
        `Timeout` instead of being sealed as still-running), then forces the
        reporter's synchronous final merge.

        This is the wait+finalize glue that used to be duplicated inside
        `Caller._invoke` and `Caller.resume`."""
        budget = read_finalize_wait_seconds() if finalize_wait_seconds is None else finalize_wait_seconds
        wait_for_terminal_signals(tree, wait_budget_seconds=budget)
        reporter.finalize(tree)

    # ---- reading / merging ----

    def load_frames(self) -> dict[str, list[dict]]:
        """Every frame under `_frames/`, grouped by frame_key, with dead-writer
        orphan reconciliation already applied. The returned structure is owned
        by the caller and safe to mutate."""
        return _load_frames(self._ctx.frames_dir)

    def merged_document(self, *, ended_at: Optional[str] = None) -> Optional[dict]:
        """Build the merged `report.yaml` document from the current frames, or
        `None` when no root frame has landed yet (a nested writer raced ahead
        of the root). Pure read — does not write."""
        ts = _utc_now_iso() if ended_at is None else ended_at
        frames = self.load_frames()
        root_frames = frames.get(ROOT_FRAME_KEY)
        if not root_frames:
            return None
        return _build_merged_report(
            invoke_id=self._ctx.invoke_id,
            frames=frames,
            root_frame=root_frames[0],
            ended_at=ts,
        )

    def write_frame(self, frame: dict, *, key: Optional[str] = None) -> Path:
        """Atomically write a raw frame dict to `_frames/{key}.yaml` (defaults
        to this report's `frame_key`). For synthesizing fixtures and for the
        one-shot report writer; the live path uses `reporter()`."""
        self._ctx.frames_dir.mkdir(parents=True, exist_ok=True)
        path = self._ctx.frame_path(key)
        _atomic_yaml_write(path, frame)
        return path

    def write_report(self, *, ended_at: Optional[str] = None) -> Optional[Path]:
        """Build the merged document and atomically write it to `report.yaml`.
        Returns the path, or `None` if there's no root frame to merge yet."""
        doc = self.merged_document(ended_at=ended_at)
        if doc is None:
            return None
        self._ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
        _atomic_yaml_write(self._ctx.report_path, doc)
        return self._ctx.report_path

    # ---- boundary finalize (MCP) ----

    def finalize_own_frames(self, *, reason: str) -> bool:
        """At a process/tool boundary, force-terminate any non-terminal nodes
        in frames written by *this* process before results cross the boundary,
        so a frame this process owns never surfaces a pinned `awaiting_*` dot.
        Frames owned by other processes are left untouched. Returns True iff at
        least one frame was rewritten."""
        return _finalize_own_frames(
            self._ctx.log_dir,
            self._ctx.invoke_id,
            reason=reason,
        )
