#!/usr/bin/env bash
# 飞书本地补丁体检 / 更新后重打（豆花 2026-11-07）
#
# 用法：
#   bash ~/.hermes/scripts/feishu_patch_guard.sh          # 只体检
#   bash ~/.hermes/scripts/feishu_patch_guard.sh --fix    # 缺失则按序重打，再复检
#
# 支持 HERMES_SRC 覆盖源码目录（临时目录演练用；--fix 请勿指向临时目录）。
# 背景：hermes update 会覆盖 gateway/ 与 plugins/platforms/feishu/ 下的核心文件，
# 本机两套补丁（分话题 / 卡片）都是源码补丁，更新后必须按本脚本核对并重打。
set -uo pipefail
H=${HERMES_SRC:-${HOME}/.hermes/hermes-agent}
ENV=${HOME}/.hermes/.env
S=${HOME}/.hermes/scripts
BASELINE=2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19
cd "$H" || exit 2
PASS=0; FAIL=0; WARN=0
TMP=$(mktemp ${HOME}/.hermes/cache/scratch/guard_XXXXXX.log)

ok(){   echo "[PASS] $1"; PASS=$((PASS+1)); }
no(){   echo "[FAIL] $1"; FAIL=$((FAIL+1)); }
warn(){ echo "[WARN] $1"; WARN=$((WARN+1)); }
has(){  grep -qF "$2" "$1" 2>/dev/null; }
exists(){ [ -f "$1" ]; }
envv(){ grep -qE "^$1=$2[[:space:]]*$" "$ENV" 2>/dev/null; }

echo "=== 飞书补丁体检 | HEAD=$(git rev-parse --short HEAD 2>/dev/null) | $(date '+%F %T') ==="

echo "--- 1. .env 门控 ---"
envv FEISHU_CARDKIT 1          && ok "FEISHU_CARDKIT=1"          || no "FEISHU_CARDKIT 不是 1"
envv FEISHU_THREAD_REPLY true  && ok "FEISHU_THREAD_REPLY=true"  || no "FEISHU_THREAD_REPLY 缺失/非 true"
envv FEISHU_DM_TOPIC_REPLY true && ok "FEISHU_DM_TOPIC_REPLY=true" || no "FEISHU_DM_TOPIC_REPLY 缺失/非 true"

echo "--- 2. 分话题补丁 ---"
exists plugins/platforms/feishu/feishu_thread_reply.py && ok "feishu_thread_reply.py 存在" || no "feishu_thread_reply.py 缺失"
has plugins/platforms/feishu/feishu_thread_reply.py 'FEISHU_DM_TOPIC_REPLY' && ok "DM 话题开关逻辑" || no "DM 话题开关逻辑"
has plugins/platforms/feishu/adapter.py '豆花补丁：DM 主框消息无 reply_to' && ok "DM 锚点 hunk" || no "DM 锚点 hunk"
has plugins/platforms/feishu/adapter.py '_last_inbound_message_id[(chat_id, thread_id or "")]' && ok "DM 锚点按 (chat_id, thread_id) 隔离" || no "DM 锚点未按 thread_id 隔离（图片会串话题）"
has plugins/platforms/feishu/adapter.py 'thread_reply: bool = False' && ok "thread_reply 设置" || no "thread_reply 设置"
has plugins/platforms/feishu/plugin.yaml 'FEISHU_DM_TOPIC_REPLY' && ok "plugin.yaml 声明" || no "plugin.yaml 声明"
has tests/gateway/test_setup_feishu.py 'TestSetupFeishuThreadReply' && ok "setup 向导话题选项" || no "setup 向导话题选项（升级易漏的 interactive_setup hunk）"
exists tests/gateway/test_feishu_thread_reply.py && ok "话题单测存在" || no "话题单测缺失"

