"""Feishu Card 2.0 setup cards for /new and /model (豆花 2026-11-09 重写).

Model lists are NOT hardcoded. The single source is Hermes' own picker catalog
(``hermes_cli.model_switch_providers.list_picker_providers``) — the same call the
gateway makes for the native /model picker. A provider or model added to
config.yaml / exposed by an endpoint's /models shows up here with no code change.

Card rules:
- Channel tabs (buttons) outside the form; clicking one re-renders the card with
  that channel's models only (Feishu form selects send no change events).
- Model option values carry their provider (``<slug>::<model>``), so a submit
  always knows which channel the chosen model belongs to.
- Aggregators (OpenRouter / Nous) are reduced to one newest entry per model
  family, keeping their own curated order. No fixed name list is involved.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

REASONING_OPTIONS = [
    {"text": {"tag": "plain_text", "content": "ultra (极限推理)"}, "value": "ultra"},
    {"text": {"tag": "plain_text", "content": "xhigh (极高)"}, "value": "xhigh"},
    {"text": {"tag": "plain_text", "content": "high (高)"}, "value": "high"},
    {"text": {"tag": "plain_text", "content": "medium (中)"}, "value": "medium"},
    {"text": {"tag": "plain_text", "content": "low (低)"}, "value": "low"},
    {"text": {"tag": "plain_text", "content": "minimal (微量)"}, "value": "minimal"},
    {"text": {"tag": "plain_text", "content": "none (关闭思考)"}, "value": "none"},
    {"text": {"tag": "plain_text", "content": "reset (恢复默认)"}, "value": "reset"},
]

AGGREGATOR_SLUGS = {"openrouter", "custom:openrouter", "nous"}
AGGREGATOR_MAX = 30        # flagship rows kept per aggregator
CHANNEL_MAX_MODELS = 80    # hard cap per dropdown (card element budget is 200)
OPTION_SEP = "::"
_HIDDEN_SLUGS = {"moa"}    # MoA needs the native picker's own flow
_LABEL_HINTS = {"dashscope": "阿里云百炼"}  # cosmetic only; unknown slugs use catalog name

# Endpoints that are not chat models (TTS/ASR/embedding/image) break an agent turn.
_NON_CHAT = re.compile(r"(?:^|[-_/.])(?:tts|asr|realtime|embed\w*|rerank\w*|whisper|"
                       r"speech|transcri\w*|moderation|audio|image\w*|dall-e)(?:$|[-_/.:])",
                       re.I)
_VARIANT = re.compile(r"(?::(?:free|batch|beta|extended|thinking)$)|"
                      r"(?:-(?:fast|flex|batch)$)|^~", re.I)
_SNAPSHOT = re.compile(r"-(?:\d{4}|\d{6}|\d{8}|\d{4}-\d{2}-\d{2})$")
_VERSION = re.compile(r"v?\d+(?:\.\d+)*")

# ── catalog cache ──────────────────────────────────────────────────────────
# Card callbacks must answer Feishu within ~3 s, so they read this cache only.
# It is filled by the gateway's /model picker call and by load_catalog().
_CATALOG_TTL = 600.0
_CATALOG: dict[str, Any] = {"providers": None, "at": 0.0}
_CATALOG_LOCK = threading.Lock()


def remember_catalog(providers: Optional[list]) -> None:
    """Store a provider list produced by Hermes' picker catalog."""
    if providers:
        with _CATALOG_LOCK:
            _CATALOG["providers"] = list(providers)
            _CATALOG["at"] = time.time()


def cached_catalog(max_age: Optional[float] = None) -> Optional[list]:
    """Last remembered catalog (any age unless *max_age* is given)."""
    with _CATALOG_LOCK:
        provs, at = _CATALOG["providers"], _CATALOG["at"]
    if provs is None or (max_age is not None and time.time() - at > max_age):
        return None
    return provs


