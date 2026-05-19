# Codex Automation 远程运行机部署设计

日期：2026-05-19
状态：已确认设计，等待实施计划

## 背景

本项目正在把短时 LLM 定时任务从旧的 Claude CLI + launchd 迁移到
Codex automations。当前这台机器是开发机，不是默认运行机；真正需要长时间
稳定运行的是一台远程 runtime host，用来做 staging 和长期运行测试。

因此正确部署模型是 pull-based：

- 远程运行机自己 clone 或 pull 公开仓库。
- 远程运行机在本机安装依赖、同步 Codex skills、安装 Codex automations、
  安装长时运行服务。
- 开发机可以通过 SSH 触发远程机执行这些标准命令，但不能把本机未提交的
  工作区直接 rsync 过去作为正式部署方式。

项目后续会开源，所以部署流程不能依赖个人机器路径、私有 host、提交到仓库
里的密钥或 token。

## 目标

- 让远程长时运行机上的 Codex automation 部署可重复、可验证。
- 让开源用户能理解并复用安装流程。
- 全面审查并优化 skills、automation prompts、脚本、文档、测试和可观测性，
  让它们适合无人值守的 Codex 定时运行。
- launchd 只保留给长时非 LLM daemon。
- 旧 Claude CLI launchd 任务只作为 legacy/manual fallback，不再是默认生产路径。
- 每次远程部署后都输出清晰的验证摘要。

## 非目标

- 本轮不做完整部署框架，不做 rollback、release channel、状态面板。
- 不让开发机承担长时运行职责。
- 不把本机未提交文件推到运行机作为正式部署路径。
- 不立即删除所有旧 Claude CLI wrapper。
- 不自动处理用户侧 Codex app 登录、授权或凭据配置。

## 当前审查结论

当前迁移方向整体是对的：

- 旧 short LLM launchd plist 已经从默认 launchd 目录移到
  `launchd/disabled/claude/`。
- `docs/codex_automations.md` 已经说明：Codex automations 必须创建在实际
  运行交易流程的机器上。
- `scripts/sync_codex_skills.sh` 会从 canonical 的 `.claude/skills` 生成
  Codex 本地使用的 `.agents/skills`。
- `scripts/install_codex_automations.sh` 会写入 `~/.codex/automations`。

还需要补齐的问题：

- `README.md` 和 `scripts/setup.sh` 仍把 launchd + Claude CLI 描述成
  short LLM 定时任务的默认路径。
- 缺少开源友好的 pull-based 远程部署辅助脚本。
- 当前 Codex automation prompt 太短，不适合无人值守任务。
- Skills 需要系统性审查：路径、输出契约、失败处理、幂等性、副作用是否适合
  Codex automation。
- validator 模式文档仍假设通过 launchd 环境变量控制，但 short jobs 已迁到
  Codex automation。
- 测试还没有覆盖 automation TOML、skill sync 路径改写、文档漂移和 prompt 契约。
- `sector_pack` 的近期窗口依赖自然日期，在周末、节假日或数据滞后时会低估题材热度。

## 部署模型

正式的开源部署路径是 remote-local：

1. 用户登录 runtime host。
2. 用户 clone 或 pull 仓库。
3. 用户在这台 runtime host 上执行文档里的 setup/install 命令。
4. Codex automations 和 launchd services 都安装在这台机器本地。

开发者便利路径是 SSH-triggered pull-based：

1. 开发机读取 gitignored 的私有配置文件或环境变量。
2. 开发机 SSH 到远程 runtime host。
3. 远程机 clone 或 pull 仓库。
4. 远程机执行与开源用户完全相同的本地安装命令。
5. SSH 脚本只负责打印部署验证摘要。

SSH helper 不是唯一部署方式，只是方便开发机触发远程 staging 部署。

## 脚本边界

### `scripts/setup.sh`

`setup.sh` 改成通用项目环境初始化脚本：

- 检查 `uv`、`sqlite3`、Python 依赖。
- 执行 `uv sync --group dev`。
- 如有需要，初始化 `data/daily.db`。
- 确保 `data/trade_calendar.csv` 存在，缺失时刷新。
- 缺少 `.env` 时从 `.env.example` 创建。
- Telegram 验证改成可选；缺少凭据时提示下一步，不把 setup 判失败。
- 不再把 `claude` 当默认运行依赖。
- 不再默认安装 short LLM launchd jobs。

### `scripts/install_runtime_services.sh`

新增 runtime services 安装脚本，只处理长时非 LLM 进程：

- `com.user.stockwatchloop`
- `com.user.stockanomalyloop`
- `com.user.stockthemeloop`
- `com.user.stocktglistener` 只在显式启用时安装

这个脚本替代现在 `scripts/install_launchd.sh` 的默认定位。旧
`install_launchd.sh` 可以保留为兼容 wrapper，但文档里的默认入口应该变成
runtime services 安装。

