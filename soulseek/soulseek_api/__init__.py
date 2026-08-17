"""soulseek_api — a reusable client for the slskd Soulseek daemon.

    from soulseek_api import SoulseekClient
"""

from .client import DEFAULT_URL, SoulseekClient
from .errors import (
    APIError,
    AuthenticationFailed,
    ConnectionFailed,
    DownloadFailed,
    SearchTimeout,
    SoulseekError,
)
from .models import SearchFile, SearchResponse, Transfer

__version__ = "0.1.0"

__all__ = [
    "SoulseekClient",
    "DEFAULT_URL",
    "SearchFile",
    "SearchResponse",
    "Transfer",
    "SoulseekError",
    "ConnectionFailed",
    "AuthenticationFailed",
    "APIError",
    "SearchTimeout",
    "DownloadFailed",
]
