#!/usr/bin/env bash
# 从本机 ~/.hermes/{patches,scripts} 与 Obsidian 同步最新补丁到本仓库
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -a ~/.hermes/patches/feishu_suite_full.patch "$REPO_ROOT/patches/"
cp -a ~/.hermes/patches/feishu_topic_thread.patch "$REPO_ROOT/patches/"
cp -a ~/.hermes/patches/feishu_topic_thread_docs_tests.patch "$REPO_ROOT/patches/"
rm -rf "$REPO_ROOT/patches/feishu_suite_files"
cp -a ~/.hermes/patches/feishu_suite_files "$REPO_ROOT/patches/"
cp -a ~/.hermes/scripts/apply_feishu_suite.sh "$REPO_ROOT/scripts/"
cp -a ~/.hermes/scripts/feishu_patch_guard.sh "$REPO_ROOT/scripts/"
cp -a ~/.hermes/scripts/apply_feishu_topic_thread.sh "$REPO_ROOT/scripts/"
cp -a ~/.hermes/scripts/apply_feishu_cardkit_fix.sh "$REPO_ROOT/scripts/"
VAULT_NOTE="${OBSIDIAN_VAULT:-${HOME}/文档/Obsidian_Vault}/projects/feishu-gateway/飞书全量补丁升级维护手册.md"
if [ -f "$VAULT_NOTE" ]; then
  cp -a "$VAULT_NOTE" "$REPO_ROOT/README.md"
fi
echo "✅ 已同步最新补丁资产至 $REPO_ROOT"
git -C "$REPO_ROOT" status -s
