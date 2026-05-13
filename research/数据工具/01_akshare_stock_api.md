# AKShare A 股接口（官方文档抓取）
来源: https://akshare.akfamily.xyz/data/stock/stock.html （v1.18.60）
抓取日期: 2026-05-12

## 日线
- stock_zh_a_hist（东方财富，无 token，支持前/后复权，推荐）
- stock_zh_a_daily（新浪，易封 IP）

## 分钟线
- stock_zh_a_minute（新浪，1/5/15/30/60 分钟）
- stock_zh_a_hist_min_em（东财，1 分钟只返回近 5 个交易日）
- stock_zh_a_hist_pre_min_em（东财，含盘前）

## 实时行情
- stock_zh_a_spot_em（东财，全 A 快照，最常用）
- stock_zh_a_spot（新浪，频繁会封 IP）
- stock_individual_spot_xq（雪球个股）

## 涨停板（打板专题）
- stock_zt_pool_em 涨停股池
- stock_zt_pool_previous_em 昨日涨停
- stock_zt_pool_strong_em / stock_zt_qsh_em 强势股池
- stock_zt_pool_sub_new_em / stock_zt_csxjc_em 次新股池
- stock_zt_pool_zbgc_em / stock_zt_zbgc_em 炸板股池
- stock_zt_pool_dtgc_em / stock_zt_dtgc_em 跌停股池

## 龙虎榜
- stock_lhb_em（东财，最常用）

## 概念板块
- stock_board_concept_name_em
- stock_zh_a_cxg_ths（同花顺概念）

## 北向资金 / 资金流
- stock_hk_hshk_fund_flow_em 沪深港通资金流
- stock_money_flow_em / stock_mf_em 资金流向
- stock_individual_fund_flow 个股资金流

## 问财
- stock_hot_rank_wc 问财热度榜

## Token
绝大多数无需 token；新浪源接口高频会被封 IP。
