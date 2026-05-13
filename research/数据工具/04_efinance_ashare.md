# efinance + Ashare（免费实时分时方案）
抓取日期: 2026-05-12

## efinance
来源: https://github.com/Micro-sheep/efinance
- `pip install efinance`
- 数据源: 东方财富
- 能力: 日 K、5 分钟 K、实时报价、资金流（分钟级、按单数）、可转债、基金
- 最近 release 2025-03，3.7k star，活跃维护
- 已知问题: 高频被限流，README 自己推荐"TickFlow"作为备份

## Ashare（mpquant/Ashare）
来源: https://github.com/mpquant/Ashare
- 单文件 Ashare.py，无需 pip
- 数据源: 新浪 + 腾讯双核，自动故障切换
- 能力: 日 K、1/5/15/30/60 分钟、分时
- 稳定运行多年，更新不勤但接口稳定
- 适合极简方案，配合 akshare 用

## 实时性边界（重要）
所有"免费实时"本质都是 **快照接口（snapshot）**：
- 新浪 hq.sinajs.cn: 一次返回所有股票，更新粒度 ~3 秒
- 东财 push2.eastmoney.com: 同上，3-6 秒
- 腾讯 qt.gtimg.cn: 3-5 秒

**不是真正的 tick（逐笔成交）**。逐笔需要 L1 行情终端或 L2 付费。

## 盘中接力打板可行性
- 打板对延迟敏感度: 极高，秒级劣势就被埋单
- 免费快照延迟 3-6 秒 + Python 处理 1 秒 + 你手动下单 5-10 秒 = **15 秒延迟**
- 结论: **不可行做主动追板**。可行做：1) 涨停后做"接力候选名单"提示，等明日竞价 2) 监控自选股异动报警
