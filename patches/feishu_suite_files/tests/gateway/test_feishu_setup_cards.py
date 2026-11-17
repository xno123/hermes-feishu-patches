"""Feishu setup cards: dynamic catalog, channel scoping, form routing (豆花 2026-11-09).

离线：目录、网关、飞书 API 全部用假对象，不发任何网络请求。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from plugins.platforms.feishu import feishu_setup_cards as fc
from plugins.platforms.feishu.adapter_choice_cards import FeishuChoiceCardsMixin


# ── synthetic catalog (mirrors the shape list_picker_providers returns) ────
CATALOG = [
    {"slug": "antigravity-manager", "name": "Antigravity Manager", "is_current": True,
     "models": ["gemini-3.8-flash-high", "claude-opus-4-6-thinking", "gpt-oss-120b"], "total_models": 3},
    {"slug": "custom:openrouter", "name": "OpenRouter", "is_user_defined": True,
     "models": ["zzz/dup-should-be-dropped"], "total_models": 1},
    {"slug": "openrouter", "name": "OpenRouter",
     "models": ["anthropic/claude-opus-5.5", "anthropic/claude-opus-4.8",
                "anthropic/claude-sonnet-5.5", "openai/gpt-6-astra", "openai/gpt-6-astra:free",
                "openai/gpt-6-astra:batch", "deepseek/deepseek-v4-pro",
                "deepseek/deepseek-v4-pro-0813", "xiaomi/mimo-v2.6-pro", "~unbiased/pareto-latest"],
     "total_models": 10},
    {"slug": "nous", "name": "Nous Portal", "models": ["anthropic/claude-sonnet-5.5", "openai/gpt-5.5"],
     "total_models": 2},
    {"slug": "workbuddy", "name": "WorkBuddy Hub",
     "models": ["hy4free", "glm-5.3", "kimi-k3.1-pro", "deepseek-v4.1-flash"], "total_models": 4},
    {"slug": "dashscope", "name": "dashscope",
     "models": ["qwen3.8-max", "deepseek-v4.1-flash-1017", "qwen-audio-3.0-tts-plus",
                "qwen-audio-3.0-asr-flash"], "total_models": 4},
    {"slug": "deepseek", "name": "DeepSeek", "models": ["deepseek-flash", "deepseek-v4-pro"],
     "total_models": 2},
    {"slug": "moa", "name": "MoA", "models": ["moa-panel"], "total_models": 1},
    {"slug": "empty-provider", "name": "Empty", "models": [], "total_models": 0},
]


@pytest.fixture(autouse=True)
def _clean_catalog(monkeypatch):
    monkeypatch.setattr(fc, "_CATALOG", {"providers": None, "at": 0.0})
    fc.remember_catalog(CATALOG)
    yield
    monkeypatch.setattr(fc, "_CATALOG", {"providers": None, "at": 0.0})


# ── catalog normalisation ─────────────────────────────────────────────────
def test_channels_come_from_catalog():
    slugs = [c["slug"] for c in fc.normalize_channels()]
    assert slugs == ["antigravity-manager", "openrouter", "nous", "workbuddy", "dashscope", "deepseek"]
    # custom:openrouter 与原生重复 → 丢掉；moa / 空渠道 → 不出现
    assert "custom:openrouter" not in slugs and "moa" not in slugs and "empty-provider" not in slugs


def test_non_chat_endpoints_filtered():
    dash = fc.find_channel("dashscope", fc.normalize_channels())
    assert dash["models"] == ["qwen3.8-max", "deepseek-v4.1-flash-1017"]


def test_aggregator_flagship_filter_is_family_based():
    models = fc.find_channel("openrouter", fc.normalize_channels())["models"]
    assert "anthropic/claude-opus-5.5" in models
    assert "anthropic/claude-opus-4.8" not in models      # 同族旧版本
    assert "deepseek/deepseek-v4-pro" in models
    assert "deepseek/deepseek-v4-pro-0813" not in models  # 日期快照
    assert not any(":free" in m or ":batch" in m or m.startswith("~") for m in models)
    assert "xiaomi/mimo-v2.6-pro" in models               # 独有家族保留
    assert len(models) <= fc.AGGREGATOR_MAX


def test_direct_provider_keeps_every_model():
    assert fc.find_channel("workbuddy", fc.normalize_channels())["models"] == [
        "hy4free", "glm-5.3", "kimi-k3.1-pro", "deepseek-v4.1-flash"]


# ── helpers ───────────────────────────────────────────────────────────────
def test_reasoning_effort_value():
    assert fc.reasoning_effort_value({"enabled": True, "effort": "high"}) == "high"
    assert fc.reasoning_effort_value({"enabled": False}) == "none"
    assert fc.reasoning_effort_value("xhigh") == "xhigh"
    assert fc.reasoning_effort_value(None) is None


def test_default_model_and_option_encoding():
    chans = fc.normalize_channels()
    assert fc.default_model_for("workbuddy", chans) == "hy4free"
    assert fc.default_model_for("workbuddy", chans, "glm-5.3") == "glm-5.3"
    assert fc.decode_option(fc.encode_option("workbuddy", "glm-5.3")) == ("workbuddy", "glm-5.3")
    assert fc.decode_option("glm-5.3", "zai") == ("zai", "glm-5.3")
    opts = fc.get_model_options("glm-5.3", fc.find_channel("workbuddy", chans))
    assert all(o["value"].startswith("workbuddy::") for o in opts)
    assert any(o["text"]["content"].startswith("✓") for o in opts)


def test_unknown_channel_falls_back_to_catalog_current():
    assert fc.pick_channel("does-not-exist", fc.normalize_channels())["slug"] == "antigravity-manager"


# ── card structure ────────────────────────────────────────────────────────
def _form(card):
    return [e for e in card["body"]["elements"] if e["tag"] == "form"][0]["elements"]


def test_new_topic_card_structure():
    card = fc.build_new_topic_card(current_model="gemini-3.8-flash-high", current_reasoning="xhigh",
                                   current_provider="antigravity-manager", chat_id="oc_x")
    elems = _form(card)
    assert [e["tag"] for e in elems] == ["input", "select_static", "select_static", "button"]
    assert elems[0]["name"] == "topic_name"
    assert all(v["value"].startswith("antigravity-manager::") for v in elems[1]["options"])
    assert elems[2]["initial_option"] == "xhigh"
    assert elems[3]["value"]["hermes_feishu_form_action"] == "new_topic"
    assert elems[3]["value"]["chat_id"] == "oc_x"


def test_switch_card_shows_live_state_and_channel_tabs():
    card = fc.build_model_switch_card(current_model="hy4free", current_reasoning={"enabled": False},
                                      current_provider="workbuddy", session_key="sk1")
    intro = card["body"]["elements"][0]["content"]
    assert "hy4free" in intro and "WorkBuddy" in intro and "none" in intro
    tabs = card["body"]["elements"][1]["columns"][0]["elements"]
    assert tabs[0]["value"]["hermes_feishu_form_action"] == "select_channel"
    reasoning = [e for e in _form(card) if e["name"] == "reasoning_select"][0]
    assert reasoning["initial_option"] == "none"   # 原实现会漏成空


def test_viewing_other_channel_does_not_claim_current_model():
    card = fc.build_model_switch_card(current_model="hy4free", current_provider="workbuddy",
                                      session_key="sk1", view_provider="deepseek")
    opts = [e for e in _form(card) if e["name"] == "model_select"][0]["options"]
    assert opts[0]["value"].startswith("deepseek::")
    # ✓ 只标在该渠道将要提交的那一项上，绝不把别的渠道的 hy4free 带过来标成当前
    checked = [o["value"] for o in opts if "✓" in o["text"]["content"]]
    assert checked == ["deepseek::deepseek-flash"]


def test_card_stays_within_feishu_limits():
    def count(node):
        if isinstance(node, dict):
            return (1 if "tag" in node else 0) + sum(count(v) for v in node.values())
        if isinstance(node, list):
            return sum(count(v) for v in node)
        return 0

    for slug in [c["slug"] for c in fc.normalize_channels()]:
        card = fc.build_model_switch_card("x", "high", slug, "sk", None)
        raw = json.dumps(card, ensure_ascii=False)
        assert count(card) < 200, slug
        assert len(raw.encode()) < 30000, slug


# ── form routing ──────────────────────────────────────────────────────────
class _FakeAsyncStore:
    def __init__(self):
        self.written = {}

    async def set_model_override(self, skey, override):
        self.written[skey] = override


class _Conv:
    model_override = None
    reasoning_override = None


class _State:
    conversation = _Conv()


class _FakeGw:
    def __init__(self):
        self.overrides = {}
        self.reasoning = {}
        self.evicted = []
        self.async_session_store = _FakeAsyncStore()
        self.session_store = SimpleNamespace(get_or_create_session=lambda src: None)

    def _session_state(self, skey):
        return _State()

    def _session_model_override(self, skey):
        return self.overrides.get(skey)

    def _rehydrate_session_model_override(self, skey):
        persisted = self.async_session_store.written.get(skey) or {}
        if persisted:
            self.overrides[skey] = {k: persisted.get(k) for k in ("model", "provider", "base_url")}

    def _evict_cached_agent(self, skey):
        self.evicted.append(skey)

    def _resolve_session_reasoning_config(self, source=None, session_key=None, model=""):
        return self.reasoning.get(session_key or "")

    def _apply_reasoning_selection(self, skey, platform_key, value, persist_global=False):
        from hermes_constants import parse_reasoning_effort
        parsed = parse_reasoning_effort(value)
        self.reasoning[skey] = None if value == "reset" else parsed
        self.evicted.append(skey)
        return f"思考等级已设为 {value}"

    def _session_key_for_source(self, source):
        return f"agent:main:feishu:dm:{source.chat_id}:{source.thread_id}"


class _Host(FeishuChoiceCardsMixin):
    def __init__(self, gw=None):
        self.submitted = []
        self.card = None
        self.patched = []
        self._last_model_picker_skey = ""
        self._picker_form_state = {}
        self.gw = gw or _FakeGw()

    def _picker_gw(self):
        return self.gw

    def _submit_on_loop(self, loop, coro):
        self.submitted.append(coro)

    def _card_response(self, card_data=None):
        self.card = card_data
        return card_data

    def _patch_card_message(self, message_id, card, *, delay=0.0):
        self.patched.append((message_id, card))

    async def get_chat_info(self, chat_id):
        return {"name": "chat", "type": "dm"}

    def build_source(self, **kw):
        return SimpleNamespace(**kw)

    def _session_key_for_source(self, source):
        return f"agent:main:feishu:dm:{source.chat_id}:{source.thread_id}"


def _event(*, value=None, form_value=None, name="", chat_id="oc_test", message_id="om_card"):
    action = SimpleNamespace(value=value, form_value=form_value, name=name)
    return SimpleNamespace(action=action,
                           context=SimpleNamespace(open_chat_id=chat_id, open_message_id=message_id))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_select_channel_rerenders_scoped_models():
    host = _Host()
    ev = _event(value={"hermes_feishu_form_action": "select_channel", "provider": "dashscope",
                       "card_type": "new_topic", "chat_id": "oc_test"},
                form_value={}, name="btn_chan_dashscope")
    card = host._handle_feishu_form_card_action(event=ev, action_value=ev.action.value, loop=None)
    opts = [e for e in _form(card) if e["name"] == "model_select"][0]["options"]
    assert [o["value"] for o in opts] == ["dashscope::qwen3.8-max", "dashscope::deepseek-v4.1-flash-1017"]
    assert host.card is card


def test_submit_new_topic_with_action_value_payload():
    host = _Host()
    ev = _event(value={"hermes_feishu_form_action": "new_topic", "chat_id": "oc_a", "provider": "workbuddy"},
                form_value={"topic_name": "量化计算",
                            "model_select": "workbuddy::glm-5.3", "reasoning_select": "high"})
    card = host._handle_feishu_form_card_action(event=ev, action_value=ev.action.value, loop=None)
    assert len(host.submitted) == 1
    assert "量化计算" in card["header"]["title"]["content"]
    assert "glm-5.3" in card["body"]["elements"][0]["content"]


def test_submit_new_topic_when_action_value_is_empty():
    host = _Host()
    ev = _event(value=None, form_value={"topic_name": "数据分析",
                                        "model_select": "dashscope::qwen3.8-max",
                                        "reasoning_select": "xhigh"}, name="submit_new_topic")
    card = host._handle_feishu_form_card_action(event=ev, action_value={}, loop=None)
    assert len(host.submitted) == 1
    assert "qwen3.8-max" in card["body"]["elements"][0]["content"]


def test_submit_empty_form_falls_back_to_button_name_and_channel_default():
    host = _Host()
    host._remember_form_state("om_card", chat_id="oc_b", view_provider="workbuddy",
                              model={"model": None, "provider": "workbuddy"})
    ev = _event(value=None, form_value={}, name="submit_new_topic", chat_id="oc_b")
    card = host._handle_feishu_form_card_action(event=ev, action_value={}, loop=None, form_value={})
    assert len(host.submitted) == 1
    assert "hy4free" in card["body"]["elements"][0]["content"]   # 渠道首个模型


def test_submit_without_catalog_is_rejected_honestly():
    """目录为空（例如配置全失效）时不许猜一个模型硬建话题。"""
    fc._CATALOG["providers"] = []
    fc._CATALOG["at"] = 0.0
    host = _Host()
    ev = _event(value=None, form_value={"topic_name": "空选"}, name="submit_new_topic")
    card = host._handle_feishu_form_card_action(event=ev, action_value={}, loop=None)
    assert host.submitted == []
    assert "未选择模型" in card["header"]["title"]["content"]


def test_route_write_is_verified_and_reports_failures(monkeypatch):
    from plugins.platforms.feishu import adapter_choice_cards as acc
    monkeypatch.setattr(acc, "_probe_provider_credentials", lambda p, m: {"api_key": "fake"})
    gw = _FakeGw()
    host = _Host(gw)
    ok, notes = _run(host._apply_route_to_session(
        gw, "sk1", model="glm-5.3", provider="workbuddy", reasoning="high"))
    assert ok, notes
    assert gw.async_session_store.written["sk1"]["model"] == "glm-5.3"
    assert gw.overrides["sk1"]["provider"] == "workbuddy"
    assert gw.reasoning["sk1"] == {"enabled": True, "effort": "high"}
    assert "sk1" in gw.evicted


def test_route_write_flags_unverified_route(monkeypatch):
    from plugins.platforms.feishu import adapter_choice_cards as acc
    monkeypatch.setattr(acc, "_probe_provider_credentials", lambda p, m: {"api_key": "fake"})

    class _DropGw(_FakeGw):
        def _rehydrate_session_model_override(self, skey):
            pass   # 模拟写入被静默丢弃

    gw = _DropGw()
    host = _Host(gw)
    ok, notes = _run(host._apply_route_to_session(gw, "sk2", model="x", provider="workbuddy"))
    assert ok is False
    assert any("未回读" in n for n in notes)


def test_model_switch_applies_and_patches_result(monkeypatch):
    from plugins.platforms.feishu import cardkit
    from plugins.platforms.feishu import adapter_choice_cards as acc
    monkeypatch.setattr(cardkit, "_api", lambda *a, **k: ({"code": 0}, 200))
    monkeypatch.setattr(acc, "_probe_provider_credentials", lambda p, m: {"api_key": "fake"})
    gw = _FakeGw()
    gw.overrides["sk3"] = {"model": "hy4free", "provider": "workbuddy"}
    host = _Host(gw)
    _run(host._apply_feishu_model_switch_from_form(
        chat_id="oc", session_key="sk3", model="deepseek-v4-pro", reasoning="medium",
        provider="workbuddy", form_message_id="om_card", form_state={}))
    assert gw.async_session_store.written["sk3"]["model"] == "deepseek-v4-pro"
    assert host.patched and host.patched[0][0] == "om_card"
    assert "已生效" in host.patched[0][1]["header"]["title"]["content"]


def test_topic_creation_binds_the_real_session_key(monkeypatch):
    from plugins.platforms.feishu import cardkit
    from plugins.platforms.feishu import adapter_choice_cards as acc
    monkeypatch.setattr(acc, "_probe_provider_credentials", lambda p, m: {"api_key": "fake"})

    def fake_api(method, path, body=None, params=None):
        if path == "/im/v1/messages":
            return {"code": 0, "data": {"message_id": "om_root"}}, 200
        if path == "/im/v1/messages/om_root/reply":
            return {"code": 0, "data": {"thread_id": "omt_new"}}, 200
        return {"code": 0}, 200

    monkeypatch.setattr(cardkit, "_api", fake_api)
    gw = _FakeGw()
    host = _Host(gw)
    _run(host._create_feishu_topic_from_form(
        chat_id="oc_z", topic_name="化学", model="qwen3.8-max", reasoning="high",
        provider="dashscope", form_message_id="om_form", form_state={}))
    skey = "agent:main:feishu:dm:oc_z:omt_new"
    assert gw.async_session_store.written[skey]["model"] == "qwen3.8-max"
    assert gw.overrides[skey]["provider"] == "dashscope"
    assert "就绪" in host.patched[0][1]["header"]["title"]["content"]
