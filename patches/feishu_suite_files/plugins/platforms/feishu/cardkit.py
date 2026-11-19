"""Feishu CardKit 2.0 streaming transport (豆花本机扩展, 2026-11-06).

Turns ONE Feishu assistant turn into ONE live 2.0 card:
  header(session.title) + status_line + main_answer(typewriter) +
  collapsible_panel("思考过程", think_text) + collapsible_panel("工具调用", tool_text).

Verified against the LIVE Feishu API on 2026-11-06 (cardkit_probe.py / cardkit_demo.py):
  * POST /cardkit/v1/cards             -> card_id  (type=card_json, data=<json string>)
  * POST /im/v1/messages               -> send card (content={"type":"card","data":{"card_id":...}})
  * POST /im/v1/messages/{id}/reply    -> reply-in-thread variant
  * PUT  /cardkit/v1/cards/{id}/elements/{eid}/content {"content","sequence"} -> typewriter
  * PATCH /cardkit/v1/cards/{id}/settings {"settings":"<json>","uuid","sequence"} -> toggle streaming
  * `sequence` is CARD-GLOBAL and must strictly increase across ALL elements/settings.
  * Card entity can be sent only ONCE. streaming_mode auto-closes 10 min after last open.
  * JSON 2.0 cards need client >= 7.20; older clients show an "upgrade" placeholder.

Pure stdlib. All network errors are swallowed and surfaced via last_error / success
flags so a broken card can NEVER take down a gateway run. Gate: FEISHU_CARDKIT=1
in ~/.hermes/.env (default OFF -> zero behaviour change).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

BASE = "https://open.feishu.cn/open-apis"
_ENV = Path.home() / ".hermes" / ".env"
_DB = Path.home() / ".hermes" / "state.db"
_TOKEN_CACHE = {"token": None, "exp": 0.0}
_TOKEN_LOCK = threading.Lock()

# Karing full-tunnel hijacks port 53 / DNS for domestic endpoints; drop proxy for these calls.
for _k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
           "https_proxy", "http_proxy", "all_proxy"):
    os.environ.pop(_k, None)

# One live card per *session scope* (2026-11-07 修)。原实现是 chat_id 单槽：多话题并发时
# 后一轮 ensure() 会 finalize 掉前一轮的卡，思考/工具/正文串台或溢出卡外。
# 现在按 session_key > (chat_id,thread_id) > chat_id 分槽，别名表兼容只传 chat_id 的旧钩子。
_ACTIVE: dict[str, "CardKitStream"] = {}   # scope -> card
_ALIASES: dict[str, str] = {}              # lookup alias -> scope
_RECENT_CARDS: dict[str, "CardKitStream"] = {}  # 最近卡片归档缓存（保证封卡后终态仍能 PATCH 覆盖）
_ACTIVE_LOCK = threading.Lock()


def _scope(chat_id: str, thread_id: Optional[str] = None,
           session_key: Optional[str] = None) -> str:
    """Prefer the exact session key; fall back to thread, then bare chat."""
    if session_key:
        return str(session_key)
    if thread_id:
        return f"{chat_id}|{thread_id}"
    return str(chat_id)


# Feishu closes a card entity's streaming mode 600 s after it was (last) opened.
# Measured 2026-11-09 with probe cards:
#   * PATCH settings streaming_mode=true while still open (t=480 s) is a no-op: the card
#     still died at the same instant (PUT 200850 "card streaming timeout", then 300309).
#   * The same PATCH *after* closure works: t=620 s 200850 -> re-arm success -> PUTs at
#     t=640 s and t=900 s succeed again.
# So: keep streaming until a PUT reports closure, re-arm once, retry that PUT. Only if
# re-arming fails do we fall back to whole-card PATCH /im/v1/messages/{id}.
STREAMING_WINDOW_SECS = 600
STREAM_CLOSED_CODES = (200850, 300309)
MAX_STREAM_REARMS = 30          # 30 x 10 min = 5 h of live streaming per card


class ElementPolicy:
    """五个元素的显示策略（2026-11-07 系统性重构）。

    CardKit 的 collapsible_panel 展开即全量，所以「只显示部分」必须在写入层做。
    """
    TITLE_MAX = 24          # header 取本轮消息摘要前 N 字
    THINK_LINES = 5         # 思考过程只显示最近 N 行（2026-11-07 用户定案）
    THINK_LINE_WIDTH = 88   # 无换行推理流按此宽度软切行
    THINK_BUFFER_MAX = 20000  # 累积缓冲上限（仅用于切行计算）
    TOOL_DETAIL_MAX = 12    # 工具明细只保留最近 N 条
    FLUSH_MIN_GAP = 0.2     # 状态行 / 正文：近实时
    PANEL_FLUSH_GAP = 2.0   # 折叠面板（思考/工具）：整段重绘，必须显著降频
    FULL_PATCH_GAP = 6.0    # 流式窗口关闭后：整卡 PATCH（每次重绘全卡，必须更慢）


def _think_visible(text: str, *, final: bool = False) -> str:
    """平滑固定视窗思考流展示：
    1. 流式思考中（final=False）：
       - 当字数超过视窗阈值（~360字）时，早期内容自动向上收起，视窗固定在最新推理段落；
       - 顶部采用恒定高度单行提示，绝无动态数字跳跃，彻底消除页面抖动与跳行；
    2. 收尾归档（final=True）：
       - 恢复 100% 全量未截断的完整思考记录，归入折叠面板供随时展开查阅。
    """
    clean = text.strip() if text else ""
    if not clean:
        return "（本轮模型无独立推理流）" if final else "（等待思考中…）"
    if final:
        return clean
    if len(clean) <= 360:
        return clean

    tail = clean[-350:]
    first_nl = tail.find("\n")
    if 0 < first_nl < 40:
        tail = tail[first_nl + 1:]
    return "<font color='grey'>▲ 早期思考已向上收起 · 正在最新推理…</font>\n\n" + tail


def format_tool_display(tool_name: str, args: Optional[dict] = None, preview: Optional[str] = None) -> str:
    """Format tool call item in professional developer style (matching screenshot):
    <font color='blue'>●</font>  ✅ **`tool_name`** — `command / query / path`
    """
    cmd = ""
    if isinstance(args, dict):
        if "command" in args:
            cmd = str(args["command"])
        elif "cmd" in args:
            cmd = str(args["cmd"])
        elif "query" in args:
            cmd = str(args["query"])
        elif "path" in args:
            cmd = str(args["path"])
        elif "url" in args:
            cmd = str(args["url"])
        elif "pattern" in args:
            cmd = str(args["pattern"])
        elif "prompt" in args:
            cmd = str(args["prompt"])
        elif "text" in args:
            cmd = str(args["text"])
        elif args:
            parts = [f"{k}={v!r}" for k, v in list(args.items())[:2]]
            cmd = ", ".join(parts)
    if not cmd and preview:
        cmd = str(preview).strip()

    if cmd:
        cmd = " ".join(cmd.split())
        if len(cmd) > 120:
            cmd = cmd[:118] + "…"
        cmd_str = f" — `{cmd}`"
    else:
        cmd_str = ""

    display_tool = "bash" if tool_name in ("terminal", "bash") else tool_name
    return f"<font color='blue'>●</font>  ✅ **`{display_tool}`**{cmd_str}"


class ToolCallEntry(str):
    """Tool entry that behaves like a string (tool_name) for backward compatibility
    while preserving formatted developer display string."""
    __slots__ = ("tool_name", "args", "preview", "formatted")

    def __new__(cls, tool_name: str, args: Optional[dict] = None, preview: Optional[str] = None):
        inst = super().__new__(cls, tool_name)
        inst.tool_name = tool_name
        inst.args = args
        inst.preview = preview
        inst.formatted = format_tool_display(tool_name, args=args, preview=preview)
        return inst


def _render_footer_text(model: Optional[str] = None, reasoning_effort: Optional[str] = None) -> str:
    parts = []
    if model:
        parts.append(f"模型: `{model}`")
    if reasoning_effort:
        parts.append(f"思考等级: `{reasoning_effort}`")
    if not parts:
        return "<font color='grey'>模型: 默认  ·  思考等级: 默认</font>"
    return "<font color='grey'>" + "  ·  ".join(parts) + "</font>"


def _title_from_hint(hint: Optional[str], fallback: str = "会话") -> str:
    """本轮用户消息 → 卡片标题；彻底不查 state.db（自动标题有落库竞态）。"""
    s = str(hint or "").strip()
    if s.startswith("[Replying to:"):
        i = s.find("]")
        if i >= 0:
            s = s[i + 1:].strip()
    s = " ".join(s.split())[:ElementPolicy.TITLE_MAX]
    return s or fallback


def _env_creds() -> dict:
    creds = {}
    try:
        for line in _ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            mm = re.match(r"(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if mm:
                creds[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    except Exception:
        pass
    return creds


def enabled() -> bool:
    """Opt-in gate. Default OFF -> adapter send/edit keep their original behaviour."""
    return str(_env_creds().get("FEISHU_CARDKIT", "")).strip().lower() in ("1", "true", "yes", "on")


def resolve(chat_id: str, *, thread_id: Optional[str] = None,
            session_key: Optional[str] = None,
            message_id: Optional[str] = None) -> Optional["CardKitStream"]:
    """Find this turn's live card: exact keys first, then bare chat fallback."""
    keys: list[str] = []
    if message_id:
        keys.append(f"msg:{message_id}")
    if session_key:
        keys.append(str(session_key))
    if thread_id:
        keys.append(f"{chat_id}|{thread_id}")
    keys.append(str(chat_id))
    with _ACTIVE_LOCK:
        for k in keys:
            scope = _ALIASES.get(k, k)
            ck = _ACTIVE.get(scope)
            if ck is not None and not ck.finalized:
                return ck
    return None


