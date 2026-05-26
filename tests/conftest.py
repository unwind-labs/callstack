"""Suite-wide test fixtures.

Hermetic env isolation
----------------------
callstack stamps a handful of ``CALLSTACK_*`` env vars (and reads Claude
Code's ``CLAUDE_CODE_SESSION_ID``) into every child process it spawns.
When this very test suite is run *inside* a callstack ``/call`` fork — e.g.
by the ``run-tasks`` workflow — those vars are present in the test
process's environment and silently poison tests that assume a pristine
top-level shell. Concretely they cause false failures such as:

  * ``CALLSTACK_ROOT_INVOKE_ID`` trips ``SessionLocator.locate``'s
    nested-invocation guard, so the mtime-fallback tests raise instead of
    resolving a session.
  * ``CALLSTACK_MAX_DEPTH`` overrides the default and breaks tests that
    assert the built-in default depth.

The behavior under test is identical either way; only the inherited env
differs. We clear it once, autouse, so the suite is hermetic regardless of
where it runs. monkeypatch restores the real environment after each test.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_callstack_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CALLSTACK_") or name == "CLAUDE_CODE_SESSION_ID":
            monkeypatch.delenv(name, raising=False)
