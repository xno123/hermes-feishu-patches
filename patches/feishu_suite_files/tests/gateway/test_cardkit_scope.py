"""CardKit 会话分槽 / 标题对齐 / 思考流 回归测试（豆花本机补丁 2026-11-07）。

离线：mock 掉 _api 与 state.db，不发任何飞书消息。
覆盖三条修复：
  1. 标题按 session_key 精确取，不再按 chat_id 取「最近一条」串台；
  2. 不同 session 的卡片互不 finalize（原 chat_id 单槽会互相踢掉）；
  3. reasoning/<think> delta 能进「思考过程」折叠框，且 on_edit 按 message_id 定位卡。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CK_PATH = ROOT / "plugins" / "platforms" / "feishu" / "cardkit.py"

# 与 state.db 里真实格式一致（gateway 按 thread_id 派生）
SKEY_A = "agent:main:feishu:dm:chatA:sess1"
SKEY_B = "agent:main:feishu:dm:chatA:sess2"


def _load():
    name = "_hermes_cardkit_test"
    spec = importlib.util.spec_from_file_location(name, str(CK_PATH))
    assert spec is not None and spec.loader is not None, "cardkit.py 加载失败"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ck(tmp_path, monkeypatch):
    mod = _load()
    mod._ACTIVE.clear()
    mod._ALIASES.clear()

    calls: list = []

    def fake_api(method, path, body=None, params=None):
        calls.append((method, path, body))
        if method == "POST" and path == "/cardkit/v1/cards":
            return {"code": 0, "data": {"card_id": f"card_{len(calls)}"}}, 200
        if method == "POST" and path.startswith("/im/v1/messages"):
            return {"code": 0, "data": {"message_id": f"msg_{len(calls)}"}}, 200
        return {"code": 0}, 200

    monkeypatch.setattr(mod, "_api", fake_api)
    monkeypatch.setattr(mod, "enabled", lambda: True)

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table sessions (session_key text, thread_id text, chat_id text, "
        "title text, last_activity_at real)"
    )
    con.executemany(
        "insert into sessions values (?,?,?,?,?)",
        [
            (SKEY_A, "omt_1", "chatA", "会话甲的标题", 1.0),
            (SKEY_B, "omt_2", "chatA", "会话乙的标题", 2.0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "_DB", db)
    setattr(mod, "calls", calls)
    yield mod
    for card in list(mod._ACTIVE.values()):
        if card._timer:
            card._timer.cancel()
    mod._ACTIVE.clear()
    mod._ALIASES.clear()


def _calls(ck):
    return getattr(ck, "calls")


def test_scope_priority(ck):
    assert ck._scope("chatA") == "chatA"
    assert ck._scope("chatA", thread_id="omt_1") == "chatA|omt_1"
    assert ck._scope("chatA", thread_id="omt_1", session_key="raw_key") == "raw_key"


def test_title_aligned_to_session_key_not_latest_chat(ck):
    """同一 chat 下最近的 session 是『会话乙』，但 sess1 的卡片必须拿『会话甲』。"""
    assert ck.session_title("chatA", session_key=SKEY_A) == "会话甲的标题"
    assert ck.session_title("chatA", session_key=SKEY_B) == "会话乙的标题"
    assert ck.session_title("chatA", thread_id="omt_1") == "会话甲的标题"
    # 只有 chat_id 时才退回「最近一条」（兼容旧钩子）
    assert ck.session_title("chatA") == "会话乙的标题"


def test_concurrent_sessions_do_not_finalize_each_other(ck):
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A,
                  title_hint="检查 hermes 更新")
    b = ck.ensure("chatA", reply_to="root2", session_key=SKEY_B,
                  title_hint="另一个任务")
    assert a is not None and b is not None
    assert a is not b and a.open_ok and b.open_ok
    assert not a.finalized, "后一轮 ensure 把前一轮的卡 finalize 掉了（原单槽 bug）"
    # 标题来自本轮消息摘要，不查 state.db（自动标题有落库竞态）
    assert a.title == "检查 hermes 更新" and b.title == "另一个任务"

    # 工具行按 scope 精确落到 b，不串到 a
    assert ck.on_tool("chatA", "terminal", session_key=SKEY_B) is True
    assert b._tool_lines == ["terminal"]
    assert a._tool_lines == []

    # 只封 a，b 仍活着
    ck.on_send("chatA", "答案A", session_key=SKEY_A, is_final=True)
    a.finalize()
    assert a.finalized and not b.finalized
    assert ck.get_active("chatA", session_key=SKEY_B) is b


def test_thinking_delta_lands_in_panel(ck):
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    assert ck.on_thinking("chatA", "先", session_key=SKEY_A) is True
    assert ck.on_thinking("chatA", "查一下", session_key=SKEY_A) is True
    assert a._think == "先查一下"
    a.finalize()
    empty = [c for c in _calls(ck) if c[0] == "PUT"
             and c[1].endswith("/think_text/content") and "无独立推理流" in str(c[2])]
    assert not empty, "有真实推理流却写了空态"


def test_finalize_writes_honest_empty_state(ck):
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    a.finalize()
    empty = [c for c in _calls(ck) if c[0] == "PUT"
             and c[1].endswith("/think_text/content") and "无独立推理流" in str(c[2])]
    assert len(empty) == 1


def test_edit_resolves_card_by_message_id(ck):
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    b = ck.ensure("chatA", reply_to="root2", session_key=SKEY_B)
    # 模拟 streaming 的 edit_message：只知道 chat_id + message_id
    assert ck.on_edit("chatA", "累积正文", message_id=b.message_id) is True
    assert b._answer == "累积正文"
    assert a._answer == ""
    assert ck.on_edit("chatA", "最终正文", finalize=True, message_id=b.message_id) is True
    b.finalize()
    assert b.finalized and not a.finalized


def test_tool_tee_runs_without_progress_queue(monkeypatch):
    """飞书 tool_progress=false → needs_progress_queue=False → progress_queue=None。

    回归：原实现把 CardKit 工具 tee 放在 `if not ctx.progress_queue: return` 之后，
    导致 on_tool 从不执行、「工具调用」折叠框永远空。
    """
    import types
    from gateway import run_turn_progress as rp
    from gateway import cardkit_bridge as bridge

    seen = []
    monkeypatch.setattr(
        bridge, "on_tool",
        lambda chat, name, **kw: seen.append((chat, name, kw)) or True,
    )
    monkeypatch.setattr(rp, "local_viewer_turn", lambda _self: False)

    inst = types.SimpleNamespace()
    inst._ctx = types.SimpleNamespace(
        source=types.SimpleNamespace(chat_id="chatA", thread_id="omt_a",
                                     session_key="skeyA", platform=None),
        session_key="skeyA",
        progress_queue=None,          # 关键：飞书实际运行态
        log_queue=None,
        _run_still_current=lambda: True,
        progress_mode="off",
        last_tool=[None],
        last_progress_msg=[None],
        repeat_count=[0],
        long_tool_hint_fired=[False],
        tool_progress_enabled=False,
        _thinking_enabled=False,
        _native_slack_task_cards=False,
        _cleanup_progress=False,
        _cleanup_msg_ids=[],
    )
    inst._progress_live_status = lambda *a, **k: None
    inst._progress_subagent_notice = lambda *a, **k: None
    inst._progress_onboarding_hint = lambda *a, **k: None
    inst._stream_consumer = lambda: None

    rp.GatewayTurnProgressMixin.progress_callback(  # type: ignore[arg-type]
        inst, "tool.started", "terminal", None, None
    )
    assert len(seen) == 1, "progress_queue=None 时工具 tee 没有执行"
    assert seen[0][0] == "chatA" and seen[0][1] == "terminal"
    assert seen[0][2]["thread_id"] == "omt_a"
    assert seen[0][2]["session_key"] == "skeyA"


def test_segment_finalize_does_not_seal_card(ck, monkeypatch):
    """finalize=True 是段落封版（工具边界/拆块），不是回合结束。

    回归：原实现见到 finalize 就封卡，第一次工具边界后卡片 finalized，
    之后所有 thinking/tool/answer 全落到卡外独立消息。
    """
    sealed = []
    monkeypatch.setattr(ck, "_seal_soon", lambda card, delay=1.5: sealed.append(card))
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)

    assert ck.on_edit("chatA", "段落一", finalize=True,
                      is_turn_final=False, session_key=SKEY_A) is True
    assert not sealed, "段落封版误触发封卡"
    assert not a.finalized
    # 封版之后工具调用仍要能进卡
    assert ck.on_tool("chatA", "terminal", session_key=SKEY_A) is True
    assert a._tool_lines == ["terminal"]

    assert ck.on_edit("chatA", "最终答复", finalize=True,
                      is_turn_final=True, session_key=SKEY_A) is True
    assert len(sealed) == 1 and sealed[0] is a, "回合结束没有触发封卡"
    assert a._answer == "最终答复"


def test_send_writes_answer_not_thinking_panel(ck, monkeypatch):
    """streaming ON 时 send() 的正文必须落进 main_answer。

    回归：补修 3 把 send 文本全塞进 think_text，最终答复走 send 时正文
    永远停在「（等待回答…）」（第四次反馈）。
    """
    sealed = []
    monkeypatch.setattr(ck, "_seal_soon", lambda card, delay=1.5: sealed.append(card))
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    assert ck.on_edit("chatA", "累积正文", finalize=False, session_key=SKEY_A) is True
    assert ck.on_send("chatA", "根因方向已经清楚了，再核一层", session_key=SKEY_A, is_final=True) is not None
    # 补修 8：带 is_final 的答复会排一次短延迟封卡，但不立即置 finalized
    assert len(sealed) == 1 and not a.finalized
    assert a._answer == "根因方向已经清楚了，再核一层", "正文没写进 main_answer"
    assert a._think == ""


def test_heartbeat_writes_status_line_not_answer(ck):
    """长任务心跳别把正文覆盖掉。"""
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    assert ck.on_edit("chatA", "累积正文", finalize=False, session_key=SKEY_A) is True
    assert ck.on_edit("chatA", "⏳ Working — 6 min — iteration 4/150",
                      finalize=False, session_key=SKEY_A) is True
    assert a._answer == "累积正文"
    a.finalize()
    status = [c for c in _calls(ck) if c[0] == "PUT"
              and c[1].endswith("/status_line/content")]
    assert status


def test_send_never_seals_card_when_streaming_on(ck, monkeypatch):
    """streaming ON 时 send() 是首次发送/段间播报/心跳，绝不是最终答复。

    回归：原实现在 _streamed=False 时见 send 就封卡，实机 181s 封卡、
    turn 实跑 640s，之后所有内容溢出卡外（第二次反馈）。
    """
    sealed = []
    monkeypatch.setattr(ck, "_seal_soon", lambda card, delay=1.5: sealed.append(card))
    monkeypatch.setattr(ck, "_streaming_on", lambda: True)
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)

    # 非 final 的 send（首次发送/中间播报/心跳）一律不封卡、不写正文
    assert ck.on_send("chatA", "我先把补丁整理一遍", session_key=SKEY_A, is_final=False) is not None
    assert ck.on_send("chatA", "⏳ Working — 9 min", session_key=SKEY_A) is not None
    assert not sealed and not a.finalized, "非最终答复误封卡"
    assert a._answer == "", "中间播报不该写正文"

    # 带 is_final 的答复：写正文 + 排一次短延迟封卡（不立即置 finalized）
    assert ck.on_send("chatA", "我先把补丁整理一遍", session_key=SKEY_A, is_final=True) is not None
    assert len(sealed) >= 1 and not a.finalized
    assert a._answer == "我先把补丁整理一遍"

    assert ck.on_send("chatA", "⏳ Working — 3 min — iteration 11/150",
                      session_key=SKEY_A) is not None
    assert len(sealed) >= 1 and not a.finalized, "心跳 send 额外封卡"
    assert a._answer == "我先把补丁整理一遍", "心跳把正文覆盖了"
    status = [c for c in _calls(ck) if c[0] == "PUT"
              and c[1].endswith("/status_line/content")]
    assert status, "心跳没写状态行"

    # 卡片还活着：后续工具调用继续进折叠框
    assert ck.on_tool("chatA", "terminal", session_key=SKEY_A) is True
    assert a._tool_lines == ["terminal"]

    # 只有真正回合结束的 edit 才封卡
    assert ck.on_edit("chatA", "最终答复", finalize=True,
                      is_turn_final=True, session_key=SKEY_A) is True
    assert len(sealed) >= 1 and sealed[0] is a


def test_streaming_off_keeps_legacy_send_semantics(ck, monkeypatch):
    """display.streaming=false 时 send() 仍是最终答复：写正文并封卡。"""
    sealed = []
    monkeypatch.setattr(ck, "_seal_soon", lambda card, delay=1.5: sealed.append(card))
    monkeypatch.setattr(ck, "_streaming_on", lambda: False)
    a = ck.ensure("chatA", reply_to="root1", session_key=SKEY_A)
    assert ck.on_send("chatA", "最终答复", session_key=SKEY_A, is_final=True) is not None
    assert a._answer == "最终答复"
    assert len(sealed) == 1


# ── 系统性重构：ElementPolicy 策略测试（2026-11-07）──────────────────────


def test_title_from_hint_strips_reply_prefix_and_truncates(ck):
    assert ck._title_from_hint(
        '[Replying to: "我在这里。"]  检查 hermes 最近更新状态'
    ) == "检查 hermes 最近更新状态"
    assert len(ck._title_from_hint("很长的标题" * 20)) == ck.ElementPolicy.TITLE_MAX
    assert ck._title_from_hint(None) == "会话"
    assert ck._title_from_hint("   ") == "会话"


def test_title_no_longer_reads_state_db(ck):
    """卡片标题不依赖 state.db：本轮消息摘要一定是本轮的，不会串台。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A,
                  title_hint="帮我查一下备份为什么没跑")
    assert a.title == "帮我查一下备份为什么没跑"
    assert "会话乙" not in a.title


