#!/usr/bin/env bash
# 重打「飞书分话题」补丁（豆花本机补丁 2026-11-07 固化，与 CardKit 补丁解耦）
# 用途：hermes update / rollback_cardkit.sh 整文件 checkout adapter.py 后，跑本脚本恢复话题能力。
# 幂等：逐文件块判断（未打→正向 apply；已打→跳过；既不能正向也不能反向→报 CONFLICT）。
# 前置：~/.hermes/patches/feishu_topic_thread.patch 与 feishu_topic_thread_files/
set -euo pipefail
H=${HOME}/.hermes/hermes-agent
P=${HOME}/.hermes/patches/feishu_topic_thread.patch
F=${HOME}/.hermes/patches/feishu_topic_thread_files
ENV=${HOME}/.hermes/.env
TMP=${HOME}/.hermes/cache/scratch/.topic_block.patch
cd "$H"

python3 - "$P" "$TMP" <<'PY'
import re, subprocess, sys
patch, tmp = open(sys.argv[1], encoding='utf-8').read(), sys.argv[2]
blocks = [b for b in re.split(r'(?=^diff --git )', patch, flags=re.M) if b.strip()]
failed = False
for b in blocks:
    name = b.split('\n', 1)[0].split(' b/')[0].replace('diff --git a/', '')
    open(tmp, 'w', encoding='utf-8').write(b)
    fwd = subprocess.run(['git', 'apply', '--check', tmp], capture_output=True)
    rev = subprocess.run(['git', 'apply', '--check', '-R', tmp], capture_output=True)
    if fwd.returncode == 0:
        r = subprocess.run(['git', 'apply', tmp], capture_output=True, text=True)
        if r.returncode:
            print('[topic] FAIL apply', name, r.stderr.strip()[:200]); failed = True
        else:
            print('[topic] applied ', name)
    elif rev.returncode == 0:
        print('[topic] present ', name)
    else:
        print('[topic] CONFLICT', name, '(neither forward nor reverse applies)'); failed = True
sys.exit(1 if failed else 0)
PY

# 两个 untracked 新文件（分话题模块 + 单测）
for pair in "feishu_thread_reply.py:plugins/platforms/feishu/feishu_thread_reply.py" \
            "test_feishu_thread_reply.py:tests/gateway/test_feishu_thread_reply.py"; do
  src="${pair%%:*}"; dst="${pair#*:}"
  if [ ! -f "$dst" ]; then cp "$F/$src" "$dst"; echo "[topic] restored $dst"; fi
done

# .env 两个开关（缺则补，不覆盖已有值）
python3 - "$ENV" <<'PY'
import sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
add = [f'{k}={v}\n' for k, v in (('FEISHU_THREAD_REPLY', 'true'), ('FEISHU_DM_TOPIC_REPLY', 'true'))
       if not any(l.strip().startswith(k + '=') for l in t.splitlines())]
if add:
    open(p, 'a', encoding='utf-8').write('\n' + ''.join(add))
    print('[topic] env appended:', [a.strip() for a in add])
else:
    print('[topic] env switches present')
PY

# 关键落点校验
grep -q '豆花补丁：DM 主框消息无 reply_to' plugins/platforms/feishu/adapter.py
grep -q 'FEISHU_DM_TOPIC_REPLY' plugins/platforms/feishu/plugin.yaml
grep -q 'FEISHU_DM_TOPIC_REPLY' "$ENV"
[ -f plugins/platforms/feishu/feishu_thread_reply.py ]

# 回归测试
/usr/bin/python3 -m pytest tests/gateway/test_feishu_thread_reply.py tests/gateway/test_setup_feishu.py \
  -q --no-header --tb=short 2>&1 | tail -3
echo "[topic] PASS — 分话题补丁完整（回滚/升级后重跑本脚本即可）"
