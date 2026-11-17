#!/usr/bin/env bash
# 重打「卡片溢出修复」（豆花本机补丁 2026-11-07）：整文件覆盖，天然幂等。
# 与按 hunk 拆的补丁不同：after/ 里是改动后工作树的完整文件，已含分话题/CardKit 全部既有补丁，
# 覆盖不会抹掉别的特性。升级或误操作后跑本脚本即可恢复。
set -euo pipefail
H=${HOME}/.hermes/hermes-agent
A=${HOME}/.hermes/patches/cardkit_fix_20261107/after
FILES="gateway/cardkit_bridge.py plugins/platforms/feishu/cardkit.py gateway/run_turn_runner.py gateway/run_turn_progress.py plugins/platforms/feishu/adapter.py gateway/stream_consumer_transport.py gateway/run_inbound.py plugins/platforms/feishu/adapter_choice_cards.py plugins/platforms/feishu/feishu_setup_cards.py tests/gateway/test_cardkit_scope.py tests/gateway/test_feishu_setup_cards.py"
cd "$H"
for f in $FILES; do
  mkdir -p "$(dirname "$f")"
  cp "$A/$f" "$f"
done
# 关键落点校验
grep -qF 'def on_thinking(' plugins/platforms/feishu/cardkit.py
grep -qF '_ALIASES: dict[str, str]' plugins/platforms/feishu/cardkit.py
grep -qF 'session_key=getattr(ctx, "session_key", None)' gateway/run_turn_runner.py
grep -qF 'message_id=message_id):' plugins/platforms/feishu/adapter.py
grep -qF 'is_turn_final' gateway/stream_consumer_transport.py
# 语法 + 回归测试
python3 -m py_compile gateway/cardkit_bridge.py plugins/platforms/feishu/cardkit.py \
  gateway/run_turn_runner.py gateway/run_turn_progress.py plugins/platforms/feishu/adapter.py
/usr/bin/python3 -m pytest tests/gateway/test_cardkit_scope.py -q --no-header 2>&1 | tail -3
echo "[cardkit-fix] PASS — 补丁完整（改动需由用户在外部重启网关后生效）"