def test_thinking_only_keeps_recent_part(ck, monkeypatch):
    monkeypatch.setattr(ck.ElementPolicy, "FLUSH_MIN_GAP", 0.0)
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    long_think = "前期推理段落甲乙丙丁" * 50 + "\n最新关键结论庚辛"
    assert ck.on_thinking("chatA", long_think, session_key=SKEY_A) is True
    text = a._render_element("think_text")
    assert "▲ 早期思考已向上收起" in text
    assert "最新关键结论庚辛" in text
    # finalize 时恢复 100% 全量未截断文本
    full_text = a._render_element("think_text", final=True)
    assert full_text == long_think


def test_consecutive_same_tools_all_counted(ck, monkeypatch):
    """回归：原 push_tool 只比最后一条去重，8 次 terminal 只剩 2 次。"""
    monkeypatch.setattr(ck.ElementPolicy, "FLUSH_MIN_GAP", 0.0)
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    for _ in range(8):
        assert ck.on_tool("chatA", "terminal", session_key=SKEY_A, args={"command": "ls -l"}) is True
    assert a.tool_count == 8 and len(a._tool_lines) == 8
    text = a._render_element("tool_text")
    assert "bash" in text
    assert "ls -l" in text
    assert "✅" in text
    a.finalize()
    status = [c for c in _calls(ck) if c[0] == "PUT"
              and c[1].endswith("/status_line/content")]
    assert any("8 次工具调用" in str(c[2]) for c in status)


