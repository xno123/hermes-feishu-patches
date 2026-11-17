#!/usr/bin/env bash
# 飞书全量特性补丁套件一键恢复/安装脚本 (豆花 2026-11-08)
#
# 功能特性：
# 1. 飞书原生话题体系（群聊与私聊独立话题、DM主框防锚点包裹）
# 2. CardKit 2.0 异步流式卡片（多会话精确分槽隔离、PATCH DSH 式完成态覆盖）
# 3. /new 交互式话题创建表单（渠道按钮行即时联动、渠道内模型专属选择、主力模型自动兜底）
# 4. 话题配置落库与首条消息 100% 绑定（SessionStore 先行建档、运行时 _session_state 双重注入）
# 5. /model 渠道与模型二级联动卡片（8档思考等级全量支持、当前生效值精准回显）
# 6. 思考流平滑防抖固定视窗（早期思考平滑向上收起、无跳跃颠簸、结尾100%全量归档）
# 7. 专业开发者高密度工具调用排版（● ✅ bash — command，命令智能提取，别名映射）
#
# 适用场景：
# - `hermes update` 升级后一键恢复全部改动
# - 迁移或部署新环境时一键就绪
set -euo pipefail

H="${HERMES_SRC:-${HOME}/.hermes/hermes-agent}"
PATCH_FILE="${HOME}/.hermes/patches/feishu_suite_full.patch"
FILES_DIR="${HOME}/.hermes/patches/feishu_suite_files"
ENV_FILE="${HOME}/.hermes/.env"

echo "=== 开始安装/恢复飞书全量特性补丁套件 ==="
echo "目标代码库: $H"
echo "补丁文件:   $PATCH_FILE"
echo "快照文件:   $FILES_DIR"

if [ ! -d "$H" ]; then
    echo "❌ 错误: 目标目录 $H 不存在！" >&2
    exit 1
fi

cd "$H"

# 1. 优先尝试标准 git apply
APPLIED=0
if [ -f "$PATCH_FILE" ]; then
    echo "--> 尝试使用 Git Patch 应用修改..."
    if git apply --check "$PATCH_FILE" 2>/dev/null; then
        git apply "$PATCH_FILE"
        echo "✅ Git Patch 应用成功！"
        APPLIED=1
    else
        echo "⚠️  Git Patch 检查未完全匹配（上游代码可能有微小漂移），切入快照覆盖模式..."
    fi
fi

# 2. 若 patch 无法直接应用，使用快照文件进行可靠覆盖
if [ "$APPLIED" -eq 0 ]; then
    if [ -d "$FILES_DIR" ]; then
        echo "--> 正在从完整快照目录安全同步各模块..."
        cp -a "$FILES_DIR/gateway/"* gateway/
        cp -a "$FILES_DIR/plugins/platforms/feishu/"* plugins/platforms/feishu/
        cp -a "$FILES_DIR/tests/gateway/"* tests/gateway/
        echo "✅ 快照文件同步完成！"
    else
        echo "❌ 错误: 补丁文件与快照目录均不可用！" >&2
        exit 1
    fi
fi

# 3. 环境变量门控自动配置
echo "--> 检查 .env 门控配置..."
mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

ensure_env() {
    local k="$1"
    local v="$2"
    if grep -qE "^[[:space:]]*$k=" "$ENV_FILE"; then
        sed -i "s|^[[:space:]]*$k=.*|$k=$v|" "$ENV_FILE"
    else
        echo "$k=$v" >> "$ENV_FILE"
    fi
}

ensure_env "FEISHU_CARDKIT" "1"
ensure_env "FEISHU_THREAD_REPLY" "true"
ensure_env "FEISHU_DM_TOPIC_REPLY" "true"
echo "✅ .env 配置已确保: FEISHU_CARDKIT=1, FEISHU_THREAD_REPLY=true, FEISHU_DM_TOPIC_REPLY=true"

# 4. 执行全套自动化回归测试
echo "--> 正在运行全量自动化测试套件 (57项)..."
TMP_LOG=$(mktemp)
if /usr/bin/python3 -m pytest \
    tests/gateway/test_feishu_thread_reply.py \
    tests/gateway/test_setup_feishu.py \
    tests/gateway/test_cardkit_scope.py \
    tests/gateway/test_feishu_setup_cards.py \
    -q --no-header > "$TMP_LOG" 2>&1; then
    RESULT=$(grep -E 'passed|failed' "$TMP_LOG" | tail -1)
    echo "✅ 自动化回归测试全部通过: $RESULT"
    rm -f "$TMP_LOG"
else
    echo "❌ 自动化回归测试存在失败项！" >&2
    tail -20 "$TMP_LOG" >&2
    rm -f "$TMP_LOG"
    exit 1
fi

echo
echo "=========================================================="
echo "🎉 飞书全量特性补丁套件安装成功并验证完毕！"
CMD_RELOAD="systemctl --user restart"
SVC_NAME="hermes-gateway.service"
echo "提示: 请在宿主机终端执行以下命令重启网关生效:"
echo "      $CMD_RELOAD $SVC_NAME"
echo "=========================================================="
