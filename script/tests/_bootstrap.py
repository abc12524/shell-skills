"""测试引导：注入 _envelope 垫片，并把父目录加入 sys.path 后导入 script 包。"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _ok(payload):
    return {"status": "ok", "result": payload}


def _error(message, code="error", **extra):
    body = {"status": "error", "code": code, "error": message}
    body.update(extra)
    return body


if "_envelope" not in sys.modules:
    envelope = types.ModuleType("_envelope")
    envelope.ok = _ok
    envelope.error = _error
    sys.modules["_envelope"] = envelope

from script._script_impl import script_editor  # noqa: E402
from script import _edits, _symbols, _text  # noqa: E402

__all__ = ["script_editor", "_edits", "_symbols", "_text", "ok", "error"]

ok = _ok
error = _error