### `scripts/install_codex_automations.sh`

保留它作为远程机本地的 Codex automation 安装器，但需要增强：

- 支持 `--dry-run` 或 `--print`，方便测试和 review。
- 支持把 automation 生成到指定目录，方便测试临时目录。
- 输出 job id、schedule、cwd、model、reasoning effort、status。
- 同 ID 重复安装时幂等覆盖。
- prompt 集中管理，方便审查。
- 要求或至少提示先执行 `scripts/sync_codex_skills.sh`。
- 明确 validator 策略：要么写进 prompt 契约，要么使用项目级配置，不能只依赖
  旧 launchd 环境变量文档。

### `scripts/deploy_remote_codex.sh`

新增给开发机使用的远程 staging 部署脚本：

- 读取 gitignored 的 `deploy.remote.env`。
- 提交 `deploy.remote.example.env`，里面只放占位字段和说明。
- 支持 `REMOTE_HOST`、`REMOTE_ROOT`、`REMOTE_REPO_URL`、`REMOTE_BRANCH`。
- SSH 到远程 host。
- 目录不存在时 clone 仓库。
- 目录存在时执行 `git fetch`、`git checkout`、`git pull --ff-only`。
- 在远程机执行标准安装序列：
  - `bash scripts/setup.sh`
  - `bash scripts/sync_codex_skills.sh`
  - `bash scripts/install_codex_automations.sh`
  - `bash scripts/install_runtime_services.sh`
  - `bash scripts/disable_legacy_claude_launchd.sh`
  - runtime verification commands
- 最后输出精简部署摘要。

这个脚本不能把私有凭据、host、token、个人绝对路径写进 tracked files。

## Skill 审查范围

每个被 Codex automation 调度的 skill 都要按下面 checklist 审查：

- 入口命令和必要 helper scripts 是否明确。
- Codex 运行路径是否使用 `.agents/skills/...`。
- `.claude/skills/...` 引用是否只是 canonical source 或 legacy 说明，而不是同步后
  仍会被误用的运行路径。
- 必须写入的输出文件是否明确。
- Telegram 推送是否必须走统一 `push.py`。
- 失败模式是否明确：缺数据、非交易日、网络失败、Telegram 失败、validator 失败、
  缺观察池、缺数据库。
- 是否不需要用户现场判断。
- 是否有幂等或防重复推送策略。

优先审查：

- `stock-premarket`：保留 `fetch_data.py -> lockup checks ->
  data/last_card.md -> push.py`，保留 allowed facts 约束和重复推送保护。
- `stock-intraday`：保留 09:30、09:45、11:30、14:30 的当前时间路由；明确缺少
  盘前观察池时怎么降级。
- `stock-postmarket`：迁出旧 `run_postmarket.sh` 后，仍要保留 `stock_basic` 刷新副作用。
- `stock-weekly`：保留 weekly review 已存在时跳过的幂等逻辑，除非显式 force。
- `stock-anomaly`、`watch_loop`、`theme_loop`：继续由 launchd 管理长进程，但要处理
  新边界下可能残留的 `.claude` 运行路径假设。

## Automation Prompt 契约

Automation prompt 不应该只是一句“Use stock-premarket skill”。每个 job prompt
应包含：

- 目标
- 使用哪个 skill
- 必须执行的步骤
- 必须产出的文件
- 必须使用的推送路径
- 失败汇报规则
- 最终回复格式

示例：

```text
使用本仓库里的 stock-premarket skill。

必须完成：
1. 通过 skill 脚本生成 fact pack。
2. 只使用 allowed facts 生成观察池卡片。
3. 写入 data/last_card.md。
4. 通过统一 push.py 路径推送。
5. 任一必需步骤失败时，报告具体失败步骤，不要声称成功。

最终回复：只返回一条简短运行摘要。
```

Prompt 不能调用 legacy `claude -p` wrapper。Codex 应该直接执行 skill 契约，避免
LLM 套 LLM。

## 验证与可观测性

### 部署验证

远程部署结束后必须输出摘要：

- 远程仓库路径。
- 当前 branch 和 commit hash。
- `.agents/skills/stock-*` 是否存在。
- `~/.codex/automations/<job>/automation.toml` 是否存在。
- 每个 job 的 id、schedule、cwd、model、reasoning effort、status。
- 当前剩余的 stock launchd jobs。
- 旧 short LLM launchd jobs 没有 loaded。
- shell 语法检查结果。
- 测试结果，或明确说明跳过原因。

### Runtime Doctor

新增 `scripts/doctor_codex_runtime.sh`，用于检查 runtime readiness，但不触发真实
交易卡推送：

