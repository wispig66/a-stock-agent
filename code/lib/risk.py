"""组合层风控计算。

3 个纯函数对外：
- load_risk_config()  读 risk_config.yaml，缺失走默认值 + stderr warning
- compute_exposure()  根据持仓 + 实时价算总仓位
- preflight_check()   决定是否需要横幅、返回剩余可用额度

设计原则：失败兜底，风控模块异常不能阻塞 L1 推送。
"""
from __future__ import annotations
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "risk_config.yaml"

DEFAULT_CONFIG = {
    "total_capital": 500000,
    "max_total_exposure_pct": 70,
    "max_single_position_pct": 30,
}


def load_risk_config() -> dict:
    """读 risk_config.yaml；缺失/解析失败走默认值 + stderr warning。"""
    if not CONFIG_FILE.exists():
        print(
            f"[risk] risk_config.yaml 未找到（{CONFIG_FILE}），使用默认值",
            file=sys.stderr,
        )
        return dict(DEFAULT_CONFIG)
    try:
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"[risk] risk_config.yaml 解析失败 ({e})，使用默认值", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in raw:
            cfg[k] = raw[k]
    return cfg
