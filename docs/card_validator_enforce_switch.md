# card_validator 模式切换

[[feedback-data-must-be-sourced]] 落地后，所有 stock skill 卡片推送前都会被
`code/lib/card_validator.py` 校验。模式由环境变量 `CARD_VALIDATOR_MODE` 控制。

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

## 切换 enforce 操作（6 处统一改）

### 1) 5 个 shell 启动脚本

```bash
sed -i '' 's/CARD_VALIDATOR_MODE=warn/CARD_VALIDATOR_MODE=enforce/' \
  code/run_tg_listener.sh \
  code/run_intraday.sh \
  code/run_postmarket.sh \
  code/run_premarket.sh \
  code/run_anomaly_loop.sh
```

### 2) 1 个 plist（weekly 走 `bash -lc` 不读 shell 脚本，只能在 plist 注入）

编辑 `launchd/com.user.stockweekly.plist` 里 `EnvironmentVariables` 块的
`CARD_VALIDATOR_MODE` 值。

### 3) 重启 daemon / 重装定时任务

```bash
# tg_listener daemon 立即生效
launchctl kickstart -k gui/$(id -u)/com.user.stocktglistener

# 5 个定时任务下次 launchd 触发时读新的 shell（无需手动重装）
# weekly 需要重装 plist 因为只有它在 plist 里塞 env：
bash scripts/install_launchd.sh  # 重装所有 plist
```

## 切完 enforce 后的预期

- 模型违规：被拦截卡推一条 ⚠️ 警告，原卡不发
- 用户收到的卡片只剩"经过校验的"，**虚构股票/反向数值 0 容忍**
- 模型偶发"任务已完成"元状态字 → 不含股票/数字 → 通过校验 → 仍会推送
  （这种另由 SKILL.md 输出契约文字约束治）

## 紧急回滚

```bash
sed -i '' 's/CARD_VALIDATOR_MODE=enforce/CARD_VALIDATOR_MODE=warn/' code/run_*.sh
# weekly plist 同步改回
launchctl kickstart -k gui/$(id -u)/com.user.stocktglistener
```

## 模式判定逻辑代码位置

- `code/lib/card_validator.py` — validate_card() 不感知模式，只返回 violations
- `.claude/skills/stock-premarket/scripts/push.py` — `_validate()` 读 env
- `scripts/tg_listener.py` — `_validate_card_for_push()` 读 env

env 不设时默认 `warn`（push.py 和 tg_listener.py 各自的 default）。
