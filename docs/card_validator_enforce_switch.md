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

当前默认策略仍是 `warn`：`push.py` 和 `tg_listener.py` 在 env 不设时都会默认
`warn`。当前还没有一条可执行命令能把所有 Codex automation short LLM jobs
切到 `enforce`；切换前需要先实现并选定项目级策略：

- 在 Codex automation prompt 里明确期望 `CARD_VALIDATOR_MODE=enforce`
- 新增 runtime config，让 `push.py` 读取统一配置
- 等 Codex automation 支持环境配置后，再通过 automation env 注入

完成策略实现后，再重新安装 automation 并做 doctor 检查：

```bash
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

### Codex automation short LLM jobs

`short LLM` jobs 由 `Codex automation` 触发，不再通过 short-job launchd plist
注入环境变量。盘前、盘中、盘后、周报这类短时 LLM 调度应在 Codex automation
侧落实 enforce 策略；在未完成项目级策略前，它们继续依赖 `push.py` 的默认
`warn` 行为。

旧 short-job launchd plist 不再保存在仓库里；它们不是 active job，不要重新安装为当前调度入口。

### launchd daemon

长时 `launchd daemon` 仍可通过 shell 脚本里的 `CARD_VALIDATOR_MODE` 控制，例如
`tg_listener`、`anomaly_loop`。如果要让这些 daemon 进入 `enforce`，更新对应
启动脚本中的环境变量后，重启服务使配置生效。

## 切完 enforce 后的预期

- 模型违规：被拦截卡推一条 ⚠️ 警告，原卡不发
- 用户收到的卡片只剩"经过校验的"，**虚构股票/反向数值 0 容忍**
- 模型偶发"任务已完成"元状态字 → 不含股票/数字 → 通过校验 → 仍会推送
  （这种另由 SKILL.md 输出契约文字约束治）

## 紧急回滚

当前可执行回滚只适用于长时 launchd daemon：把相关 shell 脚本里的
`CARD_VALIDATOR_MODE=enforce` 改回 `warn`，然后重启对应服务。

```bash
sed -i '' 's/CARD_VALIDATOR_MODE=enforce/CARD_VALIDATOR_MODE=warn/' bin/run_*.sh
launchctl kickstart -k gui/$(id -u)/com.user.stocktglistener
```

如果未来为 Codex automation short LLM jobs 落地了 runtime config 或 automation
env 方案，回滚应在同一配置源改回 `warn`，再运行：

```bash
bash scripts/install_codex_automations.sh
bash scripts/doctor_codex_runtime.sh
```

## 模式判定逻辑代码位置

- `stock_codex/market/card_validator.py` — validate_card() 不感知模式，只返回 violations
- `.agents/skills/stock-premarket/scripts/push.py` — `_validate()` 读 env
- `scripts/tg_listener.py` — `_validate_card_for_push()` 读 env

env 不设时默认 `warn`（push.py 和 tg_listener.py 各自的 default）。
