"""ModelProvider implementations."""

from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.fake import FakeProvider
from synapse_providers.npu import NPUProvider
from synapse_providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "FakeProvider",
    "ModelProvider",
    "NPUProvider",
    "OpenAICompatibleProvider",
    "ProviderCapabilities",
]