def test_tool_detail_keeps_last_12(ck, monkeypatch):
    monkeypatch.setattr(ck.ElementPolicy, "FLUSH_MIN_GAP", 0.0)
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    for i in range(20):
        ck.on_tool("chatA", f"tool_{i}", session_key=SKEY_A, args={"query": f"q_{i}"})
    text = a._render_element("tool_text")
    assert "已省略前 8 次" in text
    assert "tool_8" in text and "tool_19" in text
    assert "tool_0" not in text


def test_render_diff_and_flush_throttle(ck):
    """8 次工具事件不应产生 8 次全量 PUT；finalize 的 force 刷要补最终态。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    for _ in range(8):
        ck.on_tool("chatA", "terminal", session_key=SKEY_A)
    puts = [c for c in _calls(ck) if c[0] == "PUT"
            and c[1].endswith("/tool_text/content")]
    assert len(puts) <= 2, f"节流失效，tool_text 被 PUT {len(puts)} 次"
    a.render(force=True)
    puts = [c for c in _calls(ck) if c[0] == "PUT"
            and c[1].endswith("/tool_text/content")]
    assert "🔧 8 次工具调用" in puts[-1][2]["content"]


def test_finalize_renders_all_five_elements(ck):
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    assert ck.on_send("chatA", "最终答复", session_key=SKEY_A, is_final=True) is not None
    a.finalize()
    for eid in ("status_line", "main_answer", "think_text", "tool_text"):
        puts = [c for c in _calls(ck) if c[0] == "PUT"
                and c[1].endswith(f"/{eid}/content")]
        assert puts, f"{eid} 没被统一渲染"
    status = [c for c in _calls(ck) if c[0] == "PUT"
              and c[1].endswith("/status_line/content")]
    assert any("✅ 回合完成" in str(c[2]) for c in status)


# ── 第五次反馈回归：中间播报不得覆盖正文（2026-11-07）────────────────────


def test_interim_send_does_not_overwrite_answer(ck):
    """35 分钟长任务里工具间的 16~45 字短句不能把结论冲刷掉。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    assert ck.on_send("chatA", "最终结论：修复已生效", session_key=SKEY_A,
                      is_final=True) is not None
    assert a._answer == "最终结论：修复已生效"
    for s in ("输出太长，我精确聚合一下", "继续拉飞书 API", "再核一次标题"):
        assert ck.on_send("chatA", s, session_key=SKEY_A, is_final=False) is not None
        assert a._answer == "最终结论：修复已生效", f"被中间播报覆盖：{s}"
    assert a._status.startswith("⏳")