def _log(event: str, **kw) -> None:
    """卡片事件流水，写入 ~/.hermes/logs/cardkit_events.log（永不抛异常）。

    on_send / on_edit / on_tool / finalize 都记一笔，下次「卡片空壳/没弹出」
    能直接看是谁没被调用，不用再猜。
    """
    try:
        rec = {"t": round(time.time(), 3), "event": event, **kw}
        with open(Path.home() / ".hermes" / "logs" / "cardkit_events.log",
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _streaming_on() -> bool:
    """display.streaming 是否开启（纯文本探测，不引入 yaml 依赖；默认 True）。

    streaming ON 时 gateway 的 adapter.send() 不代表最终答复；OFF 时才是。
    """
    try:
        text = (Path.home() / ".hermes" / "config.yaml").read_text(
            encoding="utf-8", errors="ignore")
        m = re.search(r"^\s*streaming:\s*(true|false)\b", text, re.M | re.I)
        return m is None or m.group(1).lower() != "false"
    except Exception:
        return True


def get_active(chat_id: str, *, thread_id: Optional[str] = None,
               session_key: Optional[str] = None,
               message_id: Optional[str] = None) -> Optional["CardKitStream"]:
    return resolve(chat_id, thread_id=thread_id, session_key=session_key,
                   message_id=message_id)


def activate(ck: "CardKitStream") -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[ck.scope] = ck
        aliases = [str(ck.chat_id)]
        if ck.thread_id:
            aliases.append(f"{ck.chat_id}|{ck.thread_id}")
        if ck.session_key:
            aliases.append(str(ck.session_key))
        if ck.message_id:
            aliases.append(f"msg:{ck.message_id}")
        ck._aliases = list(dict.fromkeys(aliases + [ck.scope]))
        for a in ck._aliases:
            _ALIASES[a] = ck.scope


def deactivate(ck: "CardKitStream") -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(ck.scope, None)
        _RECENT_CARDS[ck.scope] = ck
        if len(_RECENT_CARDS) > 30:
            _RECENT_CARDS.pop(next(iter(_RECENT_CARDS)), None)
        for a in getattr(ck, "_aliases", []):
            if _ALIASES.get(a) == ck.scope:
                _ALIASES.pop(a, None)


def _tenant_token() -> Optional[str]:
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["exp"] - 60:
            return _TOKEN_CACHE["token"]
        c = _env_creds()
        app_id, secret = c.get("FEISHU_APP_ID"), c.get("FEISHU_APP_SECRET")
        if not app_id or not secret:
            return None
        body = json.dumps({"app_id": app_id, "app_secret": secret}).encode()
        req = urllib.request.Request(
            BASE + "/auth/v3/tenant_access_token/internal", data=body,
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                obj = json.loads(r.read().decode())
            tok = obj.get("tenant_access_token")
            if tok:
                _TOKEN_CACHE["token"] = tok
                _TOKEN_CACHE["exp"] = time.time() + int(obj.get("expire", 7200))
                return tok
        except Exception:
            return None
    return None


def _api(method: str, path: str, body: Optional[dict] = None,
         params: Optional[dict] = None) -> tuple[dict, int]:
    tok = _tenant_token()
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    h = {"Content-Type": "application/json; charset=utf-8"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()), 200
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), e.code
        except Exception:
            return {"http_error": True}, e.code
    except Exception as e:
        return {"exc": repr(e)}, -1


def session_title(chat_id: str, thread_id: Optional[str] = None,
                  session_key: Optional[str] = None) -> str:
    """Real session.title, aligned to THIS turn (2026-11-07 修).

    原来只按 chat_id 取「最近一条」session，多话题并发时 header 会贴到别的会话标题上。
    优先用精确 session_key，其次 thread_id，最后才退回 chat_id。
    """
    try:
        con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        title = None
        if session_key:
            row = con.execute(
                "select title from sessions where session_key=? "
                "order by last_activity_at desc limit 1", (session_key,)).fetchone()
            title = row["title"] if row else None
        if not title and thread_id:
            row = con.execute(
                "select title from sessions where thread_id=? "
                "order by last_activity_at desc limit 1", (thread_id,)).fetchone()
            title = row["title"] if row else None
        if not title and chat_id:
            row = con.execute(
                "select title from sessions where chat_id=? "
                "order by last_activity_at desc limit 1", (chat_id,)).fetchone()
            title = row["title"] if row else None
        con.close()
        return title or "会话"
    except Exception:
        return "会话"


def _mk_card(title: str, model: Optional[str] = None, reasoning_effort: Optional[str] = None) -> dict:
    def panel(eid, label, inner):
        return {
            "tag": "collapsible_panel", "element_id": eid, "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": label},
                       "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                                "size": "16px 16px"},
                       "icon_position": "right", "icon_expanded_angle": -180},
            "border": {"color": "grey", "corner_radius": "6px"},
            "padding": "8px 12px 8px 12px",
            "elements": [{"tag": "markdown", "element_id": inner, "content": "（等待中…）"}],
        }
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True, "wide_screen_mode": True,
            "summary": {"content": "回复生成中…"},
            "streaming_config": {
                "print_frequency_ms": {"default": 40, "android": 40, "ios": 40, "pc": 40},
                "print_step": {"default": 2, "android": 2, "ios": 2, "pc": 2},
                "print_strategy": "fast",
            },
        },
        "header": {"title": {"tag": "plain_text", "content": title[:80]},
                   "template": "turquoise"},
        "body": {"elements": [
            {"tag": "markdown", "element_id": "status_line", "content": "⏳ 生成中…"},
            {"tag": "hr"},
            {"tag": "markdown", "element_id": "main_answer", "content": "（等待回答…）"},
            panel("think_panel", "思考过程", "think_text"),
            panel("tool_panel", "工具调用", "tool_text"),
            {"tag": "hr"},
            {"tag": "markdown", "element_id": "model_footer", "content": _render_footer_text(model, reasoning_effort)},
        ]},
    }


