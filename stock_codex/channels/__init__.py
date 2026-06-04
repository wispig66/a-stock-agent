"""Channel gateway primitives.

The gateway is intentionally small: business code should depend on these
models and the gateway, while platform details stay in adapters.
"""

from stock_codex.channels.core import (
    Capabilities,
    ChannelError,
    ChannelGateway,
    ChannelAdapter,
    ChannelMessage,
    Delivery,
    FeishuAdapter,
    MockAdapter,
    get_default_gateway,
    load_env_file,
    reset_default_gateway_for_tests,
)
from stock_codex.channels.wecom import WeComAdapter
from stock_codex.channels.weixin import WeixinAdapter

__all__ = [
    "Capabilities",
    "ChannelError",
    "ChannelGateway",
    "ChannelAdapter",
    "ChannelMessage",
    "Delivery",
    "FeishuAdapter",
    "WeComAdapter",
    "WeixinAdapter",
    "MockAdapter",
    "get_default_gateway",
    "load_env_file",
    "reset_default_gateway_for_tests",
]
