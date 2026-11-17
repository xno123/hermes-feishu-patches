#!/usr/bin/env bash
# 将本仓库中的补丁与脚本安装到 ~/.hermes/ 并一键应用到 Hermes 源码
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p ~/.hermes/patches ~/.hermes/scripts
cp -a "$REPO_ROOT/patches/"* ~/.hermes/patches/
cp -a "$REPO_ROOT/scripts/"* ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/*.sh
bash ~/.hermes/scripts/apply_feishu_suite.sh
