# 推送机器人对比
抓取日期: 2026-05-12

## 飞书自定义机器人（推荐）
- 文档: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- 完全免费
- 频率: 100 次/分钟，5 次/秒，单条 ≤20KB
- 支持文本、富文本、卡片、图片
- 配置: 群设置 → 群机器人 → 自定义机器人 → 拿 webhook URL
- 可选 HmacSHA256 签名校验

```python
import requests
requests.post(
    "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
    json={"msg_type":"text","content":{"text":"涨停预警: 600519"}}
)
```

## Server 酱 Turbo
- 官网: https://sct.ftqq.com
- GitHub 登录 → 拿 SCKEY → 微信扫码绑定
- 免费版: 5 条/天（2023 改版后）
- 适合极少量重要信号（如止损/止盈）

## PushPlus
- 官网: http://www.pushplus.plus
- 微信扫码 → 复制 token
- 免费版: 200 条/天（最大方）
- 直接推到个人微信，体验最好

## 企业微信群机器人
- 免费，20 条/分钟限制
- 需要先有企业微信群

## 推荐组合
盘中实时报警走飞书（频率高、富文本好）；盘后复盘摘要走 PushPlus（直达个人微信看着舒服）。