echo "--- 3. 卡片补丁 ---"
exists plugins/platforms/feishu/cardkit.py && ok "cardkit.py 存在" || no "cardkit.py 缺失"
exists gateway/cardkit_bridge.py && ok "cardkit_bridge.py 存在" || no "cardkit_bridge.py 缺失"
exists tests/gateway/test_cardkit_scope.py && ok "卡片回归测试存在" || no "卡片回归测试缺失"
has plugins/platforms/feishu/cardkit.py '_ALIASES: dict[str, str]' && ok "会话分槽注册表" || no "会话分槽注册表"
has plugins/platforms/feishu/cardkit.py 'def on_thinking(' && ok "思考流 on_thinking" || no "思考流 on_thinking"
has plugins/platforms/feishu/cardkit.py 'def _streaming_on(' && ok "streaming 模式探测" || no "streaming 模式探测"
has plugins/platforms/feishu/cardkit.py 'if _streaming_on():' && ok "streaming 分支封卡" || no "streaming 分支封卡（答复会卡数分钟）"
has plugins/platforms/feishu/cardkit.py 'def _log(' && ok "卡片事件流水日志" || no "卡片事件流水日志"
has plugins/platforms/feishu/cardkit.py 'ok = ck.push_answer(content)' && ok "send 写正文" || no "send 写正文（否则正文停在等待回答）"
has plugins/platforms/feishu/cardkit.py 'is_turn_final: bool = False' && ok "封卡时机 is_turn_final" || no "封卡时机 is_turn_final"
has plugins/platforms/feishu/cardkit.py 'class ElementPolicy:' && ok "元素策略集中配置" || no "元素策略集中配置"
has plugins/platforms/feishu/cardkit.py 'def render(self, *, force: bool = False)' && ok "统一渲染层 render()" || no "统一渲染层 render()"
has plugins/platforms/feishu/cardkit.py 'def _title_from_hint(' && ok "标题来自本轮消息" || no "标题来自本轮消息（否则串台）"
has plugins/platforms/feishu/cardkit.py 'self._tool_lines.append(s)' && ok "工具事件全记录" || no "工具事件全记录（同名工具会被吞）"
has gateway/run_turn_runner.py 'title_hint=getattr(ctx, "message", None)' && ok "title_hint 透传" || no "title_hint 透传"
has plugins/platforms/feishu/cardkit.py 'if not is_final:' && ok "中间播报分流" || no "中间播报分流（会覆盖正文）"
has plugins/platforms/feishu/adapter.py 'is_final=bool((metadata or {}).get("notify"))' && ok "最终答复标记透传" || no "最终答复标记透传"
has plugins/platforms/feishu/cardkit.py 'PANEL_FLUSH_GAP = 2.0' && ok "面板降频" || no "面板降频（思考框会抖动）"
has plugins/platforms/feishu/cardkit.py 'THINK_LINES = 5' && ok "思考5行滚动" || no "思考5行滚动（THINK_LINES）"
has plugins/platforms/feishu/cardkit.py 'def _think_visible(' && ok "思考可见行截取" || no "思考可见行截取（_think_visible）"
has plugins/platforms/feishu/cardkit.py 'def _build_completion_card(' && ok "完成覆盖卡构造" || no "完成覆盖卡构造（_build_completion_card）"
has plugins/platforms/feishu/cardkit.py 'patch_overlay' && ok "PATCH overlay 覆盖" || no "PATCH overlay 覆盖（finalize 末尾）"
has plugins/platforms/feishu/cardkit.py 'self.reasoning_effort' && ok "reasoning_effort 属性" || no "reasoning_effort 属性"
has gateway/cardkit_bridge.py 'reasoning_effort' && ok "bridge 透传 reasoning_effort" || no "bridge 透传 reasoning_effort"
has gateway/run_turn_runner.py 'model=model' && ok "runner 透传 model" || no "runner 透传 model 到 cardkit"
has plugins/platforms/feishu/cardkit.py 'if not self._answer and self._last_interim:' && ok "空正文兜底" || no "空正文兜底（封卡会空壳）"
has plugins/platforms/feishu/cardkit.py 'content.lstrip().startswith("↪")' && ok "redirect 提示隔离" || no "redirect 提示隔离（会封死在 18 字）"
has plugins/platforms/feishu/cardkit.py '_seal_soon(ck' && ok "答复到即封卡" || no "答复到即封卡（会卡数分钟）"
has gateway/cardkit_bridge.py 'def on_thinking(' && ok "bridge 思考转发" || no "bridge 思考转发"
has gateway/run_turn_runner.py 'session_key=getattr(ctx, "session_key", None)' && ok "session_key 透传" || no "session_key 透传"
has gateway/run_turn_runner.py '_ck_reasoning' && ok "reasoning delta tee" || no "reasoning delta tee"
has gateway/run_turn_progress.py 'progress_queue guard 之前' && ok "工具 tee 在 guard 前" || no "工具 tee 在 guard 前"
has plugins/platforms/feishu/adapter.py 'message_id=message_id):' && ok "on_edit 按 message_id 定位" || no "on_edit 按 message_id 定位"
has plugins/platforms/feishu/adapter.py 'is_turn_final=is_turn_final' && ok "adapter 透传 is_turn_final" || no "adapter 透传 is_turn_final"
has gateway/stream_consumer_transport.py 'is_turn_final' && ok "transport 透传 is_turn_final" || no "transport 透传 is_turn_final"
has plugins/platforms/feishu/feishu_setup_cards.py 'def normalize_channels(' && ok "渠道内模型二级联动（目录驱动）" || no "渠道内模型二级联动"
has plugins/platforms/feishu/feishu_setup_cards.py 'list_picker_providers' && ok "渠道/模型来自 Hermes 实时目录（无硬编码表）" || no "渠道/模型未接实时目录"
has plugins/platforms/feishu/adapter_choice_cards.py 'catalog_is_truncated' && ok "目录截断自动补全" || no "目录截断未补全（模型会缺）"
has plugins/platforms/feishu/adapter_choice_cards.py '_apply_route_to_session' && ok "会话路由写入+回读校验" || no "会话路由写入未校验"
has plugins/platforms/feishu/cardkit.py 'STREAMING_WINDOW_SECS' && ok "600s 流式窗口常量" || no "600s 流式窗口常量缺失"
has plugins/platforms/feishu/cardkit.py '_flush_full_card' && ok "流式关闭后整卡 PATCH 续写" || no "流式关闭后未接整卡 PATCH（10 分钟后丢正文）"
has plugins/platforms/feishu/cardkit.py '_rearm_streaming' && ok "流式窗口关闭后自动重开" || no "缺少流式重开（10 分钟后只能整卡刷新）"
has plugins/platforms/feishu/adapter_choice_cards.py 'select_channel' && ok "渠道按钮即时切换响应" || no "渠道按钮即时切换响应"
has plugins/platforms/feishu/adapter_choice_cards.py 'get_or_create_session' && ok "新话题首条消息落库注入" || no "新话题首条消息落库注入"
has plugins/platforms/feishu/cardkit.py 'format_tool_display' && ok "工具调用高密度开发者排版" || no "工具调用高密度开发者排版"
exists plugins/platforms/feishu/feishu_setup_cards.py && ok "feishu_setup_cards.py 存在" || no "feishu_setup_cards.py 缺失"
has plugins/platforms/feishu/adapter_choice_cards.py 'hermes_feishu_form_action' && ok "表单回调路由注册" || no "表单回调路由注册"
has plugins/platforms/feishu/adapter_choice_cards.py 'async def send_model_picker(' && ok "send_model_picker 实现" || no "send_model_picker 实现"
has plugins/platforms/feishu/adapter_choice_cards.py 'async def send_new_topic_picker(' && ok "send_new_topic_picker 实现" || no "send_new_topic_picker 实现"
has gateway/run_inbound.py 'send_new_topic_picker' && ok "/new 弹配置卡逻辑（走 adapter）" || no "/new 弹配置卡逻辑"