def load_catalog(current_provider: str = "", current_model: str = "",
                 force: bool = False) -> list:
    """Blocking read of Hermes' picker catalog (disk-cached catalogs, no live probes).

    Call from a worker thread (``asyncio.to_thread``); never from a card callback.
    """
    if not force:
        fresh = cached_catalog(max_age=_CATALOG_TTL)
        if fresh is not None:
            return fresh
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.model_switch_providers import list_picker_providers
        cfg = load_config_readonly() or {}
        try:
            from hermes_cli.config import get_compatible_custom_providers
            custom = get_compatible_custom_providers(cfg)
        except Exception:
            custom = cfg.get("custom_providers")
        raw_model_cfg = cfg.get("model")
        model_cfg: dict = raw_model_cfg if isinstance(raw_model_cfg, dict) else {}
        provs = list_picker_providers(
            current_provider=current_provider or model_cfg.get("provider") or "",
            current_base_url=model_cfg.get("base_url") or "",
            current_model=current_model or model_cfg.get("default") or "",
            user_providers=cfg.get("providers"), custom_providers=custom,
            excluded_providers=(cfg.get("model_catalog") or {}).get("excluded_providers"),
            max_models=None, include_moa=False, non_blocking_catalogs=True,
            probe_custom_providers=False, probe_current_custom_provider=True,
        )
    except Exception as exc:
        logger.warning("[Feishu] model catalog load failed: %s", exc, exc_info=True)
        return cached_catalog() or []
    remember_catalog(provs)
    return provs


def catalog_is_truncated(providers: Optional[list]) -> bool:
    """True when a non-aggregator row was capped (gateway passes max_models=50)."""
    for p in providers or []:
        slug = str(p.get("slug") or "")
        total = p.get("total_models")
        if slug not in AGGREGATOR_SLUGS and isinstance(total, int) and total > len(p.get("models") or []):
            return True
    return False


# ── normalisation ──────────────────────────────────────────────────────────
def _family_key(model_id: str) -> str:
    """Collapse version numbers and date snapshots: newest-first lists keep the first hit."""
    base = _SNAPSHOT.sub("", model_id.lower())
    return _VERSION.sub("#", base)


def _flagship_filter(models: list[str]) -> list[str]:
    """One entry per model family, preserving the aggregator's own curated order."""
    out, seen = [], set()
    for m in models:
        if _VARIANT.search(m):
            continue
        fam = _family_key(m)
        if fam in seen:
            continue
        seen.add(fam)
        out.append(m)
        if len(out) >= AGGREGATOR_MAX:
            break
    return out


def normalize_channels(providers: Optional[list] = None) -> list[dict[str, Any]]:
    """Catalog rows → ``[{slug, name, models, aggregator, current}]`` for the card."""
    rows = providers if providers else (cached_catalog() or [])
    slugs = {str(p.get("slug") or "") for p in rows}
    out: list[dict[str, Any]] = []
    for p in rows:
        slug = str(p.get("slug") or "")
        if not slug or slug in _HIDDEN_SLUGS:
            continue
        if slug == "custom:openrouter" and "openrouter" in slugs:
            continue  # same service twice; the native row carries the curated list
        models = [str(m.get("id") if isinstance(m, dict) else m) for m in (p.get("models") or [])]
        models = [m for m in models if m and not _NON_CHAT.search(m)]
        aggregator = slug in AGGREGATOR_SLUGS
        models = _flagship_filter(models) if aggregator else models[:CHANNEL_MAX_MODELS]
        if not models:
            continue
        out.append({"slug": slug, "name": str(p.get("name") or slug), "models": models,
                    "aggregator": aggregator, "current": bool(p.get("is_current"))})
    return out


