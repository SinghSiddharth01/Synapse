"""ModelProvider implementations."""

from synapse_providers.aic100 import AIC100Provider
from synapse_providers.base import ModelProvider, ProviderCapabilities
from synapse_providers.fake import FakeProvider
from synapse_providers.npu import NPUProvider
from synapse_providers.openai_compat import OpenAICompatibleProvider
from synapse_providers.recording import CallLog, LLMCall, RecordingProvider

__all__ = [
    "AIC100Provider",
    "CallLog",
    "FakeProvider",
    "LLMCall",
    "ModelProvider",
    "NPUProvider",
    "OpenAICompatibleProvider",
    "ProviderCapabilities",
    "RecordingProvider",
]