def test_only_final_send_writes_main_answer(ck, monkeypatch):
    monkeypatch.setattr(ck.ElementPolicy, "FLUSH_MIN_GAP", 0.0)
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    ck.on_send("chatA", "中间播报文本", session_key=SKEY_A, is_final=False)
    puts = [c for c in _calls(ck) if c[0] == "PUT"
            and c[1].endswith("/main_answer/content")]
    assert all("中间播报文本" not in str(c[2]) for c in puts), "中间播报写进了正文"
    assert a._answer == ""
    ck.on_send("chatA", "真正的结论", session_key=SKEY_A, is_final=True)
    puts = [c for c in _calls(ck) if c[0] == "PUT"
            and c[1].endswith("/main_answer/content")]
    assert any("真正的结论" in str(c[2]) for c in puts)


# ── 第六次反馈回归：折叠面板降频 + 空正文兜底（2026-11-07）────────────────


def test_panel_flush_is_slower_than_status(ck):
    """思考/工具面板整段重绘，2s 节流；状态行/正文仍 0.2s 近实时。"""
    assert ck.ElementPolicy.PANEL_FLUSH_GAP > ck.ElementPolicy.FLUSH_MIN_GAP
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    for i in range(10):
        ck.on_thinking("chatA", f"片段{i}", session_key=SKEY_A)
    tp = [c for c in _calls(ck) if c[0] == "PUT"
          and c[1].endswith("/think_text/content")]
    assert len(tp) <= 1, f"面板未降频，2 秒内 PUT 了 {len(tp)} 次"
    a.render(force=True)  # finalize 的强制刷仍要写最终态
    tp = [c for c in _calls(ck) if c[0] == "PUT"
          and c[1].endswith("/think_text/content")]
    assert "片段9" in tp[-1][2]["content"]