def find_channel(slug: Optional[str], channels: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for ch in channels:
        if ch["slug"] == slug:
            return ch
    return None


def get_channel_label(provider: Optional[str], channels: Optional[list] = None) -> str:
    """Display name: cosmetic hint > catalog name > slug."""
    if provider in _LABEL_HINTS:
        return _LABEL_HINTS[provider]
    ch = find_channel(provider, channels if channels is not None else normalize_channels())
    return ch["name"] if ch else (provider or "默认渠道")


def pick_channel(provider: Optional[str], channels: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Requested channel, else the catalog's current one, else the first."""
    return (find_channel(provider, channels)
            or next((c for c in channels if c["current"]), None)
            or (channels[0] if channels else None))


def default_model_for(provider: Optional[str], channels: Optional[list] = None,
                      current_model: Optional[str] = None) -> str:
    """Keep *current_model* when the channel lists it, else the channel's first model."""
    chs = channels if channels is not None else normalize_channels()
    ch = find_channel(provider, chs)
    if ch is None:
        return current_model or ""
    if current_model and current_model in ch["models"]:
        return current_model
    return ch["models"][0]


def encode_option(slug: str, model: str) -> str:
    return f"{slug}{OPTION_SEP}{model}"


def decode_option(value: Optional[str], fallback_provider: str = "") -> tuple[str, str]:
    """``slug::model`` → (slug, model); a bare model keeps *fallback_provider*."""
    value = str(value or "").strip()
    if OPTION_SEP in value:
        slug, model = value.split(OPTION_SEP, 1)
        return slug.strip(), model.strip()
    return fallback_provider, value


def reasoning_effort_value(reasoning: Any) -> Optional[str]:
    """``{'enabled': False}`` → 'none'; ``{'effort': 'high'}`` / 'high' → 'high'."""
    if isinstance(reasoning, dict):
        if reasoning.get("enabled") is False:
            return "none"
        return reasoning.get("effort") or None
    return str(reasoning).strip().lower() or None if reasoning else None


# ── option builders ────────────────────────────────────────────────────────
def _opt(text: str, value: str) -> dict[str, Any]:
    return {"text": {"tag": "plain_text", "content": text[:60]}, "value": value}


def get_model_options(selected_model: Optional[str], channel: Optional[dict[str, Any]],
                      *, include_current: Optional[bool] = None) -> list[dict[str, Any]]:
    """Dropdown rows for ONE channel; values are ``slug::model``.

    ``selected_model`` is the row that will be submitted if the user does not touch the
    dropdown — it gets the ✓ mark. ``include_current`` defaults to "selected_model is not in
    the channel list" (the session's live model stays selectable even if the catalog lacks it).
    """
    if channel is None:
        return []
    slug, models = channel["slug"], list(channel["models"])
    if include_current is None:
        include_current = bool(selected_model) and selected_model not in models
    if include_current and selected_model and selected_model not in models:
        models.insert(0, selected_model)
    return [_opt(f"✓ {m}" if m == selected_model else m, encode_option(slug, m)) for m in models]


def _reasoning_options(current: Optional[str]) -> list[dict[str, Any]]:
    out = []
    for opt in REASONING_OPTIONS:
        val, txt = opt["value"], opt["text"]["content"]
        out.append(_opt(f"✓ {txt}" if val == current else txt, val))
    return out


def _channel_buttons(channels: list[dict[str, Any]], active: str, card_type: str, *,
                     chat_id: str = "", session_key: str = "", topic_name: str = "",
                     reasoning: str = "") -> list[dict[str, Any]]:
    btns = []
    for ch in channels:
        is_cur = ch["slug"] == active
        label = f"{get_channel_label(ch['slug'], channels)} ({len(ch['models'])})"
        btns.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": ("✓ " if is_cur else "") + label},
            "type": "primary" if is_cur else "default",
            "width": "fill",
            "name": f"btn_chan_{re.sub(r'[^A-Za-z0-9_]', '_', ch['slug'])}"[:60],
            "value": {"hermes_feishu_form_action": "select_channel", "provider": ch["slug"],
                      "card_type": card_type, "chat_id": chat_id, "session_key": session_key,
                      "topic_name": topic_name, "reasoning": reasoning},
        })
    return btns