echo "--- 4. 回归测试 ---"
if /usr/bin/python3 -m pytest tests/gateway/test_feishu_thread_reply.py \
     tests/gateway/test_setup_feishu.py tests/gateway/test_cardkit_scope.py \
     tests/gateway/test_feishu_setup_cards.py \
     -q --no-header >"$TMP" 2>&1; then
  ok "回归测试 ($(grep -E 'passed|failed' "$TMP" | tail -1))"
else
  no "回归测试失败"
  tail -8 "$TMP"
fi

echo "--- 5. 上游版本漂移 ---"
CUR=$(git rev-parse HEAD 2>/dev/null || echo unknown)
if [ "$CUR" = "$BASELINE" ]; then
  ok "HEAD 与打补丁时一致"
else
  warn "HEAD 已从基线变化；--fix 不会自动整文件覆盖卡片补丁"
  CH=$(git diff --name-only "$BASELINE" HEAD -- \
       gateway/cardkit_bridge.py gateway/run_turn_runner.py gateway/run_turn_progress.py \
       gateway/stream_consumer_transport.py plugins/platforms/feishu/adapter.py \
       plugins/platforms/feishu/cardkit.py plugins/platforms/feishu/feishu_thread_reply.py 2>/dev/null)
  if [ -n "$CH" ]; then
    echo "       上游改动过的补丁文件："
    echo "$CH" | sed 's/^/         - /'
  fi
  echo "       请人工核对后再决定是否覆盖（见 Obsidian《飞书补丁维护手册》）"
fi

echo
echo "=== SUMMARY: $PASS PASS / $FAIL FAIL / $WARN WARN ==="
if [ "$FAIL" -gt 0 ]; then
  echo "修复：bash $S/feishu_patch_guard.sh --fix"
else
  echo "补丁齐全。注意：改动需由用户在外部重启网关后才生效。"
fi
rm -f "$TMP"

if [ "${1:-}" = "--fix" ] && [ "$FAIL" -gt 0 ]; then
  echo
  echo "=== 自动重打全量补丁套件 ==="
  bash "$S/apply_feishu_suite.sh" || echo "[WARN] 全量补丁套件应用失败"
  echo "=== 复检 ==="
  exec bash "$0"
fi
if [ "$FAIL" -eq 0 ]; then exit 0; else exit 1; fi