def test_finalize_falls_back_to_last_interim(ck):
    """gateway 某些路径不带 notify → 最终答复被误判 interim；封卡不能空正文。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    assert ck.on_send("chatA", "这是本轮的最终结论，共若干字", session_key=SKEY_A,
                      is_final=False) is not None
    assert a._answer == ""
    a.finalize()
    assert a._answer == "这是本轮的最终结论，共若干字"
    puts = [c for c in _calls(ck) if c[0] == "PUT"
            and c[1].endswith("/main_answer/content")]
    assert any("这是本轮的最终结论" in str(c[2]) for c in puts)


# ── 第七次反馈回归：答复到即封卡 + redirect 提示不当结论（2026-11-07）────


def test_final_answer_seals_promptly(ck, monkeypatch):
    """答复一到就封卡，不再等 IDLE 兜底定时器（实测要 3~6 分钟）。"""
    sealed = []
    monkeypatch.setattr(ck, "_seal_soon",
                        lambda card, delay=1.5: sealed.append((card, delay)))
    monkeypatch.setattr(ck, "_streaming_on", lambda: True)
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    assert ck.on_send("chatA", "这是最终结论", session_key=SKEY_A,
                      is_final=True) is not None
    assert a._answer == "这是最终结论"
    assert len(sealed) == 1 and sealed[0][0] is a


def test_redirect_note_not_written_as_answer(ck):
    """busy_input_mode=interrupt 的 ↪ 提示也带 notify，不能当最终答复封卡。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    assert ck.on_send("chatA", "↪ Redirected current run", session_key=SKEY_A,
                      is_final=True) is not None
    assert a._answer == "", "系统提示被写进正文了"
    assert a._status.startswith("⏳")


# ── 600 秒流式窗口（2026-11-09 探针实测）──────────────────────────────
# 飞书在 streaming_mode 打开 600s 后自动关闭（PUT 返回 200850 → 300309）。
# 未关闭前 PATCH settings 重开无效；关闭之后重开有效（t=620s 重开 → 640s/900s PUT 成功）。
# 策略：PUT 报关闭 → 重开 → 重发同一笔；重开失败才降级整卡 PATCH 消息。

