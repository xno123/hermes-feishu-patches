"""CardKit bridge (豆花本机补丁 2026-11-07).

Problem: ``plugins/platforms/feishu/__init__.py`` does ``from .adapter import register``,
so ANY ``from plugins.platforms.feishu import cardkit`` drags in adapter.py -> gateway.config
-> ruamel etc. The gateway runs its own interpreter with ``-I`` (isolated), where those
deps are NOT importable -> the import raises ModuleNotFoundError, which the hooks' bare
``except: pass`` swallowed silently. Result: no card, no tool/answer tee (the real root
cause of "输出内容并没有出现在该出现的位置").

Fix: load cardkit.py BY FILE PATH via importlib, as a process-wide singleton, bypassing
the package __init__ entirely. cardkit.py is pure stdlib, so this works in both the
normal interpreter and the gateway's isolated one. All three hook sites import THIS
module (a top-level gateway module with no heavy deps), guaranteeing one shared
``_ACTIVE`` registry.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

_MOD_NAME = "_hermes_cardkit_stream"
_PATH = (Path(__file__).resolve().parent.parent
         / "plugins" / "platforms" / "feishu" / "cardkit.py")
_LOCK = threading.Lock()
_MOD = None


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    with _LOCK:
        if _MOD is not None:
            return _MOD
        # Reuse an already-loaded module if present (e.g. normal import elsewhere).
        existing = sys.modules.get(_MOD_NAME)
        if existing is not None:
            _MOD = existing
            return _MOD
        spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_PATH))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_MOD_NAME] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(_MOD_NAME, None)
            return None
        _MOD = mod
        return _MOD


def get():
    """Return the cardkit module (singleton), or None if it could not load.
    Never raises: callers must degrade to their original path on None."""
    try:
        return _load()
    except Exception:
        return None


# Convenience proxies so hook sites stay one-liners. Each returns a safe default
# on any failure, matching the "never break the gateway run" contract.
def ensure(chat_id, *, reply_to=None, thread_id=None, session_key=None,
           title=None, title_hint=None, model=None, reasoning_effort=None):
    m = get()
    if m is None:
        return None
    try:
        return m.ensure(chat_id, reply_to=reply_to, thread_id=thread_id,
                        session_key=session_key, title=title,
                        title_hint=title_hint, model=model,
                        reasoning_effort=reasoning_effort)
    except Exception:
        return None


def on_send(chat_id, content, *, reply_to=None, thread_id=None, session_key=None,
            is_final=False):
    m = get()
    if m is None:
        return None
    try:
        return m.on_send(chat_id, content, reply_to=reply_to,
                         thread_id=thread_id, session_key=session_key,
                         is_final=is_final)
    except Exception:
        return None


def on_edit(chat_id, content, *, finalize=False, is_turn_final=False,
            message_id=None, thread_id=None, session_key=None):
    m = get()
    if m is None:
        return False
    try:
        return m.on_edit(chat_id, content, finalize=finalize,
                         is_turn_final=is_turn_final, message_id=message_id,
                         thread_id=thread_id, session_key=session_key)
    except Exception:
        return False


def on_tool(chat_id, line, *, thread_id=None, session_key=None, args=None, preview=None):
    m = get()
    if m is None:
        return False
    try:
        return bool(m.on_tool(chat_id, line, thread_id=thread_id,
                              session_key=session_key, args=args, preview=preview))
    except Exception:
        return False


def on_thinking(chat_id, text, *, thread_id=None, session_key=None):
    m = get()
    if m is None:
        return False
    try:
        return bool(m.on_thinking(chat_id, text, thread_id=thread_id,
                                  session_key=session_key))
    except Exception:
        return False
