# pywencai （同花顺问财非官方 SDK）
来源: https://github.com/zsrl/pywencai
抓取日期: 2026-05-12

## 安装
```
pip install pywencai
```
需要 Node.js v16+（要执行 hexin-v.bundle.js 生成动态参数）。

## 关键变化（2024-2025）
- **Cookie 现在是必填参数**，问财改了登录策略。
- 实现：通过 fake_useragent 随机 UA + 动态 hexin-v 参数对抗反爬。

## 调用
```python
import pywencai
res = pywencai.get(
    query='今日涨停 非ST 流通市值小于100亿 连板数大于1',
    cookie='xxx',  # 从浏览器复制
    loop=True
)
```

## 限额
- 未登录: 单次最多 1000 条
- 登录: 5000 条
- 高频会被屏蔽，作者明确反对高频调用

## 适合做什么（盘前选股最强）
- 自然语言筛股: "昨日涨停今日低开高走"、"5 日内首板 + 题材 = AI"、"龙虎榜机构净买入 + 次日竞价"
- 题材轮动追踪: "近 5 日上涨概念排名"
- 不需要自己拼复杂筛选条件

## 不适合做什么
- 不适合实时盘中高频调用（建议 ≥30 秒间隔）
- Cookie 会过期，需要每隔几天/周手动更新
- 商业用途有版权风险

## 替代
- akshare 的 stock_hot_rank_wc（功能弱很多）
- 自己用 selenium 抓问财（更不稳定）