def _stream_api(calls, *, rearm_ok=True, put_codes=None):
    """fake _api：PUT 依次返回 put_codes（用尽后 0），settings 重开按 rearm_ok。"""
    seq = list(put_codes or [])

    def fake_api(method, path, body=None, params=None):
        calls.append((method, path, body))
        if method == "PUT" and "/elements/" in path:
            code = seq.pop(0) if seq else 0
            return ({"code": code, "msg": "card streaming timeout"} if code
                    else {"code": 0}), 200
        if method == "PATCH" and path.endswith("/settings") and '"streaming_mode": true' in str(body):
            return ({"code": 0} if rearm_ok else {"code": 300309, "msg": "closed"}), 200
        return {"code": 0, "data": {"card_id": "card_x", "message_id": "msg_x"}}, 200
    return fake_api


def test_closed_window_is_rearmed_and_the_put_retried(ck):
    """关闭后重开成功：同一笔正文重发进流式元素，不降级、不丢字。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._seq = 0
    calls = _calls(ck)
    ck._api = _stream_api(calls, rearm_ok=True, put_codes=[200850])
    a.push_answer("十分钟后的正文")
    puts = [c for c in calls if c[0] == "PUT"]
    rearm_i = next(i for i, c in enumerate(calls) if c[0] == "PATCH" and c[1].endswith("/settings")
                   and '"streaming_mode": true' in str(c[2]))
    rejected = calls[rearm_i - 1]
    assert rejected[0] == "PUT", "重开前那一笔应是被 200850 拒掉的 PUT"
    retried = calls[rearm_i + 1]
    assert retried[0] == "PUT" and retried[1] == rejected[1], "重开后要重发被拒的同一元素"
    assert retried[2]["content"] == rejected[2]["content"]
    assert any("十分钟后的正文" in str(c[2]) for c in puts[1:]), "正文仍走流式元素写入"
    assert sum(1 for c in calls if c[0] == "PATCH" and c[1].endswith("/settings")) == 1
    assert a._stream_closed is False, "重开成功不该降级为整卡 PATCH"
    seqs = [c[2]["sequence"] for c in calls if isinstance(c[2], dict) and "sequence" in c[2]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "sequence 必须严格递增"


def test_failed_rearm_falls_back_to_full_card_patch(ck):
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._seq = 0
    calls = _calls(ck)
    ck._api = _stream_api(calls, rearm_ok=False, put_codes=[200850, 200850])
    a.push_answer("第一段正文")
    assert a._stream_closed is True
    patches = [c for c in calls if c[0] == "PATCH" and c[1].startswith("/im/v1/messages/")]
    assert patches, "重开失败后必须改写整卡消息"
    assert "第一段正文" in str(patches[-1][2])


def test_rearm_success_but_put_still_closed_falls_back(ck):
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._seq = 0
    calls = _calls(ck)
    ck._api = _stream_api(calls, rearm_ok=True, put_codes=[200850, 300309])
    a.push_answer("仍然关着")
    assert a._stream_closed is True
    assert any(c[0] == "PATCH" and c[1].startswith("/im/v1/messages/") for c in calls)


def test_rearm_attempts_are_capped(ck):
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._rearms = ck.MAX_STREAM_REARMS
    assert a._rearm_streaming() is False


def test_no_time_guard_before_feishu_reports_closure(ck, monkeypatch):
    """不再按本地计时提前停 PUT：窗口是从「最后一次打开」算起，本地计时会误判。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._seq = 0
    monkeypatch.setattr(ck.time, "time", lambda: a.started + ck.STREAMING_WINDOW_SECS + 120)
    calls = _calls(ck)
    ck._api = _stream_api(calls)
    a.push_answer("第 12 分钟的正文")
    assert [c for c in calls if c[0] == "PUT" and "第 12 分钟的正文" in str(c[2])]
    assert a._stream_closed is False


def test_full_card_patch_carries_late_content_after_window(ck):
    """降级模式下，第 10 分钟之后到达的正文仍要落屏（就是原来丢失的那段）。"""
    a = ck.ensure("chatA", reply_to="root", session_key=SKEY_A)
    a._seq = 0
    a._stream_closed = True
    calls = _calls(ck)
    a._full_patch_at = 0.0
    a.push_answer("十分钟之后才生成的结论")
    a.push_tool("terminal")
    a.finalize()
    patches = [c for c in calls if c[0] == "PATCH" and c[1].startswith("/im/v1/messages/")]
    assert patches
    assert any("十分钟之后才生成的结论" in str(p[2]) for p in patches)
