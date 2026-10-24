"""Read-only Telegram/TDLib ingestion foundation."""

from .collector import TelegramCollector
from .config import (
    ChatConfigError,
    ChatPolicy,
    TelegramCollectorConfig,
    load_chats_config,
    parse_chats_config,
)
from .models import (
    CollectorEvent,
    DeletedMessageEvent,
    ForwardOrigin,
    IgnoredMessageMetadata,
    MessageEntity,
    SourceMessage,
)
from .normalizer import TelegramNormalizationError, normalize_tdlib_message
from .tdlib import (
    CTypesTDLibTransport,
    FakeTDLibTransport,
    InteractiveAuthCallbacks,
    TDLibAuthInputRequired,
    TDLibChat,
    TDLibClient,
    TDLibError,
    TDLibRequestError,
    TDLibRequestTimeout,
    TDLibReadOnlyViolation,
    TDLibSettings,
    TDLibTransportError,
    TDLibUser,
)

__all__ = [
    "CTypesTDLibTransport",
    "ChatConfigError",
    "ChatPolicy",
    "CollectorEvent",
    "DeletedMessageEvent",
    "FakeTDLibTransport",
    "ForwardOrigin",
    "IgnoredMessageMetadata",
    "InteractiveAuthCallbacks",
    "MessageEntity",
    "SourceMessage",
    "TDLibAuthInputRequired",
    "TDLibChat",
    "TDLibClient",
    "TDLibError",
    "TDLibRequestError",
    "TDLibRequestTimeout",
    "TDLibReadOnlyViolation",
    "TDLibSettings",
    "TDLibTransportError",
    "TDLibUser",
    "TelegramCollector",
    "TelegramCollectorConfig",
    "TelegramNormalizationError",
    "load_chats_config",
    "normalize_tdlib_message",
    "parse_chats_config",
]
