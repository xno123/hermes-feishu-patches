"""Feishu clarify cards and the finite-choice picker card.

Mixed into ``FeishuAdapter``: the ``send_clarify`` / ``retire_clarify_card`` / ``send_choice_picker``
capabilities from ``BasePlatformAdapter`` plus their card-action handlers. The card builders,
``_send_interactive_card``, ``_validate_card_action`` and ``_card_response`` live on the facade, so
every click passes the same operator authorization and chat check as an exec-approval click before
it answers anything.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Optional

from agent.i18n import t
from gateway.platforms.helpers import prompt_id_counter
from gateway.platforms.base import SendResult, describe_proxy_error, env_proxy_for_redaction
from plugins.platforms.feishu.feishu_setup_cards import cached_catalog, reasoning_effort_value

logger = logging.getLogger(__name__)

_BUTTON_LABEL_MAX = 40


def _probe_provider_credentials(provider: str, model: str) -> dict:
    """Resolve a provider's runtime credentials the same way a turn would.

    Kept module-level so tests can stub it without importing ``gateway.run``.
    """
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    return _resolve_runtime_agent_kwargs_for_provider(provider, target_model=model)


class FeishuChoiceCardsMixin:
    """Clarify and choice-picker cards for ``FeishuAdapter`` (needs its card/state helpers)."""

    # Card-action value key -> handler; the first key a click's value carries routes it.
    _CARD_ACTION_ROUTES = (
        ("hermes_action", "_handle_approval_card_action"),
        ("hermes_update_prompt_action", "_handle_update_prompt_card_action"),
        ("hermes_clarify_action", "_handle_clarify_card_action"),
        ("hermes_choice_picker_action", "_handle_choice_picker_card_action"),
        ("hermes_feishu_form_action", "_handle_feishu_form_card_action"),
    )

    def _init_choice_card_state(self) -> None:
        self._picker_form_state: dict[str, dict[str, Any]] = {}
        # clarify_id -> {session_key, message_id, chat_id, question}
        self._clarify_state: dict[str, dict[str, str]] = {}
        # picker_id -> {session_key, message_id, chat_id, body, choices, on_choice_selected, metadata}
        self._choice_picker_state: dict[int, dict[str, Any]] = {}
        self._choice_picker_counter = prompt_id_counter()

    # --- clarify ---
    async def send_clarify(
        self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
        session_key: str, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Multiple choice: a card with one numbered button per option plus "Other" (flips the entry
        to text capture). Open-ended: the base text prompt, answered by the next message."""
        if not choices:
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        if not self._client:
            return SendResult(success=False, error="Not connected")
        from plugins.platforms.feishu.adapter import _card_button
        from tools.clarify_gateway import get_pending, mark_awaiting_text

        lines = [t("gateway.clarify.option_line", index=i, choice=c) for i, c in enumerate(choices, start=1)]
        body = f"{question}\n\n" + "\n".join(lines)
        entry = get_pending(clarify_id)
        if entry is not None and entry.multi_select:
            # A tap answers one option; several (or a free-form answer, as the hint says) are typed,
            # so typed text must be captured as the answer like the base text prompt does.
            body += "\n\n" + t("gateway.clarify.hint_multi")
            mark_awaiting_text(clarify_id)
        actions = [
            _card_button(str(i), "primary" if i == 1 else "default",
                         {"hermes_clarify_action": str(i - 1), "clarify_id": clarify_id})
            for i in range(1, len(choices) + 1)]
        actions.append(_card_button(t("platform.feishu.clarify.other"), "default",
                                    {"hermes_clarify_action": "other", "clarify_id": clarify_id}))
        card = self._clarify_card(body, "blue", actions=actions)
        try:
            return await self._send_interactive_card(
                chat_id, card, metadata, "send_clarify failed", state_map=self._clarify_state,
                state_id=clarify_id, session_key=session_key, extra_state={"question": str(question)})
        except Exception as exc:
            logger.warning("[Feishu] send_clarify failed: %s", (shown := describe_proxy_error(exc, env_proxy_for_redaction(f"https://open.{'larksuite.com' if self._domain_name == 'lark' else 'feishu.cn'}"))) or exc, exc_info=not shown)
            return SendResult(success=False, error=shown or str(exc))

    @staticmethod
    def _clarify_card(body: str, template: str, *, actions: Optional[list] = None) -> dict[str, Any]:
        from plugins.platforms.feishu.adapter import _card
        card = _card(f"❓ {t('platform.feishu.clarify.title')}", template, body, actions=actions)
        # Shared card: PATCH (retire_clarify_card) only reaches every recipient when the sent AND
        # the updated card both declare it.
        card["config"]["update_multi"] = True
        return card

    @classmethod
    def _settled_clarify_card(cls, state: dict[str, str], line: str, template: str) -> dict[str, Any]:
        return cls._clarify_card(f"{state.get('question', '')}\n\n{line}", template)

    def _handle_clarify_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
        """Answer a clarify from a tap and return the settled card inline (syncs every client).
        ``resolve_gateway_clarify`` is a locked Event set, safe from the SDK callback thread."""
        clarify_id = str(action_value.get("clarify_id") or "")
        state = self._clarify_state.get(clarify_id)
        if not state:
            logger.debug("[Feishu] Clarify %s already resolved or unknown", clarify_id or "<missing>")
            return self._card_response()
        checked = self._validate_card_action(event=event, state=state, label="clarify", ident=clarify_id)
        if checked is None:
            return self._card_response()
        open_id, _chat_id, user_name, user_id, _union_id = checked
        from gateway.approval_actor import clarify_refusal, refusal_text
        # SessionSource.user_id is the tenant user_id when the app can see it, else the open_id.
        if requester := clarify_refusal(clarify_id, user_id or open_id, extra=self.config.extra):
            return self._card_toast(refusal_text(requester))  # only the clicker sees it; card unchanged
        from tools import clarify_gateway

        entry = clarify_gateway.get_pending(clarify_id)
        token = str(action_value.get("hermes_clarify_action") or "")
        if entry is None or entry.session_key != state.get("session_key"):
            # The wait ended (timeout, /new) before the retire reached this card.
            self._clarify_state.pop(clarify_id, None)
            return self._card_response(self._settled_clarify_card(state, t("gateway.clarify.expired"), "grey"))
        if token == "other":
            # State stays so the typed answer's retire_clarify_card can settle this card.
            if not clarify_gateway.mark_awaiting_text(clarify_id):
                return self._card_response(self._settled_clarify_card(state, t("gateway.clarify.expired"), "grey"))
            awaiting = t("platform.feishu.clarify.awaiting_typed", user=user_name)
            return self._card_response(self._settled_clarify_card(state, f"*{awaiting}*", "blue"))
        choices = list(entry.choices or [])
        if not (token.isdigit() and int(token) < len(choices)):
            logger.debug("[Feishu] Clarify %s has invalid choice token=%r", clarify_id, token)
            return self._card_response()
        if self._clarify_state.pop(clarify_id, None) is None:
            return self._card_response()  # a concurrent tap won
        answer = str(choices[int(token)])
        if not clarify_gateway.resolve_gateway_clarify(clarify_id, answer):
            return self._card_response(self._settled_clarify_card(state, t("gateway.clarify.expired"), "grey"))
        logger.info("Feishu clarify button resolved (id=%s, choice_index=%s, user=%s)", clarify_id, token, user_id or open_id)
        logger.debug("Feishu clarify choice text (id=%s): %.100r", clarify_id, answer)
        return self._card_response(self._settled_clarify_card(state, f"**{user_name}:** {answer}", "green"))

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Gateway hook for a clarify that ended without a tap (timeout, reset, superseding prose,
        typed answer): rewrite the card button-less. The state pop runs before the first await, so a
        tap racing this call is refused."""
        state = self._clarify_state.pop(clarify_id, None)
        if not state or not state.get("message_id") or not self._client:
            return
        from plugins.platforms.feishu import adapter as facade

        card = self._settled_clarify_card(state, notice, "grey")
        body = facade._sdk_build(facade.PatchMessageRequestBody, content=json.dumps(card, ensure_ascii=False))
        request = facade._sdk_build(facade.PatchMessageRequest, message_id=state["message_id"], request_body=body)
        response = await self._run_blocking(self._client.im.v1.message.patch, request)
        if not self._response_succeeded(response):
            logger.debug("[Feishu] Retiring clarify card %s rejected: code=%s", clarify_id, getattr(response, "code", None))

    # --- finite-choice picker ---
    async def send_choice_picker(
        self, chat_id: str, title: str, choices: list, session_key: str, on_choice_selected,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """One-tap picker card for /reasoning, /fast, etc.; choice dicts are
        ``{"value", "label", "is_current"}``. One live picker per chat, like Telegram."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not choices:
            return SendResult(success=False, error="No choices")
        from plugins.platforms.feishu.adapter import _card_button

        picker_id = next(self._choice_picker_counter)
        actions = []
        for i, choice in enumerate(choices):
            label = str(choice.get("label") or choice.get("value") or "")
            label = self._truncate_preview(f"✓ {label}" if choice.get("is_current") else label, _BUTTON_LABEL_MAX, "…")
            actions.append(_card_button(label, "primary" if choice.get("is_current") else "default",
                                        {"hermes_choice_picker_action": str(i), "picker_id": picker_id}))
        heading, _, rest = str(title or "").partition("\n")
        header = heading.replace("*", "").strip()
        # A one-line title (usage-limit offer) gets the full labels as body: buttons truncate them.
        body = rest.strip() or "\n".join(f"• {c.get('label') or c.get('value') or ''}" for c in choices)
        try:
            result = await self._send_interactive_card(
                chat_id, self._picker_card(header, "blue", body, actions=actions), metadata, "send_choice_picker failed",
                state_map=self._choice_picker_state, state_id=picker_id, session_key=session_key,
                extra_state={"header": header, "body": body, "choices": choices,
                             "on_choice_selected": on_choice_selected, "metadata": metadata})
        except Exception as exc:
            logger.warning("[Feishu] send_choice_picker failed: %s", (shown := describe_proxy_error(exc, env_proxy_for_redaction(f"https://open.{'larksuite.com' if self._domain_name == 'lark' else 'feishu.cn'}"))) or exc, exc_info=not shown)
            return SendResult(success=False, error=shown or str(exc))
        if result.success:
            # Snapshot: the SDK callback thread pops from this dict concurrently.
            for stale in [k for k, v in list(self._choice_picker_state.items()) if k != picker_id and v["chat_id"] == chat_id]:
                self._choice_picker_state.pop(stale, None)
        return result

    def _handle_choice_picker_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
        """Settle the picker card inline and run the selection's applier on the adapter loop."""
        picker_id = action_value.get("picker_id")
        state = self._choice_picker_state.get(picker_id)
        if not state:
            logger.debug("[Feishu] Choice picker %s already resolved or unknown", picker_id)
            return self._card_response()
        checked = self._validate_card_action(event=event, state=state, label="choice picker", ident=picker_id)
        if checked is None:
            return self._card_response()
        from gateway.approval_actor import picker_refusal, refusal_text
        if requester := picker_refusal(state["on_choice_selected"], checked[3] or checked[0], extra=self.config.extra):
            return self._card_toast(refusal_text(requester))  # another member's tap: only they see it, card stays open
        token = str(action_value.get("hermes_choice_picker_action") or "")
        choices = state["choices"]
        if not (token.isdigit() and int(token) < len(choices)):
            return self._card_response()
        if self._choice_picker_state.pop(picker_id, None) is None:
            return self._card_response()  # a concurrent tap won
        choice = choices[int(token)]
        if not self._submit_on_loop(loop, self._apply_choice_picker_selection(state, str(choice.get("value") or ""))):
            self._choice_picker_state[picker_id] = state
            return self._card_response()
        label = str(choice.get("label") or choice.get("value") or "")
        return self._card_response(self._picker_card(state["header"], "green", f"{state['body']}\n\n✓ **{label}** — {checked[2]}"))

    @staticmethod
    def _picker_card(header: str, template: str, body: str, *, actions: Optional[list] = None) -> dict[str, Any]:
        from plugins.platforms.feishu.adapter import _card
        card = _card(header, template, body, actions=actions)
        card["config"]["update_multi"] = True  # shared: the inline settle reaches the whole group
        return card

    def _on_webhook_card_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A card tap over HTTP, handled as the SDK delivers it: the button value stays a dict (the
        routes key on it) and the settled card goes back as the response body."""
        data = self._namespace_from_mapping(payload)
        action = getattr(getattr(data, "event", None), "action", None)
        if action is not None:
            action.value = ((payload.get("event") or {}).get("action") or {}).get("value")
        response = self._on_card_action_trigger(data)
        body: dict[str, Any] = {}
        if (card := getattr(response, "card", None)) is not None:
            body["card"] = {"type": card.type, "data": card.data}
        if (toast := getattr(response, "toast", None)) is not None:  # a refusal only the clicker sees
            body["toast"] = {"type": toast.type, "content": toast.content}
        return body

    async def _apply_choice_picker_selection(self, state: dict[str, Any], value: str) -> None:
        """Run the picker's applier and send its confirmation to the chat."""
        try:
            result_text = await state["on_choice_selected"](state["chat_id"], value)
        except Exception as exc:
            logger.exception("[Feishu] Choice picker selection failed: %s", exc)
            result_text = t("platform.feishu.picker.apply_error", error=str(exc))
        if result_text:
            await self.send(state["chat_id"], str(result_text), metadata=state.get("metadata"))

    #
    # 2026-11-09 重写要点（全部依据实测，不再手搓 session 状态）：
    #   1. 模型/渠道一律来自 Hermes 自己的 picker 目录（list_picker_providers），不再有硬编码表；
    #   2. 新建话题用 gateway._session_key_for_source() 生成会话键，与入站消息完全一致；
    #   3. 路由写入走 store.set_model_override({"model","provider"}) + 清空内存键让
    #      _rehydrate_session_model_override 按 provider 重新解析凭据（与重启后同一路径），
    #      写完立刻回读校验，失败如实报错，不再"点了就算成功"；
    #   4. /model 提交优先走 gateway 原生 picker 回调（校验/DB/笔记/全局语义都一致）；
    #   5. 思考等级走 gateway._apply_reasoning_selection（与 /reasoning 同一实现）。

    def _picker_gw(self):
        handler = getattr(self, "_message_handler", None)
        return getattr(handler, "__self__", None) or getattr(self, "gateway_runner", None)

    def _session_route_now(self, session_key: str, fallback_provider: str) -> dict[str, Any]:
        """Current model/provider/reasoning of a session (authoritative: the gateway)."""
        gw = self._picker_gw()
        override: dict = {}
        reasoning = None
        if gw is not None:
            try:
                override = gw._session_model_override(session_key) or {}
            except Exception:
                override = {}
            if session_key:
                try:
                    reasoning = gw._resolve_session_reasoning_config(session_key=session_key)
                except Exception:
                    pass
        return {
            "model": override.get("model") or None,
            "provider": override.get("provider") or fallback_provider,
            "reasoning": reasoning_effort_value(reasoning),
        }

    def _remember_form_state(self, message_id: Optional[str], **state: Any) -> None:
        if not message_id:
            return
        data = getattr(self, "_picker_form_state", None)
        if data is None:
            data = self._picker_form_state = {}
        data[message_id] = state
        while len(data) > 50:
            data.pop(next(iter(data)), None)

    def _form_state(self, message_id: Optional[str]) -> dict[str, Any]:
        data = getattr(self, "_picker_form_state", {}) or {}
        return dict(data.get(message_id) or {}) if message_id else {}

    async def send_new_topic_picker(self, chat_id: str, *,
                                    metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """/new card: channels and models come from the live catalog, never a fixed table."""
        from plugins.platforms.feishu.feishu_setup_cards import (
            build_new_topic_card, catalog_is_truncated, load_catalog,
        )

        gw = self._picker_gw()
        route = {"model": None, "provider": None, "reasoning": None}
        if gw is not None:
            try:
                route["model"], runtime = await asyncio.to_thread(
                    gw._resolve_session_agent_runtime, source=None, session_key=None)
                route["provider"] = (runtime or {}).get("provider")
            except Exception:
                pass
            try:
                route["reasoning"] = await asyncio.to_thread(gw._resolve_session_reasoning_config)
            except Exception:
                pass
        providers = await asyncio.to_thread(
            load_catalog, route["provider"] or "", route["model"] or "")
        if catalog_is_truncated(providers):
            providers = await asyncio.to_thread(load_catalog, route["provider"] or "",
                                                route["model"] or "", True)
        card = build_new_topic_card(
            current_model=route["model"], current_reasoning=route["reasoning"],
            current_provider=route["provider"], chat_id=chat_id, providers=providers)
        response = await self._feishu_send_with_retry(
            chat_id=chat_id, msg_type="interactive",
            payload=json.dumps(card, ensure_ascii=False),
            reply_to=None, metadata=metadata or {"thread_reply": False})
        result = self._finalize_send_result(response, "send_new_topic_picker failed")
        self._remember_form_state(
            getattr(result, "message_id", None),
            chat_id=chat_id, model={"model": route["model"], "provider": route["provider"]},
            reasoning_effort=route["reasoning"], view_provider=route["provider"])
        return result

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive Form card for /model command."""
        from plugins.platforms.feishu.feishu_setup_cards import (
            build_model_switch_card, catalog_is_truncated, load_catalog, remember_catalog,
        )
        remember_catalog(providers)  # card callbacks reply from this cache
        if catalog_is_truncated(providers):
            providers = await asyncio.to_thread(load_catalog, current_provider, current_model, True)
        route = self._session_route_now(session_key, current_provider)
        current_model = route["model"] or current_model
        current_provider = route["provider"] or current_provider

        card = build_model_switch_card(
            current_model=current_model,
            current_reasoning=route["reasoning"],
            current_provider=current_provider,
            session_key=session_key,
            providers=providers,
        )
        if not hasattr(self, "_model_picker_callbacks"):
            self._model_picker_callbacks = {}
        self._model_picker_callbacks[session_key] = on_model_selected
        self._last_model_picker_skey = session_key  # 表单提交时 action.value 可能为空，兜底用

        reply_to = self._resolve_reply_anchor(metadata) if hasattr(self, "_resolve_reply_anchor") else None
        response = await self._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=json.dumps(card, ensure_ascii=False),
            reply_to=reply_to,
            metadata=metadata,
        )
        result = self._finalize_send_result(response, "send_model_picker failed")
        self._remember_form_state(
            getattr(result, "message_id", None), chat_id=chat_id, session_key=session_key,
            model={"model": current_model, "provider": current_provider},
            reasoning_effort=route["reasoning"], view_provider=current_provider)
        return result

    def _handle_feishu_form_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any,
                                        form_value: Optional[dict[str, Any]] = None) -> Any:
        """Handle Feishu Card 2.0 form submissions for /new topic and /model switch.

        飞书**表单提交**时 SDK 的 ``action.value`` 常常为空，真正的数据在 ``action.form_value``，
        按钮 payload 也不一定回传。三级推断：value → 按钮 name → form_value 字段。
        卡片上一版的选择从 ``_picker_form_state[open_message_id]`` 回读（action.value 不可靠）。
        """
        from plugins.platforms.feishu.feishu_setup_cards import (
            build_settled_card, build_new_topic_card, build_model_switch_card,
            decode_option, get_channel_label, normalize_channels, pick_channel,
        )

        action = getattr(event, "action", None)
        if not isinstance(form_value, dict) or not form_value:
            form_value = getattr(action, "form_value", None) or {}
            if not isinstance(form_value, dict):
                form_value = {}
        if not form_value:
            op = getattr(event, "operator", None)
            cand = getattr(op, "form_value", None) if op is not None else None
            if isinstance(cand, dict):
                form_value = cand

        btn_name = str(getattr(action, "name", "") or "")
        context = getattr(event, "context", None)
        chat_id = (str((action_value or {}).get("chat_id") or "")
                   or str(getattr(context, "open_chat_id", "") or ""))
        card_message_id = str(getattr(context, "open_message_id", "") or "")
        state = self._form_state(card_message_id)
        logger.info("[Feishu] Form submit: value=%r name=%r keys=%r card_msg=%s",
                    action_value if isinstance(action_value, dict) else type(action_value).__name__,
                    btn_name, list(form_value), card_message_id)

        action_type = str((action_value or {}).get("hermes_feishu_form_action") or "").strip()
        if not action_type:
            low = btn_name.lower()
            if "chan" in low:
                action_type = "select_channel"
            elif "new" in low:
                action_type = "new_topic"
            elif "model" in low or "switch" in low:
                action_type = "switch_model"
            elif "topic_name" in form_value:
                action_type = "new_topic"
            elif "model_select" in form_value:
                action_type = "switch_model"

        # ── 渠道 Tab：只看不改，重绘成该渠道的模型列表 ──
        if action_type == "select_channel":
            view = str((action_value or {}).get("provider") or "").strip()
            card_type = str((action_value or {}).get("card_type") or "").strip()
            if not card_type:
                card_type = "switch_model" if state.get("session_key") else "new_topic"
            topic_name = str((action_value or {}).get("topic_name") or state.get("topic_name") or "")
            reasoning = str((action_value or {}).get("reasoning") or state.get("reasoning_effort") or "")
            providers = cached_catalog()

            if card_type == "switch_model":
                target_skey = str((action_value or {}).get("session_key") or state.get("session_key") or "")
                route = self._session_route_now(target_skey, "")
                new_card = build_model_switch_card(
                    current_model=route["model"],
                    current_reasoning=reasoning or route["reasoning"],
                    current_provider=route["provider"],
                    session_key=target_skey or None,
                    providers=providers, view_provider=view or None,
                )
            else:
                new_card = build_new_topic_card(
                    current_model=(state.get("model") or {}).get("model"),
                    current_reasoning=reasoning or state.get("reasoning_effort"),
                    current_provider=(state.get("model") or {}).get("provider"),
                    chat_id=chat_id,
                    topic_name_init=topic_name or None,
                    providers=providers, view_provider=view or None,
                )
            if card_message_id and view:
                self._remember_form_state(card_message_id, **{**state, "view_provider": view})
            return self._card_response(new_card)

        # ── 新建话题 ──
        if action_type == "new_topic":
            import time
            channels = normalize_channels(cached_catalog())
            view = str(state.get("view_provider") or "")
            ch = pick_channel(view, channels)
            fallback_provider = str((action_value or {}).get("provider") or (ch or {}).get("slug") or "")
            provider, model = decode_option(form_value.get("model_select"), fallback_provider)
            if not model and ch is not None:
                model = ch["models"][0]
            reasoning = str(form_value.get("reasoning_select") or "").strip()
            topic_name = (str(form_value.get("topic_name") or "").strip()
                          or f"新话题 {time.strftime('%m-%d %H:%M')}")
            if not model or not provider:
                return self._card_response(build_settled_card(
                    "⚠️ 未选择模型", "请先点选一个渠道，再在下拉框里选具体模型。", template="orange"))

            self._submit_on_loop(loop, self._create_feishu_topic_from_form(
                chat_id=chat_id, topic_name=topic_name, model=model,
                reasoning=reasoning, provider=provider,
                form_message_id=card_message_id, form_state=state,
            ))
            settled = build_settled_card(
                f"话题【{topic_name}】正在创建",
                f"**话题名称**：{topic_name}\n"
                f"**渠道来源**：`{get_channel_label(provider, channels)}`\n"
                f"**执行模型**：`{model}`\n"
                f"**思考等级**：`{reasoning or '默认'}`\n\n"
                f"正在建立话题并写入会话配置，几秒内会在本卡更新实际生效结果。",
                template="blue")
            return self._card_response(settled)

        # ── 切换模型 ──
        if action_type == "switch_model":
            view = str(state.get("view_provider") or "")
            channels = normalize_channels(cached_catalog())
            fallback_provider = str((action_value or {}).get("provider") or view or "")
            provider, model = decode_option(form_value.get("model_select"), fallback_provider)
            reasoning = str(form_value.get("reasoning_select") or "").strip()
            session_key = (str((action_value or {}).get("session_key") or "")
                           or str(state.get("session_key") or "")
                           or str(getattr(self, "_last_model_picker_skey", "") or ""))
            if not session_key:
                return self._card_response(build_settled_card(
                    "⚠️ 无法定位会话", "请在本话题内重新发送 /model。", template="orange"))
            if not model:
                return self._card_response(build_settled_card(
                    "⚠️ 未选择模型", "请在渠道点选后，于下拉框里选择具体模型。", template="orange"))

            self._submit_on_loop(loop, self._apply_feishu_model_switch_from_form(
                chat_id=chat_id, session_key=session_key, model=model,
                reasoning=reasoning, provider=provider,
                form_message_id=card_message_id, form_state=state,
            ))
            settled = build_settled_card(
                "模型配置应用中",
                f"**渠道来源**：`{get_channel_label(provider, channels)}`\n"
                f"**目标模型**：`{model}`\n"
                f"**思考等级**：`{reasoning or '保持当前'}`\n\n"
                f"正在写入会话并校验凭据，几秒内会在本卡更新实际生效结果。",
                template="blue")
            return self._card_response(settled)

        logger.warning("[Feishu] Form submit with unknown action: value=%r name=%r keys=%r",
                       action_value, btn_name, list(form_value))
        return self._card_response()

    def _patch_card_message(self, message_id: str, card: dict[str, Any], *, delay: float = 1.5) -> None:
        """Replace a card message's content with the real outcome (after the callback settles)."""
        import time as _time
        from plugins.platforms.feishu.cardkit import _api
        if not message_id:
            return
        _time.sleep(delay)  # 回调期间飞书拒绝改卡（200810）；等它释放
        obj, code = _api("PATCH", f"/im/v1/messages/{message_id}",
                         {"msg_type": "interactive",
                          "content": json.dumps(card, ensure_ascii=False)})
        if obj.get("code") != 0:
            logger.warning("[Feishu] card result patch failed: code=%s http=%s %s",
                           obj.get("code"), code, obj.get("msg"))

    async def _apply_route_to_session(self, gw: Any, session_key: str, *, model: str = "",
                                      provider: str = "", reasoning: str = "") -> tuple[bool, list[str]]:
        """Write model/provider/reasoning for a session and verify it actually landed.

        Model routing is persisted exactly like /model (non-secret keys) and then rehydrated
        through ``_rehydrate_session_model_override`` — the same path a gateway restart uses —
        so api_key/base_url come from the provider's own resolution instead of a hand-built
        dict that skipped them.
        """
        notes: list[str] = []
        ok = True
        if model and provider:
            override = {"model": model, "provider": provider, "base_url": None}
            async_store = getattr(gw, "async_session_store", None)
            store = getattr(gw, "session_store", None)
            try:
                if async_store is not None:
                    await async_store.set_model_override(session_key, override)
                elif store is not None:
                    await asyncio.to_thread(store.set_model_override, session_key, override)
            except Exception as exc:
                return False, [f"写入会话失败：{exc}"]
            try:
                gw._session_state(session_key).conversation.model_override = None  # 让 rehydrate 生效
                gw._rehydrate_session_model_override(session_key)
            except Exception as exc:
                logger.debug("[Feishu] rehydrate after override failed: %s", exc)
            live = {}
            try:
                live = gw._session_model_override(session_key) or {}
            except Exception:
                pass
            if (live.get("model") or "") != model:
                ok = False
                notes.append("⚠️ 会话路由未回读到目标模型，请重试或在话题里发 /model 确认")
            else:
                try:
                    await asyncio.to_thread(_probe_provider_credentials, provider, model)
                except Exception as exc:
                    ok = False
                    notes.append(f"⚠️ 模型已写入，但渠道凭据解析失败：{exc}")
            try:
                gw._evict_cached_agent(session_key)
            except Exception:
                pass

        if reasoning:
            current = None
            try:
                current = reasoning_effort_value(gw._resolve_session_reasoning_config(session_key=session_key))
            except Exception:
                pass
            if reasoning == "reset" or current != reasoning:
                try:
                    notes.append(gw._apply_reasoning_selection(session_key, "feishu", reasoning))
                except Exception as exc:
                    ok = False
                    notes.append(f"⚠️ 思考等级设置失败：{exc}")
            else:
                notes.append(f"思考等级保持 `{reasoning}`（与当前一致，未新增会话级覆盖）")
        return ok, notes

    async def _create_feishu_topic_from_form(
        self, chat_id: str, topic_name: str, model: str, reasoning: str,
        provider: Optional[str] = None, form_message_id: str = "",
        form_state: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create a topic thread and bind model/provider/reasoning to ITS session key."""
        from plugins.platforms.feishu.cardkit import _api
        from plugins.platforms.feishu.feishu_setup_cards import (
            build_settled_card, get_channel_label, normalize_channels, pick_channel,
        )

        root_msg_id = ""
        notes: list[str] = []
        ok = True
        try:
            channels = normalize_channels()
            prov_label = get_channel_label(provider, channels)
            root_card = {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": topic_name[:80]},
                           "template": "turquoise"},
                "body": {"elements": [{"tag": "markdown", "content": (
                    f"**{topic_name}**\n\n"
                    f"渠道 `{provider or '默认'}` · 模型 `{model or '默认'}` · 思考 `{reasoning or '默认'}`\n\n"
                    "直接在本话题下回复即可开始，上下文独立。")}]},
            }
            sobj, _ = _api("POST", "/im/v1/messages",
                           {"receive_id": chat_id, "msg_type": "interactive",
                            "content": json.dumps(root_card, ensure_ascii=False)},
                           params={"receive_id_type": "chat_id"})
            root_msg_id = (sobj.get("data") or {}).get("message_id") or ""
            if not root_msg_id:
                raise RuntimeError(f"发送话题卡失败：code={sobj.get('code')} {sobj.get('msg')}")

            robj, _ = _api("POST", f"/im/v1/messages/{root_msg_id}/reply",
                           {"msg_type": "text", "reply_in_thread": True,
                            "content": json.dumps({"text": "在话题内直接回复即可开始。"}, ensure_ascii=False)})
            thread_id = (robj.get("data") or {}).get("thread_id") or ""
            if not thread_id:
                raise RuntimeError(f"话题 thread 未建立：code={robj.get('code')} {robj.get('msg')}")

            gw = self._picker_gw()
            if gw is None:
                ok = False
                notes.append("⚠️ 网关句柄缺失：话题已建，但模型/思考等级未写入")
            else:
                try:
                    chat_info = await self.get_chat_info(chat_id)
                except Exception:
                    chat_info = {}
                chat_type = str((chat_info or {}).get("type") or "dm")
                if chat_type not in {"dm", "group", "forum"}:
                    chat_type = "dm" if chat_type in {"p2p", ""} else "group"
                source = self.build_source(
                    chat_id=chat_id, chat_name=(chat_info or {}).get("name") or chat_id,
                    chat_type=chat_type, thread_id=thread_id)
                try:
                    await asyncio.to_thread(gw.session_store.get_or_create_session, source)
                except Exception as exc:
                    logger.debug("[Feishu] get_or_create_session failed: %s", exc)
                session_key = gw._session_key_for_source(source)
                ok, notes = await self._apply_route_to_session(
                    gw, session_key, model=model, provider=provider or "", reasoning=reasoning)
                logger.info("[Feishu] new topic %s skey=%s ok=%s notes=%r",
                            thread_id, session_key, ok, notes)
        except Exception as exc:
            ok = False
            notes.append(f"❌ 建话题失败：{exc}")
            logger.warning("[Feishu] Failed to create topic from form: %s", exc, exc_info=True)

        status = "✅" if ok else "⚠️"
        detail = "\n".join(f"- {n}" for n in notes) if notes else ""
        settled = build_settled_card(
            f"{status} 话题【{topic_name}】" + ("就绪" if ok else "创建异常"),
            f"**渠道来源**：`{provider or '默认'}`\n"
            f"**执行模型**：`{model or '默认'}`\n"
            f"**思考等级**：`{reasoning or '默认'}`\n\n{detail}\n\n"
            "进入该话题直接对话即可；如需改配置，在话题内发 /model。",
            template="green" if ok else "orange")
        if form_message_id:
            self._patch_card_message(form_message_id, settled)

    async def _apply_feishu_model_switch_from_form(
        self, chat_id: str, session_key: str, model: str, reasoning: str,
        provider: Optional[str] = None, form_message_id: str = "",
        form_state: Optional[dict[str, Any]] = None,
    ) -> None:
        """Apply model/reasoning from the /model card and report the verified outcome."""
        from plugins.platforms.feishu.feishu_setup_cards import build_settled_card, get_channel_label, normalize_channels

        state = form_state or {}
        prev = state.get("model") if isinstance(state.get("model"), dict) else {}
        prev = prev or {}
        # 与「本卡发出时」的会话状态比较，而不是与刚写入后的状态比较（否则永远判为未变）。
        unchanged = bool(prev.get("model")) and (prev.get("model") == model
                                                and prev.get("provider") == (provider or prev.get("provider")))
        ok = True
        notes: list[str] = []
        gw = self._picker_gw()
        channels = normalize_channels()
        label = get_channel_label(provider, channels)

        if gw is None:
            ok = False
            notes.append("⚠️ 网关句柄缺失：无法写入会话配置")
        else:
            cb = (getattr(self, "_model_picker_callbacks", {}) or {}).get(session_key)
            if cb is not None and not unchanged:
                # 原生 picker 回调：解析 provider、写 DB、置 next-turn note、驱逐 agent 都走官方路径。
                try:
                    text = await cb(chat_id, model, provider or "")
                except Exception as exc:
                    ok = False
                    notes.append(f"❌ 切换失败：{exc}")
                else:
                    if text:
                        body = str(text)
                        if body.lower().startswith("error") or "失败" in body:
                            ok = False
                        notes.append(body.splitlines()[0])
            else:
                # 无回调（网关重启过 / 本话题非当前 picker）或选择未变：直接写路由并回读校验。
                if unchanged:
                    notes.append("模型与卡片发出时一致，按当前选择重申一次并校验")
                write_ok, write_notes = await self._apply_route_to_session(
                    gw, session_key, model=model, provider=provider or "", reasoning="")
                ok = ok and write_ok
                notes.extend(write_notes)
            if reasoning:
                try:
                    notes.append(gw._apply_reasoning_selection(session_key, "feishu", reasoning))
                except Exception as exc:
                    ok = False
                    notes.append(f"⚠️ 思考等级设置失败：{exc}")
            try:
                gw._evict_cached_agent(session_key)
            except Exception:
                pass

        status = "✅" if ok else "⚠️"
        settled = build_settled_card(
            f"{status} 模型配置" + ("已生效" if ok else "未完全生效"),
            f"**渠道来源**：`{label}`\n"
            f"**当前模型**：`{model}`\n"
            f"**思考等级**：`{reasoning or '保持当前'}`\n\n"
            + ("\n".join(f"- {n}" for n in notes) if notes else ""),
            template="green" if ok else "orange")
        if form_message_id:
            self._patch_card_message(form_message_id, settled)
            route = self._session_route_now(session_key, provider or "")
            self._remember_form_state(form_message_id, **{
                **state, "model": {"model": model, "provider": provider},
                "reasoning_effort": reasoning or route["reasoning"],
                "view_provider": provider, "session_key": session_key})
