from __future__ import annotations

from ..config import Settings
from .client import AvtovincodClient, Provider, ProviderError, ProviderResponse, call_accidents_rt
from .mock import MockProvider

__all__ = ["AvtovincodClient", "MockProvider", "Provider", "ProviderError", "ProviderResponse",
           "call_accidents_rt", "make_provider"]


def make_provider(settings: Settings) -> Provider:
    if settings.provider_mode == "live":
        return AvtovincodClient(settings.avc_token, settings.avc_base_url, settings.avc_rate_per_minute)
    return MockProvider()