class CardKitStream:
    """One assistant turn -> one live card. Thread-safe, non-raising, auto-finalizing."""

    # 异常兜底 ONLY：建卡后若三个显式收尾信号(on_send / on_edit(finalize) / 下轮
    # ensure)全都没发生，超时后自动封卡。放宽到 3600s（1小时），彻底避免长时间终端任务或大模型深度思考中途被误杀。
    IDLE_FINALIZE_SECS = 3600

    def __init__(self, chat_id: str, title: Optional[str] = None,
                 reply_to: Optional[str] = None, thread_id: Optional[str] = None,
                 session_key: Optional[str] = None,
                 title_hint: Optional[str] = None):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.session_key = session_key
        self.scope = _scope(chat_id, thread_id, session_key)
        # 标题来自本轮消息摘要，不再依赖异步落库的 session.title
        self.title = title or _title_from_hint(title_hint)
        self.reply_to = reply_to
        self._aliases: list[str] = []
        self._streamed = False
        self.card_id: Optional[str] = None
        self.message_id: Optional[str] = None
        self.open_ok = False
        self.finalized = False
        self.last_error: Optional[str] = None
        self.tool_count = 0
        self.started = time.time()
        self.model: Optional[str] = None
        self.reasoning_effort: Optional[str] = None
        self._seq = 0
        # ── 本轮状态（所有写入只经 render()）──
        self._answer = ""
        self._think = ""                 # 已截断的可见思考文本
        self._think_total = 0            # 累计字符数（用于「已省略前 N 字」）
        self._tool_lines: list[str] = []  # 全部工具事件，按序号，不去重同名
        self._status = "⏳ 生成中…"
        self._last_interim = ""          # 最后一次中间播报，finalize 空正文时兜底
        self._last_written: dict[str, str] = {}
        self._last_put: dict[str, float] = {}
        # Feishu auto-closes streaming_mode 600 s after it was opened. A PUT then returns
        # 200850/300309; we re-arm (works once closed) and retry. Only a failed re-arm switches
        # the card to whole-card PATCH on the message (see STREAMING_WINDOW_SECS).
        self._stream_closed = False   # True only after a re-arm failed: whole-card PATCH mode
        self._rearms = 0
        self._rearm_ok = True
        self._full_patch_at = 0.0
        self._dirty = False
        self._lock = threading.RLock()
        self._render_lock = threading.Lock()
        self._render_pending = False
        self._render_thread: Optional[threading.Thread] = None
        self._timer: Optional[threading.Timer] = None
        self._seal_timer: Optional[threading.Timer] = None
        self._heartbeat_timer: Optional[threading.Timer] = None

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def open(self) -> bool:
        """Create the card entity and send it exactly once."""
        with self._lock:
            if self.card_id:
                return self.open_ok
            card = _mk_card(self.title, self.model, self.reasoning_effort)
            obj, code = _api("POST", "/cardkit/v1/cards",
                             {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)})
            self.card_id = (obj.get("data") or {}).get("card_id")
            if not self.card_id:
                self.last_error = f"card.create code={obj.get('code')} http={code} {obj.get('msg')}"
                return False
            content = json.dumps({"type": "card", "data": {"card_id": self.card_id}},
                                 ensure_ascii=False)
            sent = False
            if self.reply_to:
                robj, rcode = _api("POST", f"/im/v1/messages/{self.reply_to}/reply",
                                   {"msg_type": "interactive", "content": content,
                                    "reply_in_thread": True})
                if robj.get("code") == 0 and (robj.get("data") or {}).get("message_id"):
                    self.message_id = robj["data"]["message_id"]
                    sent = True
                else:
                    self.last_error = f"reply code={robj.get('code')} http={rcode}; fallback to create"
            if not sent and self.thread_id:
                # 豆花补丁 2026-11-19 v2：飞书 message.create 不接受 receive_id_type=thread_id
                # （实测必返 99992402 field validation failed；adapter 音频路径早有同款结论与
                # retry 注释）。落进已有话题的唯一合法方式：从话题里取一条消息当锚点走
                # reply + reply_in_thread——锚点取自 thread 容器，必然加入原话题而非新建。
                tobj, _tcode = _api("GET", "/im/v1/messages", None,
                                    params={"container_id_type": "thread",
                                            "container_id": self.thread_id, "page_size": 1})
                titems = ((tobj.get("data") or {}).get("items") or []) if tobj.get("code") == 0 else []
                anchor = titems[0].get("message_id") if titems else None
                if anchor:
                    robj, rcode = _api("POST", f"/im/v1/messages/{anchor}/reply",
                                       {"msg_type": "interactive", "content": content,
                                        "reply_in_thread": True})
                    if robj.get("code") == 0 and (robj.get("data") or {}).get("message_id"):
                        self.message_id = robj["data"]["message_id"]
                        sent = True
                    else:
                        self.last_error = (f"thread-anchor reply code={robj.get('code')} "
                                           f"http={rcode}; fallback to create")
            if not sent:
                sobj, scode = _api("POST", "/im/v1/messages",
                                   {"receive_id": self.chat_id, "msg_type": "interactive",
                                    "content": content},
                                   params={"receive_id_type": "chat_id"})
                if sobj.get("code") == 0 and (sobj.get("data") or {}).get("message_id"):
                    self.message_id = sobj["data"]["message_id"]
                    sent = True
                else:
                    self.last_error = (f"send code={sobj.get('code')} http={scode} "
                                       f"{sobj.get('msg')}")
            self.open_ok = sent
            if sent:
                activate(self)
                self._arm_finalize()
                self._start_heartbeat()
            return sent

    # -- auto finalize & heartbeat ---------------------------------------------
    def _start_heartbeat(self) -> None:
        """长任务自动心跳：每 30 秒刷新一次运行耗时并 touch 续期，确保卡片长久存活。"""
        if getattr(self, "_heartbeat_timer", None):
            return
        def _beat():
            if self.finalized or not self.open_ok:
                return
            with self._lock:
                if not self._answer:
                    secs = int(time.time() - self.started)
                    m, s = divmod(secs, 60)
                    time_str = f"{m}m {s}s" if m > 0 else f"{s}s"
                    if self._think:
                        self._status = f"🧠 思考中… (已推理 {self._think_total:,} 字) · ⏱️ {time_str}"
                    elif self.tool_count > 0:
                        self._status = f"🔧 执行工具中… (已调用 {self.tool_count} 次) · ⏱️ {time_str}"
                    else:
                        self._status = f"⏳ 正在处理中… · ⏱️ {time_str}"
                    # 心跳只改状态再排队渲染：写卡全部走单一渲染线程，
                    # 心跳定时器线程绝不做网络调用（曾把 .hermes/.env 都读不动的
                    # Too many open files 风暴就是这样来的）。
                    self._schedule_render()
            self._touch()
            if not self.finalized:
                t = threading.Timer(30.0, _beat)
                t.daemon = True
                self._heartbeat_timer = t
                t.start()

        t = threading.Timer(30.0, _beat)
        t.daemon = True
        self._heartbeat_timer = t
        t.start()
    def _arm_finalize(self) -> None:
        if self.finalized:
            return
        if self._timer:
            self._timer.cancel()
        t = threading.Timer(self.IDLE_FINALIZE_SECS, self._auto_finalize)
        t.daemon = True
        self._timer = t
        t.start()

    def _auto_finalize(self) -> None:
        try:
            self.finalize()
        except Exception:
            pass

    def _touch(self) -> None:
        self._arm_finalize()
        with self._lock:
            if self._seal_timer is not None:
                try:
                    self._seal_timer.cancel()
                except Exception:
                    pass
                self._seal_timer = None

    # -- low-level -------------------------------------------------------------
    def _content(self, eid: str, text: str, *, force: bool = False) -> bool:
        # force=True 供 finalize() 收尾写入：此时 finalized 已置位，普通推流应停止，
        # 但状态条 / 空态 / 关流式这几笔必须仍写得进去（2026-11-06 实测曾全被挡）。
        if not self.open_ok or not self.card_id or (self.finalized and not force):
            return False
        # Re-arming already failed once: element PUTs are dead, whole-card PATCH carries on.
        if self._stream_closed:
            self._flush_full_card(force=force)
            return True
        # 首选瞬时写入（2026-11-17 用户反馈「模型很快但卡片一点一点吐字」）：
        # 流式 content PUT 的打字机动画按固定速率渲染增量，整段回答要打几十秒；
        # batch_update 直达目标状态。失败（老卡片实体/权限差异）才回退流式 PUT。
        obj = self._put_content_instant(eid, text)
        if obj.get("code") != 0:
            _log("instant_put_fallback", scope=self.scope, eid=eid,
                 code=obj.get("code"), msg=obj.get("msg") or obj.get("exc"))
            obj = self._put_content(eid, text)
        if obj.get("code") in STREAM_CLOSED_CODES:
            # 飞书 600 秒流式窗口到点（2026-11-09 实测）。关闭后重新打开有效 → 重开并重发这一笔；
            # 重开失败才降级为整卡 PATCH，保证长任务正文不丢。
            _log("stream_closed", scope=self.scope, eid=eid, code=obj.get("code"),
                 age=int(time.time() - self.started), rearms=self._rearms)
            if self._rearm_streaming():
                obj = self._put_content(eid, text)
            if obj.get("code") in STREAM_CLOSED_CODES or not self._rearm_ok:
                self._stream_closed = True
                self._dirty = True
                _log("stream_fallback_full_card", scope=self.scope, eid=eid,
                     code=obj.get("code"), rearms=self._rearms)
                self._flush_full_card(force=force)
                return True
        ok = obj.get("code") == 0
        if not ok:
            self.last_error = f"content({eid}) code={obj.get('code')} {obj.get('msg')}"
        return ok

    def _put_content(self, eid: str, text: str) -> dict:
        obj, _code = _api("PUT", f"/cardkit/v1/cards/{self.card_id}/elements/{eid}/content",
                          {"content": text, "sequence": self._next()})
        return obj if isinstance(obj, dict) else {}

    def _put_content_instant(self, eid: str, text: str) -> dict:
        """一帧替换元素内容（batch_update / partial_update_element）。

        流式 content PUT 会让飞书客户端用**固定速度的打字机动画**渲染增量
        （print_step 2 @ 40ms ≈ 50 字/秒）：模型回答快、整段到达时，卡片要
        "打"几十秒才追上早已完成的模型 —— 就是用户看到的「一点一点吐字」。
        partial_update_element 是卡片实体变更，直接渲染成目标状态，无动画。
        """
        actions = [{
            "action": "partial_update_element",
            "params": {"element_id": eid, "partial_element": {"content": text}},
        }]
        obj, _code = _api("POST", f"/cardkit/v1/cards/{self.card_id}/batch_update",
                          {"uuid": str(uuid.uuid4()), "sequence": self._next(),
                           "actions": json.dumps(actions, ensure_ascii=False)})
        return obj if isinstance(obj, dict) else {}

    def _rearm_streaming(self) -> bool:
        """Re-open streaming_mode after Feishu's 600 s auto-close (works only once closed)."""
        if self._rearms >= MAX_STREAM_REARMS:
            self._rearm_ok = False
            return False
        self._rearms += 1
        obj, _code = _api("PATCH", f"/cardkit/v1/cards/{self.card_id}/settings",
                          {"settings": json.dumps({"config": {"streaming_mode": True}}),
                           "uuid": str(uuid.uuid4()), "sequence": self._next()})
        self._rearm_ok = isinstance(obj, dict) and obj.get("code") == 0
        _log("stream_rearm", scope=self.scope, ok=self._rearm_ok, n=self._rearms,
             code=obj.get("code") if isinstance(obj, dict) else None)
        return self._rearm_ok

    def _full_card_payload(self) -> dict:
        """Whole-card JSON for PATCH /im/v1/messages (no card_id entity needed)."""
        def panel(label: str, inner: str, final: bool) -> dict:
            content = self._render_element(inner, final=final) or "（无）"
            return {
                "tag": "collapsible_panel", "expanded": False,
                "header": {"title": {"tag": "plain_text", "content": label[:40]},
                           "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                                    "size": "16px 16px"},
                           "icon_position": "right", "icon_expanded_angle": -180},
                "border": {"color": "grey", "corner_radius": "6px"},
                "padding": "8px 12px 8px 12px",
                "elements": [{"tag": "markdown", "content": content}],
            }

        think_title = (f"思考过程（共 {len(self._think):,} 字）" if self._think else "思考过程")
        tool_title = f"工具调用（共 {self.tool_count} 次）" if self.tool_count else "工具调用"
        elements = [
            {"tag": "markdown", "content": self._status or "⏳ 生成中…"},
            {"tag": "hr"},
            {"tag": "markdown", "content": self._render_element("main_answer") or "（等待回答…）"},
            panel(think_title, "think_text", False),
            panel(tool_title, "tool_text", False),
        ]
        footer = self._render_element("model_footer")
        if footer:  # 空 markdown 会被飞书拒收
            elements += [{"tag": "hr"}, {"tag": "markdown", "content": footer}]
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "summary": {"content": self.title[:60]}},
            "header": {"title": {"tag": "plain_text", "content": self.title[:80]},
                       "template": "turquoise"},
            "body": {"elements": elements},
        }

    def _flush_full_card(self, *, force: bool = False) -> None:
        """Rate-limited whole-card PATCH; the only write path once streaming closes."""
        if not self.message_id:
            return
        now = time.time()
        if not force and now - self._full_patch_at < ElementPolicy.FULL_PATCH_GAP:
            return  # the next hook (or finalize) carries the newer state
        self._full_patch_at = now
        self._dirty = False
        obj, code = _api("PATCH", f"/im/v1/messages/{self.message_id}",
                         {"msg_type": "interactive",
                          "content": json.dumps(self._full_card_payload(), ensure_ascii=False)})
        ok = obj.get("code") == 0
        if not ok:
            self.last_error = f"full_patch code={obj.get('code')} http={code} {obj.get('msg')}"
        _log("full_patch", scope=self.scope, ok=ok, http=code)

    # -- 统一渲染层（2026-11-07 系统性重构）---------------------------------
    def _render_element(self, eid: str, *, final: bool = False) -> str:
        """由状态算出单个元素的目标文本。策略全部集中在 ElementPolicy。"""
        if eid == "status_line":
            return self._status
        if eid == "main_answer":
            return self._answer or ("（本轮无最终答复）" if final else "（等待回答…）")
        if eid == "think_text":
            if not self._think:
                return "（本轮模型无独立推理流）" if final else "（等待中…）"
            return _think_visible(self._think, final=final)
        if eid == "tool_text":
            n = len(self._tool_lines)
            if not n:
                return "（本轮无工具调用）" if final else "（等待中…）"
            omit = max(0, n - ElementPolicy.TOOL_DETAIL_MAX)
            lines = [f"🔧 {n} 次工具调用", ""]
            if omit:
                lines.append(f"<font color='grey'>（已省略前 {omit} 次调用）</font>\n")
            for ev in self._tool_lines[-ElementPolicy.TOOL_DETAIL_MAX:]:
                formatted = getattr(ev, "formatted", None)
                if not formatted:
                    formatted = ev if (isinstance(ev, str) and (ev.startswith("<font") or ev.startswith("●") or ev.startswith("✅"))) else format_tool_display(str(ev))
                lines.append(formatted)
            return "\n\n".join(lines)
        if eid == "model_footer":
            return _render_footer_text(self.model, self.reasoning_effort)
        return ""

    def render(self, *, force: bool = False) -> None:
        """diff 后只 PUT 变化的元素；所有 hook 只改状态、调 render()。

        禁止各 hook 直接拼文本写元素 —— 这是前四轮「修 A 坏 B」的根源。
        force=True 用于 finalize：跳过节流并写最终空态。
        """
        if not self.open_ok or not self.card_id or self.finalized:
            return
        if self._stream_closed:
            # 流式已关：本次 render 的所有变化都折进一次整卡 PATCH（节流由它自己管）。
            self._flush_full_card(force=force)
            return
        now = time.time()
        for eid in ("status_line", "main_answer", "think_text", "tool_text", "model_footer"):
            text = self._render_element(eid, final=force)
            if text == self._last_written.get(eid):
                continue
            # 折叠面板每次都是「前缀 + 600 字窗口」整段重发，飞书会重绘整块；
            # 0.2s 节流实测约 5 次/秒 → 视觉抖动。面板单独用 2s，finalize 仍 force 刷准。
            gap = (ElementPolicy.PANEL_FLUSH_GAP
                   if eid in ("think_text", "tool_text")
                   else ElementPolicy.FLUSH_MIN_GAP)
            if not force and now - self._last_put.get(eid, 0.0) < gap:
                continue  # 节流：留给下一次 render / finalize 的 force 刷
            # 按尝试时刻节流而不是成功时刻：飞书连续失败时（限流/断网/EMFILE），
            # 原实现不记时间戳 → 每个模型 delta 都全量重试，曾把网关 FD 打爆。
            self._last_put[eid] = now
            if self._content(eid, text):
                self._last_written[eid] = text

    def set_status(self, text: str) -> None:
        with self._lock:  # sequence counter and state must be mutated atomically
            self._status = text
            self._schedule_render()

    # -- streaming sinks -------------------------------------------------------
    def _schedule_render(self) -> None:
        """Coalesce card updates off the model's stream-reading thread.

        A Feishu request is allowed to be slow or wedged; model callbacks only update
        state and return. One writer per card drains the latest state, so a stalled
        display request cannot backpressure WorkBuddy or delay later tool calls.
        """
        if self._render_pending and self._render_thread is not None and self._render_thread.is_alive():
            return
        self._render_pending = True
        self._render_thread = threading.Thread(
            target=self._render_worker, name=f"feishu-card-render-{self.card_id or 'new'}", daemon=True)
        self._render_thread.start()

    def _render_worker(self) -> None:
        try:
            while True:
                with self._lock:
                    if not self._render_pending:
                        break
                    self._render_pending = False
                # 单一写者：渲染期间不持状态锁（模型回调随时更新状态），
                # 但 _render_lock 保证同一时刻只有一个人在写卡——
                # sequence 不会交错，finalize 的收尾渲染也不会和它并发。
                with self._render_lock:
                    self.render()
        except Exception:
            _log("render_worker_error", scope=self.scope, exc_info=True)
        finally:
            with self._lock:
                self._render_thread = None
                if self._render_pending and not self.finalized:
                    self._schedule_render()

    def push_answer(self, text: str) -> bool:
        """累积全量正文；具体落卡由 render() 统一决定。"""
        with self._lock:
            if text is None or not self.open_ok or self.finalized:
                return self.finalized
            self._answer = text
            secs = int(time.time() - self.started)
            m, s = divmod(secs, 60)
            time_str = f"{m}m {s}s" if m > 0 else f"{s}s"
            self._status = f"✍️ 正在回答… · ⏱️ {time_str}"
            self._schedule_render()
            self._touch()
            return True

    def push_thinking(self, text: str) -> bool:
        """推理增量：内部保留全量计数，可见文本只留最近 THINK_MAX 字。"""
        with self._lock:
            if not text or not self.open_ok or self.finalized:
                return self.finalized
            self._think_total += len(text)
            self._think = (self._think + text)[-ElementPolicy.THINK_BUFFER_MAX:]
            secs = int(time.time() - self.started)
            m, s = divmod(secs, 60)
            time_str = f"{m}m {s}s" if m > 0 else f"{s}s"
            self._status = f"🧠 思考中… (已推理 {self._think_total:,} 字) · ⏱️ {time_str}"
            self._schedule_render()
            self._touch()
            return True

    def push_tool(self, line: str, *, args: Optional[dict] = None, preview: Optional[str] = None) -> bool:
        """按序号记录全部工具事件，格式化为专业开发者高密度视图。"""
        with self._lock:
            if not line or not self.open_ok or self.finalized:
                return self.finalized
            s = str(line).strip()
            if s:
                if isinstance(line, ToolCallEntry):
                    entry = line
                elif s.startswith("<font") or s.startswith("●") or s.startswith("✅"):
                    entry = s
                else:
                    entry = ToolCallEntry(s, args=args, preview=preview)
                s = entry
                self._tool_lines.append(s)
                self.tool_count = len(self._tool_lines)
            self._schedule_render()
            self._touch()
            return True

    def _build_completion_card(self) -> dict:
        """Build a static inline card for the PATCH overlay (DSH-style ✅ 覆盖)."""
        def panel(label: str, content: str) -> dict:
            return {
                "tag": "collapsible_panel", "expanded": False,
                "header": {"title": {"tag": "plain_text", "content": label},
                           "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                                    "size": "16px 16px"},
                           "icon_position": "right", "icon_expanded_angle": -180},
                "border": {"color": "grey", "corner_radius": "6px"},
                "padding": "8px 12px 8px 12px",
                "elements": [{"tag": "markdown", "content": content}],
            }
        header_title = f"✅ 回复：{self.title}"
        think_len = len(self._think.strip()) if self._think else 0
        think_title = f"思考过程（共 {think_len:,} 字）" if think_len else "思考过程"
        tool_len = self.tool_count
        tool_title = f"工具调用（共 {tool_len} 次）" if tool_len else "工具调用"
        elements = [
            {"tag": "markdown", "content": self._render_element("status_line", final=True)},
            {"tag": "hr"},
            {"tag": "markdown", "content": self._render_element("main_answer", final=True)},
            panel(think_title, self._render_element("think_text", final=True)),
            panel(tool_title, self._render_element("tool_text", final=True)),
        ]
        # footer：模型 + 思考等级
        footer_parts = []
        m = self.model
        if m:
            footer_parts.append(f"模型 {m}")
        if self.reasoning_effort:
            footer_parts.append(f"思考等级 {self.reasoning_effort}")
        if footer_parts:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown",
                             "content": "<font color='grey'>" + "  ·  ".join(footer_parts) + "</font>"})
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True,
                       "summary": {"content": header_title[:60]}},
            "header": {"title": {"tag": "plain_text", "content": header_title[:80]},
                       "template": "green"},
            "body": {"elements": elements},
        }

    def finalize(self, model: Optional[str] = None, permission: Optional[str] = None,
                 iterations: Optional[int] = None) -> bool:
        """Fill the status line, close streaming_mode, unregister. Idempotent."""
        with self._lock:
            if self.finalized or not self.open_ok or not self.card_id:
                return False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._heartbeat_timer:
                try:
                    self._heartbeat_timer.cancel()
                except Exception:
                    pass
                self._heartbeat_timer = None
            # 兜底：整轮没收到 is_final 的最终答复时（gateway 某些路径不带 notify），
            # 用最后一次中间播报补正文，避免封卡时 main_answer 是空的。
            if not self._answer and self._last_interim:
                self._answer = self._last_interim
            secs = int(time.time() - self.started)
            bits = ["✅ 回合完成"]
            if model or self.model:
                bits.append(str(model or self.model))
            if permission:
                bits.append(permission)
            bits.append(f"{secs}s")
            if iterations:
                bits.append(f"第 {iterations} 轮")
            bits.append(f"{self.tool_count} 次工具调用")
            self._status = "  ·  ".join(bits)
            # 唯一收尾出口：一次性渲染全部元素的最终态（含诚实空态），跳过节流。
            # 与后台渲染线程互斥：force 渲染期间不允许元素写入并发穿插。
            with self._render_lock:
                self.render(force=True)
                # 流式窗口已关时 render(force) 走的是整卡 PATCH；这里再强制刷一次，
                # 保证「✅ 回合完成」状态行与最终正文一定落屏（不受 6s 节流限制）。
                if self._stream_closed:
                    self._flush_full_card(force=True)
            obj, code = _api("PATCH", f"/cardkit/v1/cards/{self.card_id}/settings",
                             {"settings": json.dumps({"config": {"streaming_mode": False}}),
                              "uuid": str(uuid.uuid4()), "sequence": self._next()})
            ok = obj.get("code") == 0
            if not ok:
                self.last_error = f"settings code={obj.get('code')} {obj.get('msg')}"
            # ── PATCH overlay：把 CardKit 流式卡原地覆盖成 DSH 式「✅ 回复」完成态 ──
            # 更新 header title/template → 话题名变成 ✅ 回复：xxx
            if self.message_id:
                if model:
                    self.model = model
                final_card = self._build_completion_card()
                pobj, pcode = _api("PATCH", f"/im/v1/messages/{self.message_id}",
                                   {"msg_type": "interactive",
                                    "content": json.dumps(final_card, ensure_ascii=False)})
                patch_ok = pobj.get("code") == 0
                if not patch_ok:
                    self.last_error = f"patch_overlay code={pobj.get('code')} {pobj.get('msg')}"
                _log("patch_overlay", scope=self.scope, ok=patch_ok, http=pcode)

                # ── 话题完成推送通知（触发飞书移动端/桌面端消息通知与 ✅ 标示） ──
                # 只有真实的回复回合才需要弹通知（不打扰普通控制流）
                target_mid = self.message_id or self.reply_to
                if target_mid:
                    finish_parts = [f"✅ 回复已完成 ({secs}s)"]
                    if self.model or model:
                        finish_parts.append(f"模型: `{self.model or model}`")
                    if self.reasoning_effort:
                        finish_parts.append(f"思考: `{self.reasoning_effort}`")
                    if self.tool_count:
                        finish_parts.append(f"{self.tool_count}次工具")
                    finish_text = " · ".join(finish_parts)
                    try:
                        nobj, ncode = _api("POST", f"/im/v1/messages/{target_mid}/reply",
                                           {"msg_type": "text",
                                            "content": json.dumps({"text": finish_text}, ensure_ascii=False),
                                            "reply_in_thread": True})
                        _log("finish_notification", scope=self.scope, ok=(nobj.get("code") == 0), http=ncode)
                    except Exception as e:
                        _log("finish_notification_err", scope=self.scope, err=str(e))
            # 全部写入完成后再置位：并发推流不会挤进半个收尾。
            self.finalized = True
            _log("finalize", scope=self.scope, secs=secs,
                 answer_len=len(self._answer), tools=self.tool_count)
            deactivate(self)
            return ok