- `uv`、`sqlite3`、Codex automation 目录存在。
- `.env` 必要字段存在，但不打印 secret。
- `data/daily.db` 存在，基本 schema 可查询。
- `data/trade_calendar.csv` 存在。
- `.agents/skills` 下有预期 stock skills。
- automation 的 `cwds` 指向当前仓库。
- 旧 short LLM launchd jobs 未 loaded。
- 长时 launchd jobs 已 loaded，或明确是故意未启用。
- 当前 validator 策略可见。

### 日志和证据

运行证据继续保留在项目本地，并保持 gitignored：

- Telegram 推送记录在 `push_log`。
- validator 违规记录在 `data/card_violations/`。
- fact pack 和 allowed latest 文件在 `data/`。
- 部署日志在 `logs/`。

脚本失败应尽量返回非 0；Codex prompt 必须要求报告失败步骤，而不是声称成功。

## 测试策略

新增聚焦迁移行为的测试：

- `tests/test_codex_automations.py`
  - 在临时目录生成 automation TOML。
  - 断言所有预期 job 都存在。
  - 断言 schedule 和文档一致。
  - 断言 prompt 包含 required outputs 和 failure reporting。
  - 断言 `cwds`、`model`、`reasoning_effort`、`status`、execution environment 字段存在。

- `tests/test_codex_skill_sync.py`
  - 用临时 fixture 跑 skill sync。
  - 断言 `.agents/skills` 被生成。
  - 断言运行路径被正确改写。
  - 断言普通说明文字不会被误改。

- `tests/test_docs_codex_migration.py`
  - 断言 README 不再把 short LLM jobs 描述成 launchd 默认。
  - 断言 validator 文档包含 Codex automation 策略。
  - 断言 legacy Claude launchd 只被描述为 fallback。

- 修复现有日期窗口测试：
  - 需要用数据库里最新可用交易数据作为近期窗口锚点，而不是自然今天。
  - 这样远程机在周末、节假日、数据延迟时不会低估题材热度。

## 文档计划

README 的默认叙事改成：

- Codex automations 运行短时 LLM jobs。
- launchd 运行长时 daemon。
- 本地开发使用 `scripts/setup.sh`。
- runtime host 部署使用 setup、skill sync、Codex automation install、runtime service install。
- 开发者远程 staging 使用 `scripts/deploy_remote_codex.sh`。

`docs/codex_automations.md` 改成详细 runbook：

- 远程机本地安装。
- SSH-triggered staging 部署。
- 重装 automations。
- 检查 job files。
- 禁用 legacy launchd jobs。
- 故障排查。

`docs/card_validator_enforce_switch.md` 需要更新：

- 分清 launchd daemon 策略和 Codex automation 策略。
- 说明 Codex jobs 的 validator mode 如何控制。
- 避免只更新旧 short-job launchd plist 的操作说明。

## 兼容策略

旧 Claude CLI wrappers 先保留：

- 保留 `code/run_premarket.sh`、`code/run_intraday.sh`、
  `code/run_postmarket.sh`、`scripts/weekly_loop.py`。
- 在注释和文档中标记为 legacy/manual fallback。
- 不再列为默认生产路径。
- 保留 `launchd/disabled/claude/` 作为迁移历史和 fallback templates。
- 保留 `scripts/disable_legacy_claude_launchd.sh` 作为迁移清理工具。

## 分阶段落地

### Phase 1：Spec 和测试

- 添加本设计 spec。
- 添加 automation generation、skill sync、docs drift 的静态测试。
- 修复日期窗口测试不稳定。

### Phase 2：脚本边界

- 重新定位 `scripts/setup.sh`。
- 新增 `scripts/install_runtime_services.sh`。
- 增强 `scripts/install_codex_automations.sh`。
- 新增 `deploy.remote.example.env`。
- 新增 `scripts/deploy_remote_codex.sh`。
- 新增 `scripts/doctor_codex_runtime.sh`。

### Phase 3：Skills 和 prompts

- 强化 automation prompts。
- 审查并更新被定时调用的 skills，补齐 Codex 路径和失败契约。
- 保留 postmarket 的 `stock_basic` 刷新。
- 保留 weekly 幂等逻辑。

### Phase 4：文档

- 更新 README。
- 扩展 `docs/codex_automations.md`。
- 更新 validator mode 文档。
- 明确标记 legacy Claude CLI launchd 路径只作为 fallback。

## 成功标准

- 开源用户能从 README 看懂 runtime model。
- 远程长时运行机能从仓库 pull，并用本机命令部署。
- 开发机能通过 SSH 触发同一套 pull-based 部署。
- Codex automation 安装支持 dry-run、可测试、幂等。
- 长时 launchd daemons 与短时 LLM jobs 分离。
- 测试能防止项目退回旧 launchd-only short-job 模型。
- Skills 和 prompts 适合无人值守 Codex 执行。
