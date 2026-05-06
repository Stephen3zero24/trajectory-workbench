"""Unit tests for search2qa.scene_handler.install_dependencies (T1).

Covers the three import-check branches:
1. all deps importable in sandbox  → no exception, emits "skip pip install"
2. deps missing + env unset        → RuntimeError with missing-package summary
3. deps missing + env == "1"       → fallback runtime pip install path runs

The sandbox object is mocked: only ``files.write_file`` and ``commands.run``
are exercised. Each ``commands.run`` call returns a fake result whose
``logs.stdout`` / ``logs.stderr`` are lists of objects with a ``.text`` attr,
matching the OpenSandbox SDK shape used elsewhere in scene_handler.py.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Iterable
from unittest.mock import AsyncMock

import pytest

from search2qa import scene_handler


# ─── helpers ──────────────────────────────────────────────────────────────


def _line(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _result(stdout_lines: Iterable[str] = (), stderr_lines: Iterable[str] = (), error: str | None = None):
    return SimpleNamespace(
        logs=SimpleNamespace(
            stdout=[_line(l) for l in stdout_lines],
            stderr=[_line(l) for l in stderr_lines],
        ),
        error=error,
    )


def _all_present_payload() -> str:
    return "IMPORT_CHECK_RESULT:" + json.dumps([])


def _missing_payload(pkgs: list[tuple[str, str, str]]) -> str:
    return "IMPORT_CHECK_RESULT:" + json.dumps(
        [{"pkg": p, "mod": m, "err": e} for (p, m, e) in pkgs]
    )


def _make_sandbox(*run_results):
    """Return a mock sandbox whose commands.run yields the given results in order."""
    sandbox = SimpleNamespace()
    sandbox.files = SimpleNamespace(write_file=AsyncMock(return_value=None))
    sandbox.commands = SimpleNamespace(run=AsyncMock(side_effect=list(run_results)))
    return sandbox


def _emits():
    """Return (emit_fn, recorder list)."""
    captured: list[tuple[str, str]] = []

    def emit(event_type: str, message: str) -> None:
        captured.append((event_type, message))

    return emit, captured


# ─── happy path: all deps already installed in sandbox image ──────────────


@pytest.mark.asyncio
async def test_install_dependencies_all_present_skips_pip(monkeypatch):
    monkeypatch.delenv("SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL", raising=False)

    sandbox = _make_sandbox(_result(stdout_lines=[_all_present_payload()]))
    emit, captured = _emits()

    await scene_handler.install_dependencies(sandbox, emit)

    # exactly one commands.run call (the import check); no pip install groups
    assert sandbox.commands.run.await_count == 1
    sandbox.files.write_file.assert_awaited_once()
    assert any("skip pip install" in msg for _, msg in captured)


# ─── strict default: missing deps without fallback env → raise ────────────


@pytest.mark.asyncio
async def test_install_dependencies_missing_default_raises(monkeypatch):
    monkeypatch.delenv("SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL", raising=False)

    missing = [("crawl4ai", "crawl4ai", "No module named 'crawl4ai'")]
    sandbox = _make_sandbox(_result(stdout_lines=[_missing_payload(missing)]))
    emit, captured = _emits()

    with pytest.raises(RuntimeError) as excinfo:
        await scene_handler.install_dependencies(sandbox, emit)

    err_msg = str(excinfo.value)
    assert "crawl4ai" in err_msg
    assert "SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL" in err_msg
    assert scene_handler.SEARCH2QA_SANDBOX_IMAGE in err_msg
    # only one commands.run (the check); no fallback pip install groups ran
    assert sandbox.commands.run.await_count == 1
    # an install_error event must have been emitted
    assert any(evt == "install_error" for evt, _ in captured)


# ─── fallback path: missing deps + opt-in env → runtime pip install ───────


@pytest.mark.asyncio
async def test_install_dependencies_missing_with_fallback_runs_pip(monkeypatch):
    monkeypatch.setenv("SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL", "1")

    missing = [
        ("ddgs", "ddgs", "No module named 'ddgs'"),
        ("PyMuPDF", "fitz", "No module named 'fitz'"),
    ]
    # 1 import check + 6 pip-install groups = 7 commands.run results
    sandbox = _make_sandbox(
        _result(stdout_lines=[_missing_payload(missing)]),
        *[_result() for _ in range(6)],
    )
    emit, captured = _emits()

    await scene_handler.install_dependencies(sandbox, emit)

    assert sandbox.commands.run.await_count == 7
    # check that the legacy-path notice was emitted
    assert any(
        "falling back" in msg.lower() and "legacy" in msg.lower() for _, msg in captured
    )
    # collect the actual pip install commands invoked
    pip_cmds = [
        call.args[0]
        for call in sandbox.commands.run.await_args_list
        if call.args and "pip install" in call.args[0]
    ]
    assert len(pip_cmds) == 6, f"expected 6 pip install groups, saw {pip_cmds}"
    # ensurepip must NOT appear anywhere — that was the buggy step we removed
    assert not any("ensurepip" in c for c in pip_cmds)
    # version constraints must match sandbox-requirements.txt ranges
    joined = " ".join(pip_cmds)
    assert "ddgs>=9.0,<10" in joined
    assert "PyMuPDF>=1.23,<2" in joined


# ─── degenerate case: import-check produced no parseable result ───────────


@pytest.mark.asyncio
async def test_install_dependencies_no_marker_raises(monkeypatch):
    monkeypatch.delenv("SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL", raising=False)

    sandbox = _make_sandbox(
        _result(stdout_lines=["random noise"], stderr_lines=["python interpreter not found"]),
    )
    emit, _captured = _emits()

    with pytest.raises(RuntimeError) as excinfo:
        await scene_handler.install_dependencies(sandbox, emit)

    assert re.search(r"import-check failed", str(excinfo.value))
    assert scene_handler.SEARCH2QA_SANDBOX_IMAGE in str(excinfo.value)