def ensure(chat_id: str, *, reply_to: Optional[str] = None,
           thread_id: Optional[str] = None, session_key: Optional[str] = None,
           title: Optional[str] = None,
           title_hint: Optional[str] = None,
           model: Optional[str] = None,
           reasoning_effort: Optional[str] = None) -> Optional["CardKitStream"]:
    """Turn-scoped entry point, called once per turn from the gateway runner.

    Finalizes any stale card left by a hard-interrupted previous turn (sealing the
    half-card), then opens + registers a fresh one. Every later tee (reasoning /
    tool / answer) reuses it via ``get_active(chat_id)``. Returns the active stream
    or None on failure (never raises into the gateway).
    """
    if not enabled():
        return None
    try:
        # 只回收 *同一 scope* 的半截卡，不再 finalize 整个 chat 的其它话题卡片。
        with _ACTIVE_LOCK:
            old = _ACTIVE.get(_scope(chat_id, thread_id, session_key))
        if old is not None and not old.finalized:
            try:
                old.finalize()
            except Exception:
                deactivate(old)
        ck = CardKitStream(chat_id, title=title, reply_to=reply_to,
                           thread_id=thread_id, session_key=session_key,
                           title_hint=title_hint)
        if model:
            ck.model = model
        if reasoning_effort:
            ck.reasoning_effort = reasoning_effort
        if ck.open():
            return ck
        return None
    except Exception:
        return None


