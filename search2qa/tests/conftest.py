"""Test-only shims so scene_handler can be imported without the full
runtime stack (httpx / opensandbox / sandbox_utils).

These tests target the pure-logic branches inside install_dependencies and
do not exercise any real HTTP or sandbox SDK behaviour, so we register
lightweight stand-in modules in sys.modules before scene_handler is
imported by the test file.
"""

from __future__ import annotations

import sys
import types
from datetime import timedelta as _timedelta


def _install_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# httpx — only AsyncClient is referenced at module top level (async with) and
# is not invoked during install_dependencies tests, so a no-op class is fine.
class _DummyAsyncClient:
    def __init__(self, *_, **__):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


_install_stub("httpx", AsyncClient=_DummyAsyncClient)


# opensandbox.* — stub the three submodules referenced by scene_handler.
_opensandbox = _install_stub("opensandbox")
_install_stub("opensandbox.sandbox", Sandbox=type("Sandbox", (), {}))


class _RunCommandOpts:
    def __init__(self, timeout: _timedelta | None = None):
        self.timeout = timeout


_install_stub("opensandbox.models", WriteEntry=type("WriteEntry", (), {}))
_install_stub("opensandbox.models.execd", RunCommandOpts=_RunCommandOpts)


class _ConnectionConfig:
    def __init__(self, *_, **__):
        pass


_install_stub("opensandbox.config", ConnectionConfig=_ConnectionConfig)


# sandbox_utils — co-located helper module at repo root; only
# _parse_sandbox_endpoint is imported and not invoked by the tests.
_install_stub(
    "sandbox_utils",
    _parse_sandbox_endpoint=lambda url: ("localhost", "http"),
)
