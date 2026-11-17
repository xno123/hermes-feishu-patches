# hermes-feishu-patches

Hermes Agent 飞书（Feishu / Lark）增强补丁套件：私聊与群聊话题体系、CardKit 2.0 流式卡片（含 600 秒流式窗口自动重开）、`/new` 交互式建话题、`/model` 渠道与模型二级联动、思考流与工具调用的开发者向排版。

A source-level patch suite for the Feishu (Lark) adapter of [Hermes Agent](https://github.com/NousResearch/hermes-agent): DM/group topics, CardKit 2.0 streaming cards (with automatic re-arm beyond Feishu's 600s streaming window), interactive `/new` and `/model` cards driven by Hermes' own dynamic model catalog, and polished thinking-stream / tool-call rendering. MIT licensed.

> 这是**源码补丁**而非独立插件：Hermes 上游升级会覆盖 `gateway/` 与 `plugins/platforms/feishu/` 下的文件，本仓库提供一键重打补丁与体检看门狗。
> 打补丁基线：Hermes v0.23.0（commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`）。其他版本请先用 `git apply --check` 验证。

## 快速开始

```bash
# 新环境一键安装（复制补丁资产到 ~/.hermes 并打入 Hermes 源码、自动跑回归测试）
git clone https://github.com/xno123/hermes-feishu-patches.git
bash hermes-feishu-patches/install.sh

# 升级 Hermes 后：体检 → 有 FAIL 自动修复 → 复检
bash ~/.hermes/scripts/feishu_patch_guard.sh          # 体检
bash ~/.hermes/scripts/feishu_patch_guard.sh --fix    # 自动修复
systemctl --user restart hermes-gateway.service       # 重启网关生效
```

`.env` 门控开关：`FEISHU_CARDKIT=1`、`FEISHU_THREAD_REPLY=true`、`FEISHU_DM_TOPIC_REPLY=true`（私聊开话题）。
本机改动回同步：`bash sync_from_local.sh && git commit -am "update" && git push`。

---

## 一、 增强功能清单与底层架构

| 模块 | 功能名称 | 涉及核心文件 | 解决痛点 / 业务价值 |
| :--- | :--- | :--- | :--- |
| **话题体系** | **私聊与群聊独立话题** | `feishu_thread_reply.py`<br>`adapter.py`<br>`plugin.yaml` | 飞书官方默认私聊无话题；开启 `FEISHU_DM_TOPIC_REPLY=true` 支持私聊开话题；主聊天框发 `/new` 不会把卡片误打包成名为 `/new` 的话题；入站锚点按 `(chat_id, thread_id)` 隔离防私聊发图串话题。 |
| **流式卡片** | **CardKit 2.0 异步渲染** | `cardkit.py`<br>`cardkit_bridge.py`<br>`run_turn_runner.py` | 支持会话隔离分槽（`_ACTIVE`, `_ALIASES`）；多话题并发不串台；收尾时通过 PATCH 静默覆盖为 DSH 式完成卡，并发送轻量级完成 Ping 触发端侧通知。飞书流式窗口满 600 秒自动关闭后，自动 `_rearm_streaming()` 重开并重发被拒更新；重开失败降级为整卡 PATCH 续写，正文不丢。 |
| **会话创建** | **`/new` 交互式表单卡片** | `feishu_setup_cards.py`<br>`adapter_choice_cards.py`<br>`run_inbound.py` | 提供话题名称自定义、渠道按钮即时切换、专属渠道内模型筛选、8 档思考等级选择；支持渠道默认主力模型自动兜底。 |
| **状态持久化** | **话题配置首条消息生效** | `adapter_choice_cards.py`<br>`session.py`<br>`run_turn_prepare.py` | 解决新建话题未落库导致 `set_model_override` 被静默丢弃的问题。先行调用 `get_or_create_session` 建档，四重注入 SQLite、运行时内存 `_session_state`、思考模式与缓存驱逐。 |
| **模型切换** | **`/model` 渠道与模型二级联动** | `feishu_setup_cards.py`<br>`adapter_choice_cards.py` | 顶部提供渠道 Tabs 按钮，点击即时原地刷新，模型下拉框仅展示当前渠道模型；对齐 8 档思考等级；自动精准回显当前生效值。模型目录直接读 Hermes 原生 `list_picker_providers()`，新增渠道/模型零改代码。 |
| **思考流展示** | **平滑固定视窗（防抖归档）** | `cardkit.py` | 超过 360 字符平滑向上收起，顶部固定单行提示 `<font color='grey'>▲ 早期思考已向上收起 · 正在最新推理…</font>`，高度恒定锁定，绝无跳行；收尾时 100% 完整全量归档入折叠面板。 |
| **工具调用** | **高密度专业开发者排版** | `cardkit.py`<br>`run_turn_progress.py`<br>`cardkit_bridge.py` | 像素级对齐 Open-WebUI/DSH 风格：`<font color='blue'>●</font>  ✅ **`bash`** — `command``；参数全链路抓取透传；保持 `str` 继承向后兼容；折叠栏动态显示 `🔧 工具调用（共 N 次）`。 |

---

## 二、 补丁资产清单

- **GitHub 公开仓库**：`https://github.com/xno123/hermes-feishu-patches`（MIT License）
- **本地同步工作目录**：`~/hermes-workspace/hermes-feishu-patches`
  - 新环境一键安装：`git clone https://github.com/xno123/hermes-feishu-patches.git && bash hermes-feishu-patches/install.sh`
  - 本机改动同步推送：`cd ~/hermes-workspace/hermes-feishu-patches && bash sync_from_local.sh && git commit -am "update" && git push`

所有运行时资产同时独立保存在 `~/.hermes/` 内部，升级主仓库源码不会被删除或覆盖：

1. **统一 Git Patch 文件**：
   - 路径：`~/.hermes/patches/feishu_suite_full.patch`
   - 大小：约 204 KB
   - 作用：一键将所有修改以标准 git diff 形式打入源码。
2. **完整镜像快照目录（Mirror Snapshot）**：
   - 路径：`~/.hermes/patches/feishu_suite_files/`
   - 包含文件：
     - `gateway/`: `cardkit_bridge.py`, `run_inbound.py`, `run_turn_runner.py`, `run_turn_progress.py`, `stream_consumer_transport.py`
     - `plugins/platforms/feishu/`: `cardkit.py`, `feishu_setup_cards.py`, `feishu_thread_reply.py`, `adapter.py`, `adapter_choice_cards.py`, `plugin.yaml`
     - `tests/gateway/`: `test_cardkit_scope.py`, `test_feishu_setup_cards.py`, `test_feishu_thread_reply.py`, `test_setup_feishu.py`
   - 作用：当上游升级发生大幅度代码重构导致 patch 冲突时，作为 100% 可靠的原件进行差异比对与文件级恢复。
3. **一键安装与恢复脚本**：
   - 路径：`~/.hermes/scripts/apply_feishu_suite.sh`
   - 特性：自动判断 `git apply` 或快照恢复、自动校准 `.env`、自动运行全套 pytest 回归测试。
4. **体检看门狗脚本**：
   - 路径：`~/.hermes/scripts/feishu_patch_guard.sh`
   - 用法：`bash ~/.hermes/scripts/feishu_patch_guard.sh`（体检）或 `bash ~/.hermes/scripts/feishu_patch_guard.sh --fix`（自动修复）。

---

## 三、 未来升级 Hermes 后的标准恢复工作流

当用户或维护脚本执行 `hermes update`（拉取官方最新代码）之后，按以下标准两步走即可完全恢复所有定制功能：

### 步骤 1：一键重打补丁并跑通回归测试
在宿主机终端执行：

```bash
bash ~/.hermes/scripts/feishu_patch_guard.sh --fix
```
或者直接执行：
```bash
bash ~/.hermes/scripts/apply_feishu_suite.sh
```

**执行效果**：
- 自动完成补丁打入或快照安全同步；
- 自动校验 `.env` 环境变量；
- 自动运行 pytest 单元测试并确保通过（最近基线：176 passed / 0 failed / 18 skipped，详见 `docs/`）。

### 步骤 2：重启网关加载生效

在终端执行：

```bash
systemctl --user restart hermes-gateway.service
```

重启耗时约 2 秒，即可在飞书中无缝继续使用全部话题、卡片、思考流与工具排版能力。

---

## 四、 维护文档

`docs/` 下保留了关键排障与设计记录：

- `2026-11-07_飞书补丁维护手册.md`：补丁基线、升级防错 SOP。
- `2026-11-08_飞书卡片配置生效与思考流优化.md`：卡片配置生效链路与思考流优化。
- `2026-11-09_飞书卡片600秒流式上限与动态模型目录.md`：飞书 600 秒流式窗口实测探针、重开/降级策略、模型目录动态化。

## License

MIT © 2026 dexun (xno123)
