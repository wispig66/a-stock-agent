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
    TelegramAdapter,
    get_default_gateway,
    load_env_file,
    reset_default_gateway_for_tests,
)

__all__ = [
    "Capabilities",
    "ChannelError",
    "ChannelGateway",
    "ChannelAdapter",
    "ChannelMessage",
    "Delivery",
    "FeishuAdapter",
    "MockAdapter",
    "TelegramAdapter",
    "get_default_gateway",
    "load_env_file",
    "reset_default_gateway_for_tests",
]