def on_send(chat_id: str, content: str, *, reply_to: Optional[str] = None,
            thread_id: Optional[str] = None,
            session_key: Optional[str] = None,
            is_final: bool = False) -> Optional[str]:
    """Adapter ``send()`` hook.

    2026-11-07 修：gateway streaming ON 时 send() 会被三类「非最终答复」调用——
      * stream_consumer 的首次发送（_first_send，此时还没走过 edit_message）
      * 段间播报 / interim
      * 长任务心跳「⏳ Working — N min」的首次 send
    原实现见到 send 且 _streamed=False 就 _seal_soon() 封卡，实机 181s 封卡、
    turn 实跑 640s，之后所有 thinking/tool/answer 全溢出卡外。
    现在 streaming ON 时 send 一律不封卡：心跳写状态行，其它进思考框；
    封卡只由 on_edit(is_turn_final=True) 负责。streaming OFF 保持原语义。
    """
    if not enabled():
        return None
    ck = get_active(chat_id, thread_id=thread_id, session_key=session_key)
    if ck is None or ck.finalized:
        # 豆花防溢出兜底（2026-11-08）：如果本轮卡片已被提前误封，但同一 scope 刚发过卡片，
        # 绝不让最终文本溢出成外部 post 独立气泡！直接通过 PATCH 把内容更新进卡片正文！
        stale_ck = None
        with _ACTIVE_LOCK:
            target_scope = _scope(chat_id, thread_id, session_key)
            cand = _ACTIVE.get(target_scope) or _RECENT_CARDS.get(target_scope)
            if cand and cand.message_id:
                stale_ck = cand
            else:
                for a, sc in list(_ALIASES.items()):
                    if sc == target_scope:
                        cand = _ACTIVE.get(sc) or _RECENT_CARDS.get(sc)
                        if cand and cand.message_id:
                            stale_ck = cand
                            break
        if stale_ck and stale_ck.message_id and is_final:
            try:
                stale_ck._answer = (stale_ck._answer + "\n\n" + content).strip() if stale_ck._answer else content
                final_card = stale_ck._build_completion_card()
                _api("PATCH", f"/im/v1/messages/{stale_ck.message_id}",
                     {"msg_type": "interactive", "content": json.dumps(final_card, ensure_ascii=False)})
                _log("on_send_revive_patch", scope=target_scope, chars=len(content))
                return stale_ck.message_id
            except Exception:
                pass
        return None
    ck._streamed = True
    heartbeat = content.lstrip().startswith("⏳ Working")
    if heartbeat:
        # 长任务心跳：只刷状态行，别覆盖正文，更不封卡。
        ck.set_status(content)
        ck._touch()
        _log("on_send", kind="heartbeat", scope=ck.scope, chars=len(content))
        return ck.message_id
    if not is_final:
        # 2026-11-07 修（第五次反馈）：工具之间的中间播报 / 首帧预览不是最终答复。
        # 原实现把它们也写 main_answer，35 分钟长任务期间正文被 16~45 字短句
        # 反复冲刷，用户看到的永远是中间文本而非结论。现在只当进度写状态行。
        ck._last_interim = content
        ck.set_status("⏳ " + content.strip().splitlines()[0][:60])
        ck._touch()
        _log("on_send", kind="interim", scope=ck.scope, chars=len(content),
             answer_len=len(ck._answer))
        return ck.message_id
    if content.lstrip().startswith("↪"):
        # busy_input_mode=interrupt 的 redirect 提示（"↪ Redirected current run"）
        # 也带 notify，原实现把它当最终答复写正文并立刻封卡，卡片死在 18 字。
        ck.set_status("⏳ " + content.strip()[:60])
        ck._touch()
        _log("on_send", kind="redirect_note", scope=ck.scope, chars=len(content))
        return ck.message_id
    # 最终答复：唯一写 main_answer 的 send 路径（gateway metadata.notify=True）。
    ok = ck.push_answer(content)
    _log("on_send", kind="answer", scope=ck.scope, chars=len(content),
         wrote=ok, is_final=True, streaming=_streaming_on(),
         answer_len=len(ck._answer))
    # 最终答复一到就短延迟封卡，不再等 IDLE 兜底定时器（实测 3~6 分钟后才
    # 显示「✅ 回合完成」，用户以为卡片卡住）。同时 force 刷思考/工具最终态。
    if _streaming_on():
        _seal_soon(ck, delay=3.5)
    else:
        _seal_soon(ck)
    return ck.message_id


