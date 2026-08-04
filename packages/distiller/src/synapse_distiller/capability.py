"""Per-model capability records and the segment-budget derivation.

This is what Plan A's segmenter reads to derive its budget. The budget is
**never** a shared hard-coded constant: swapping the distiller model, or editing
the prompt, must re-budget segmentation automatically.

Separation of concerns, and it matters:

  CapabilityRecord   facts about the *model on this hardware* — how much context
                     it actually serves, how fast it prefills. Measured.
  PromptPack         facts about the *prompt* — how many tokens the fixed part
                     costs. Derived from the pack's own text, never configured.

Keeping prompt overhead out of this record is the point. If it lived here, the
first prompt edit would leave it stale, the budget would come out too large, and
segments would be silently truncated at the context boundary.

Every number here must be *measured*, not taken from a model card. Plan B Task
B.5 says so for a specific reason, confirmed on 2026-08-04: the QAIRT bundle for
Qwen3-4B-Instruct-2507 advertises a far larger context than it serves.
"""

from __future__ import annotations

from dataclasses import dataclass

# A segment smaller than this cannot carry a meaningful turn, so a configuration
# that implies it is an error rather than a tight budget.
MIN_USABLE_SEGMENT_TOKENS = 500


class CapabilityError(ValueError):
    """A configuration that cannot produce a usable segment budget."""


@dataclass(frozen=True)
class CapabilityRecord:
    """Measured properties of one distiller model on one compute unit.

    usable_context        measured, not advertised
    prefill_toks_per_sec  measured; bounds how much prompt fits in the latency budget
    response_reserve      tokens held back for the model's own output
    """

    model: str
    usable_context: int
    prefill_toks_per_sec: float
    response_reserve: int
    notes: str = ""

    def segment_budget(
        self,
        prompt_overhead_tokens: int,
        *,
        max_seconds_per_call: float = 30.0,
        override: int | None = None,
    ) -> int:
        """Tokens of Segment content that may be rendered into one prompt.

        Two independent limits, whichever binds first:
          1. what fits in the context after prompt overhead and response reserve
          2. what can be *prefilled* inside the per-call latency budget

        `override` exists because operators legitimately want a smaller budget
        (faster calls, finer segments). It is validated rather than trusted: an
        override *above* what the context allows would silently truncate every
        prompt, so it is rejected.
        """
        from_context = self.usable_context - prompt_overhead_tokens - self.response_reserve
        from_latency = int(self.prefill_toks_per_sec * max_seconds_per_call)
        derived = min(from_context, from_latency)

        if override is not None:
            if override > from_context:
                raise CapabilityError(
                    f"segment_budget override of {override} tokens exceeds what "
                    f"{self.model!r} can actually hold ({from_context} = "
                    f"{self.usable_context} context - {prompt_overhead_tokens} prompt "
                    f"overhead - {self.response_reserve} response reserve). Every "
                    f"prompt would be truncated at the context boundary, losing the "
                    f"end of each segment silently. Lower the override, shorten the "
                    f"prompt pack, or use a model with more context."
                )
            if override < MIN_USABLE_SEGMENT_TOKENS:
                raise CapabilityError(
                    f"segment_budget override of {override} tokens is below the "
                    f"{MIN_USABLE_SEGMENT_TOKENS}-token minimum; findings would be "
                    f"distilled from fragments."
                )
            return override

        if derived < MIN_USABLE_SEGMENT_TOKENS:
            raise CapabilityError(
                f"Configuration for {self.model!r} implies a segment budget of "
                f"{derived} tokens, below the {MIN_USABLE_SEGMENT_TOKENS}-token "
                f"minimum (context limit gives {from_context}, latency limit gives "
                f"{from_latency}). Emitting segments this small would produce "
                f"findings from fragments. Fix the record or shorten the prompt pack "
                f"rather than lowering the minimum."
            )
        return derived


# Measured on Snapdragon X Elite X1E80100, Hexagon NPU v73, GenieX CLI v0.3.18,
# 2026-08-04.
#
# usable_context: a prompt reporting prompt_tokens=3809 succeeded; ~7000 was
# rejected with HTTP 400. QAIRT bundles bake context in at compile time and it
# cannot be changed at runtime — there is no --nctx on this path. 4096 is
# therefore a hard ceiling on this arm, not a tuning knob. Plan B Task B.5
# predicted exactly this ("qairt bundles are often 4K regardless of what the
# model claims on paper"); it is now confirmed.
#
# prefill_toks_per_sec: PROVISIONAL. The only timed call so far included model
# load, so it is not a clean number. Task B.6 must replace this with a
# seed-pinned measurement before any latency claim is made.
NPU_QWEN3_4B_INSTRUCT_2507 = CapabilityRecord(
    model="qualcomm/Qwen3-4B-Instruct-2507:W4A16",
    usable_context=4096,
    prefill_toks_per_sec=250.0,
    response_reserve=500,
    notes=(
        "QAIRT/W4A16, NPU-exclusive. usable_context measured 2026-08-04 and is a "
        "hard ceiling (no --nctx on the qairt path). prefill_toks_per_sec is "
        "PROVISIONAL — needs a seed-pinned measurement."
    ),
)

BUILTIN_RECORDS: dict[str, CapabilityRecord] = {
    NPU_QWEN3_4B_INSTRUCT_2507.model: NPU_QWEN3_4B_INSTRUCT_2507,
}
