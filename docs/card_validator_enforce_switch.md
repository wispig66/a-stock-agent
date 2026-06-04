# card_validator 模式切换

[[feedback-data-must-be-sourced]] 落地后，所有 stock skill 卡片推送前都会被
`stock_codex/market/card_validator.py` 校验。模式由环境变量 `CARD_VALIDATOR_MODE` 控制。

## 三档

| 模式 | 行为 | 用途 |
|---|---|---|
| `off` | 完全跳过校验 | 紧急回滚 |
| `warn`（**默认**） | 违规写 `data/card_violations/<ts>_<source>.json` 审计日志，原卡照推 + 追一条 ⚠️ 简讯 | 上线初期观察误伤 |
| `enforce` | 拒推原卡，改推一条 ⚠️ 拦截卡含违规摘要 | 稳定后正式开启 |

## 当前状态

- **`warn`**（2026-05-18 上线）
- 观察期 1 周，到 **2026-05-25** 评估误伤情况：
  - 看 `data/card_violations/` 累积 → 误伤率高就调容差
  - 真伪虚构比例 > 80% 且零真伤 → 切 `enforce`

## 切换 enforce 操作

当前短时 LLM jobs 通过 `.agents/skills/stock-premarket/scripts/push.py` 推送。Codex automation prompt 已要求 `CARD_VALIDATOR_MODE=enforce`，如果 env 不设，`push.py` 仍会按 source 对 scheduled jobs 走 enforce 默认策略。切换或回滚时只需要维护这条统一 push 路径，不再维护旧入站 listener。

- 在 Codex automation prompt 里明确期望 `CARD_VALIDATOR_MODE=enforce`
- 确认所有 scheduled skills 都通过统一 `push.py` 路径推送
- 重新安装 automation，让本机 `~/.codex/automations/` 里的 prompt 生效

完成策略实现后，再重新安装 automation 并做 doctor 检查：

```bash
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

### Codex automation short LLM jobs

`short LLM` jobs 由 `Codex automation` 触发，不再通过 short-job launchd plist
注入环境变量。盘前、盘中、盘后、周报这类短时 LLM 调度应在 Codex automation
侧落实 enforce 策略。当前 `push.py` 会对 scheduled sources 默认 enforce。

旧 short-job launchd plist 不再保存在仓库里；它们不是 active job，不要重新安装为当前调度入口。

### launchd daemon

长时 `launchd daemon` 仍可通过 shell 脚本里的 `CARD_VALIDATOR_MODE` 控制，例如 `anomaly_loop`、`watch_loop` 和 `theme_emergence_loop`。如果要让这些 daemon 进入 `enforce`，更新对应启动脚本中的环境变量后，重启服务使配置生效。

## 切完 enforce 后的预期

- 模型违规：被拦截卡推一条 ⚠️ 警告，原卡不发
- 用户收到的卡片只剩"经过校验的"，**虚构股票/反向数值 0 容忍**
- 模型偶发"任务已完成"元状态字 → 不含股票/数字 → 通过校验 → 仍会推送
  （这种另由 SKILL.md 输出契约文字约束治）

## 紧急回滚

当前可执行回滚分两类。短时 Codex automation 回滚时，调整 prompt 或环境后重装 automation；长时 launchd daemon 回滚时，把相关 shell 脚本里的 `CARD_VALIDATOR_MODE=enforce` 改回 `warn`，然后重启对应服务。

```bash
sed -i '' 's/CARD_VALIDATOR_MODE=enforce/CARD_VALIDATOR_MODE=warn/' bin/run_*.sh
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

如果未来为 Codex automation short LLM jobs 落地了 runtime config 或 automation
env 方案，回滚应在同一配置源改回 `warn`，再运行：

```bash
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

## 模式判定逻辑代码位置

- `stock_codex/market/card_validator.py` — validate_card() 不感知模式，只返回 violations
- `.agents/skills/stock-premarket/scripts/push.py` — `_validate()` 读 env，并按 scheduled source 设置默认策略

env 不设时，`push.py` 对 scheduled source 默认 enforce，对手工 source 默认 warn。