def on_edit(chat_id: str, content: str, *, finalize: bool = False,
            is_turn_final: bool = False,
            message_id: Optional[str] = None, thread_id: Optional[str] = None,
            session_key: Optional[str] = None) -> bool:
    """Adapter ``edit_message()`` hook (gateway streaming ON: accumulated text).

    Returns True so the caller skips its own edit. ``finalize=True`` (the stream
    consumer's turn-final edit) seals the card after a short delay.
    """
    if not enabled():
        return False
    ck = get_active(chat_id, thread_id=thread_id, session_key=session_key,
                    message_id=message_id)
    if ck is None or ck.finalized:
        return False
    ck._streamed = True
    # 长任务心跳（"⏳ Working — N min …"）写状态行，别覆盖正文。
    if content.lstrip().startswith("⏳ Working"):
        ck.set_status(content)
    else:
        ck.push_answer(content)
    # finalize=True 只是「这段消息不再编辑」：工具边界 / 段落封版 / 长文拆块都会触发，
    # 一轮内可能发生多次。只有 is_turn_final 才是真正的回合结束（2026-11-07 修：
    # 原实现见到 finalize 就封卡，第一次工具边界就把卡封死，后续内容全溢出卡外）。
    _log("on_edit", scope=ck.scope, chars=len(content), finalize=finalize,
         is_turn_final=is_turn_final, answer_len=len(ck._answer))
    if is_turn_final:
        _seal_soon(ck)
    return True


