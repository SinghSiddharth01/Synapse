"""Segment rendering and message assembly.

The prompt *text* now lives in config/prompts/*.toml as a PromptPack — see
promptpack.py for why. This module holds only the two things that are code
rather than content: how a Segment is flattened into text, and how a pack plus a
Segment become a message list.
"""

from __future__ import annotations

from collections.abc import Collection

from synapse_contracts import Segment

from synapse_distiller.promptpack import PromptPack

# Mirrors AgentEvent.kind. Kept as a module constant so config can validate a
# configured kind against it instead of failing at render time on a typo.
VALID_KINDS: frozenset[str] = frozenset({"text", "thinking", "tool_use", "tool_result"})

# Assistant prose is already a frontier model's distillation of its own work,
# which is why Plan A.5 says to weight retained content toward it. Restricting
# to it also shrinks the leak surface: identifiers mostly live in tool_use
# arguments and tool_result output, so most of what rule 1 forbids quoting
# simply never enters the prompt.
DEFAULT_KINDS: tuple[str, ...] = ("text",)

# How much structure surrounds each event's content in the prompt.
#
#   labelled  "assistant (tool_use: bash): pytest ..."  — role, kind and tool
#             name on every block. Necessary once tool events are in play,
#             since "which tool failed" is often the load-bearing detail.
#   content   the event content alone, blocks separated by blank lines. Only
#             coherent when the kinds are homogeneous — with text-only there is
#             no tool name to lose and the labels are pure overhead.
VALID_RENDER_STYLES: frozenset[str] = frozenset({"labelled", "content"})

# "labelled" despite "content" being the smaller prompt. Measured back-to-back
# 2026-08-04, same pack and kinds, temperature 0:
#
#   content   1661 prompt tokens · seg-001 decision FACTUALLY INVERTED
#   labelled  1673 prompt tokens · seg-001 decision correct
#
# The saving is 12 tokens (0.7%) — the role labels are ~3 tokens each. That buys
# nothing, and the content arm inverted a fact. See docs on the inversion.
DEFAULT_RENDER_STYLE = "labelled"

# Who said it, in terms that cannot collide with the chat envelope.
#
# The rendered segment is carried inside a role=user message, and the few-shots
# above it are literal user/assistant turns. Labelling transcript lines "user:"
# and "assistant:" therefore uses both words in two senses in one prompt — the
# envelope's turn structure and the observed session's participants. Renaming
# removes the collision.
#
# It also matches CONTEXT.md, which lists "user" under _Avoid_ for Contributor
# and "assistant" under _Avoid_ for Agent.
#
# The distinction is worth keeping rather than dropping: developer turns carry
# intent and correction ("no, we tried that"), agent turns carry reasoning and
# conclusions. A finding's type often depends on which one it came from.
ROLE_LABELS: dict[str, str] = {
    "user": "developer",
    "assistant": "agent",
    "system": "system",
}


def render_segment(
    segment: Segment,
    kinds: Collection[str] | None = None,
    style: str = DEFAULT_RENDER_STYLE,
) -> str:
    """Flatten a Segment into the text the model reads.

    `kinds` selects which AgentEvent kinds reach the model; None means all of
    them. `style` selects how much structure wraps them — see
    VALID_RENDER_STYLES.

    Metadata beyond role/kind/tool_name is never rendered in either style: cwd
    and git_branch are attribution concerns, and putting them in the prompt
    invites the model to quote them.
    """
    if style not in VALID_RENDER_STYLES:
        raise ValueError(
            f"Unknown render style {style!r}; valid: {sorted(VALID_RENDER_STYLES)}"
        )

    selected = VALID_KINDS if kinds is None else set(kinds)
    blocks: list[str] = []
    for event in segment.events:
        if event.kind not in selected:
            continue
        if style == "content":
            blocks.append(event.content)
            continue
        role = ROLE_LABELS.get(event.role, event.role)
        label = role
        if event.kind == "tool_use":
            label = f"{role} (tool_use: {event.tool_name or 'unknown'})"
        elif event.kind == "tool_result":
            label = f"tool_result ({event.tool_name or 'unknown'})"
        elif event.kind == "thinking":
            label = f"{role} (thinking)"
        blocks.append(f"{label}: {event.content}")
    return "\n\n".join(blocks)


def build_messages(
    segment: Segment,
    pack: PromptPack,
    kinds: Collection[str] | None = None,
    style: str = DEFAULT_RENDER_STYLE,
) -> list[dict[str, str]]:
    """System prompt + the pack's few-shots + the rendered Segment.

    The task suffix, when the pack defines one, is appended to the *same* user
    message as the segment rather than sent separately — the point is to restate
    the task adjacent to the data, and a separate message would put another turn
    boundary between them.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": pack.system}]
    for shot in pack.few_shots:
        messages.append({"role": "user", "content": shot.input})
        messages.append({"role": "assistant", "content": shot.output})
    messages.append(
        {
            "role": "user",
            "content": render_segment(segment, kinds, style) + pack.task_suffix,
        }
    )
    return messages


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["learning", "decision", "dead_end", "open_question"],
                    },
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
        }
    },
    "required": ["findings"],
}