# ── card builders ──────────────────────────────────────────────────────────
def _setup_card(*, card_type: str, header: str, intro: str, current_model: Optional[str],
                current_reasoning: Any, current_provider: Optional[str], providers: Optional[list],
                chat_id: str = "", session_key: str = "", topic_name: str = "",
                view_provider: Optional[str] = None) -> dict[str, Any]:
    channels = normalize_channels(providers)
    ch = pick_channel(view_provider or current_provider, channels)
    slug = ch["slug"] if ch else (view_provider or current_provider or "")
    # The session's model is only "current" inside the channel it actually runs on.
    live_model = current_model if ch and (view_provider or current_provider) == slug else None
    # 会话正在用的模型：只有它属于本渠道时才当作「当前项」，否则用本渠道第一个模型
    in_channel = bool(ch and live_model and live_model in ch["models"])
    active_m = (live_model if in_channel else default_model_for(slug, channels)) if ch else (live_model or "")
    effort = reasoning_effort_value(current_reasoning)
    model_opts = get_model_options(active_m, ch, include_current=bool(live_model) and not in_channel)
    btns = _channel_buttons(channels, slug, card_type, chat_id=chat_id, session_key=session_key,
                            topic_name=topic_name, reasoning=effort or "")
    half = (len(btns) + 1) // 2
    lbl = get_channel_label(slug, channels)

    form: list[dict[str, Any]] = []
    if card_type == "new_topic":
        form.append({"tag": "input", "name": "topic_name", "required": False,
                     "default_value": topic_name or "",
                     "placeholder": {"tag": "plain_text", "content": "话题名称（如：量化计算）"}})
    if model_opts:
        selected = encode_option(slug, active_m or "")
        form.append({"tag": "select_static", "name": "model_select", "options": model_opts,
                     "initial_option": selected,
                     "placeholder": {"tag": "plain_text", "content": f"{lbl} 的模型"}})
    reasoning_sel = {"tag": "select_static", "name": "reasoning_select",
                     "options": _reasoning_options(effort),
                     "placeholder": {"tag": "plain_text", "content": "思考等级（默认）"}}
    if effort in {o["value"] for o in REASONING_OPTIONS}:
        reasoning_sel["initial_option"] = effort  # never send null: Feishu rejects it
    form.append(reasoning_sel)
    submit_name = "submit_new_topic" if card_type == "new_topic" else "submit_model_switch"
    form.append({"tag": "button", "type": "primary", "form_action_type": "submit",
                 "name": submit_name,
                 "text": {"tag": "plain_text",
                          "content": "创建话题" if card_type == "new_topic" else "确认切换"},
                 "value": {"hermes_feishu_form_action": card_type, "chat_id": chat_id,
                           "session_key": session_key, "provider": slug}})

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": intro}]
    if btns:
        elements.append({"tag": "column_set", "flex_mode": "none", "background_style": "grey",
                         "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                                      "elements": btns[:half]},
                                     {"tag": "column", "width": "weighted", "weight": 1,
                                      "elements": btns[half:]}]})
    else:
        elements.append({"tag": "markdown",
                         "content": "<font color='red'>未读到任何可用渠道，请检查 config.yaml 的 providers。</font>"})
    elements += [{"tag": "hr"}, {"tag": "form", "name": f"{card_type}_form", "elements": form}]
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": header}, "template": "blue"},
            "body": {"elements": elements}}


def build_new_topic_card(current_model: Optional[str] = None, current_reasoning: Any = None,
                         current_provider: Optional[str] = None, chat_id: Optional[str] = None,
                         providers: Optional[list] = None,
                         topic_name_init: Optional[str] = None,
                         view_provider: Optional[str] = None) -> dict[str, Any]:
    """/new: topic name + in-channel model + reasoning."""
    return _setup_card(
        card_type="new_topic", header="新建话题",
        intro="**选择渠道**（按钮切换后，下方只列该渠道的模型）",
        current_model=current_model, current_reasoning=current_reasoning,
        current_provider=current_provider, providers=providers,
        chat_id=chat_id or "", topic_name=topic_name_init or "",
        view_provider=view_provider)


def build_model_switch_card(current_model: Optional[str] = None, current_reasoning: Any = None,
                            current_provider: Optional[str] = None,
                            session_key: Optional[str] = None,
                            providers: Optional[list] = None,
                            view_provider: Optional[str] = None) -> dict[str, Any]:
    """/model: shows what the session runs NOW, then the in-channel picker."""
    channels = normalize_channels(providers)
    effort = reasoning_effort_value(current_reasoning) or "默认"
    intro = (f"**当前生效**：`{current_model or '默认'}` · "
             f"{get_channel_label(current_provider, channels)} · 思考 `{effort}`\n"
             "**选择渠道**（按钮切换后，下方只列该渠道的模型）")
    return _setup_card(
        card_type="switch_model", header="切换模型与思考等级", intro=intro,
        current_model=current_model, current_reasoning=current_reasoning,
        current_provider=current_provider, providers=providers, session_key=session_key or "",
        view_provider=view_provider)


def build_settled_card(title: str, message: str, template: str = "green") -> dict[str, Any]:
    """Result card shown in place of the form after a submit."""
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title[:80]}, "template": template},
            "body": {"elements": [{"tag": "markdown", "content": message}]}}
