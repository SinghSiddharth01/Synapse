"""On-device distillation: Segment -> Finding[]."""

from synapse_distiller.capability import (
    BUILTIN_RECORDS,
    CapabilityError,
    CapabilityRecord,
)
from synapse_distiller.config import ProviderConfig, SynapseConfig, load_config
from synapse_distiller.distiller import Distiller, DistillStats
from synapse_distiller.guards import (
    CanaryResult,
    PromptDropError,
    assert_prompt_conditioned,
    check_canary,
)
from synapse_distiller.promptpack import (
    PromptPack,
    PromptPackError,
    available_packs,
    load_pack,
    load_pack_by_name,
)

__all__ = [
    "BUILTIN_RECORDS",
    "CanaryResult",
    "CapabilityError",
    "CapabilityRecord",
    "DistillStats",
    "Distiller",
    "PromptDropError",
    "PromptPack",
    "PromptPackError",
    "ProviderConfig",
    "SynapseConfig",
    "assert_prompt_conditioned",
    "available_packs",
    "check_canary",
    "load_config",
    "load_pack",
    "load_pack_by_name",
]