def _seal_soon(ck: "CardKitStream", delay: float = 2.5) -> None:
    """Seal after the typewriter renders the tail. Can be cancelled by subsequent activity."""
    with ck._lock:
        if ck.finalized:
            return
        if ck._seal_timer is not None:
            try:
                ck._seal_timer.cancel()
            except Exception:
                pass
            ck._seal_timer = None

        def _do():
            try:
                ck.finalize()
            except Exception:
                pass

        try:
            t = threading.Timer(delay, _do)
            t.daemon = True
            ck._seal_timer = t
            t.start()
        except Exception:
            _do()


def on_tool(chat_id: str, line: str, *, thread_id: Optional[str] = None,
            session_key: Optional[str] = None, args: Optional[dict] = None,
            preview: Optional[str] = None) -> bool:
    """Progress tee: append a real tool.started line into the 工具调用 panel.

    Returns True when the card took the line (so the caller suppresses the
    out-of-card progress bubble); False otherwise (original progress path runs).
    """
    if not line or not enabled():
        return False
    ck = get_active(chat_id, thread_id=thread_id, session_key=session_key)
    if ck is None or ck.finalized:
        return False
    try:
        ok = ck.push_tool(line, args=args, preview=preview)
        _log("on_tool", scope=ck.scope, tool=str(line)[:60], wrote=ok)
        return ok
    except Exception:
        return False



def on_thinking(chat_id: str, text: str, *, thread_id: Optional[str] = None,
                session_key: Optional[str] = None) -> bool:
    """Reasoning / <think> delta -> 「思考过程」折叠框 (2026-11-07).

    接的是 agent.reasoning_callback 的真实流式增量（原生 reasoning 与内联 <think> 块），
    不伪造内容；整轮没有推理流时 finalize() 会写诚实空态。
    """
    if not text or not enabled():
        return False
    ck = get_active(chat_id, thread_id=thread_id, session_key=session_key)
    if ck is None or ck.finalized:
        return False
    try:
        ok = ck.push_thinking(text)
        _log("on_thinking", scope=ck.scope, chars=len(text),
             total=ck._think_total, visible=len(ck._think))
        return ok
    except Exception:
        return False
