"""ClaudeCliProvider — Claude through the `claude` CLI, on a personal subscription.

The third distiller arm, and the only one that needs no credential of any kind.
`AnthropicProvider` (anthropic_provider.py) needs an API key per developer;
`NPUProvider` needs the one X Elite box; `AIC100Provider` shares a key capped
at ~20 requests/hour. This one shells out to the `claude` binary in headless
mode, which is already authenticated against whatever subscription the
developer signed in with:

    claude -p "<prompt>" --output-format json

Why a subprocess and not the MCP `sampling/createMessage` capability, which is
the protocol's own answer to "ask the connected client to run inference":
Claude Code does not implement sampling as a client (the official client matrix
lists it unsupported), the capability is deprecated protocol-wide as of MCP
revision 2026-07-28, and — decisively — the passive worker is a separate
process with no MCP session in either direction, so there is no client for it
to ask. Investigated 2026-08-05; that route is closed, this one is open.

THREE THINGS THAT MAKE THIS DIFFERENT FROM EVERY OTHER PROVIDER HERE
--------------------------------------------------------------------

1. `usage.input_tokens` IS NOT THE PROMPT SIZE. Measured on 2.1.223, a call
   whose prompt was ~31,000 tokens reported:

       input_tokens = 10
       cache_creation_input_tokens = 13456
       cache_read_input_tokens     = 17536

   `input_tokens` is the uncached remainder only, and the CLI carries its own
   large system context on every call, so nearly all of it is always cached.
   `distiller/guards.py:assert_prompt_conditioned` fails a call whose
   `input_tokens <= 1` — because a model that dropped its prompt emits
   schema-plausible findings invented from nothing, straight into shared team
   memory. Reporting 10 for a 31,000-token prompt would leave that guard
   technically passing and functionally decorative. So `ModelUsage.input_tokens`
   here is the SUM of all three fields: the true prompt size under every cache
   configuration, which is what the guard is actually asking about.

2. `claude -p` IS AN AGENT, NOT A COMPLETION ENDPOINT. Left alone it can read
   files, run commands, and search the web mid-answer — none of which belongs
   in a pure text transform, and any of which could pull content into a
   Finding that the Segment never contained. `--allowedTools ""` and
   `--max-turns 1` reduce it to one turn of text in, text out.

3. IT TAKES ONE PROMPT STRING, NOT A LIST OF TURNS. Every other arm here hands
   the model a real conversation, so the few-shots arrive as *its own* prior
   assistant turns and are structurally distinct from the material to work on.
   `-p` has one argument, and until 2026-08-06 this provider produced it by
   joining every non-system message with blank lines — which erases the only
   thing marking which text was an example. See `_flatten_prompt` for the
   measured consequence and what replaced it.

WHAT THIS SHARES WITH THE OTHER CLOUD ARM: it is off by default
(`SYNAPSE_DISTILLER=claude-cli` opts in), and it sends raw Agent Session
content to a third party, so the privacy invariant the NPU exists to protect
does not hold on this arm. The demo runs the NPU. See
docs/brainstorming/2026-08-03-hybrid-frontier-local-amendment.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 180.0

# The CLI's JSON is not a stable contract — it is a tool's output shape, and an
# upgrade could rename a field without anyone considering this a breaking
# change. Every field we depend on is asserted at the point of use with a
# message naming the CLI, so a rename surfaces as one clear error rather than a
# distiller that quietly stops producing findings.
_REQUIRED_FIELDS = ("result", "usage")

# --- flattening a chat into one prompt argument -----------------------------------
#
# `distiller.prompt.build_messages` emits system, then one user/assistant pair per
# few-shot, then the real request. On an arm that takes a message list, those pairs
# ARE examples: the wire format says the assistant already said that, about
# something else. On this arm there is one string, and the roles have to survive in
# the text or not at all.
#
# WHAT HAPPENED WHEN THEY DID NOT. Joining the turns with blank lines — this
# provider's behaviour until 2026-08-06 — produced a prompt in which the few-shot
# inputs are labelled "developer:"/"agent:" exactly like the rendered Segment, the
# few-shot outputs are bare JSON objects with nothing marking them as answers, and
# the pack's own suffix then says "rewrite the session above as notes". The session
# above was the examples too, so the model did as it was told. Measured live in W7
# (docs/overnight/w7-live-evidence.md, F1): one contributed paragraph in, nine
# findings out, and six of them were v4-condense's own two few-shot outputs
# paraphrased and filed as a named engineer's verified experience. Reproduced on
# both runs; 2 + 4 example notes is exactly the six.
#
# So the blocks are named. The markers are deliberately loud and deliberately not
# JSON: they must be unmistakable inside a prompt whose payload is a transcript
# that could itself contain fences, braces or headings.
#
# These cost tokens, and prompt tokens on other arms are budgeted
# (`promptpack.calibration.overhead_tokens` feeds the segment budget). They are
# free here on both counts: this text exists only on this arm, and this arm has no
# context ceiling to budget against — `segment_budget` is pinned in
# config/synapse.toml and `max_tokens` is accepted and ignored here. Nothing in
# scripts/calibrate_prompt.py's measurement path sees them.
_EXAMPLE_INPUT_MARK = (
    "=== EXAMPLE {n} · INPUT — an illustration of the format. "
    "NOT the material you are working on. ==="
)
_EXAMPLE_REPLY_MARK = (
    "=== EXAMPLE {n} · THE REPLY THAT WAS WANTED — shows the shape only. "
    "It describes a different session; never repeat or paraphrase its content. ==="
)
_REQUEST_MARK = (
    "=== THE MATERIAL TO WORK ON — everything above this line was an example. "
    "Answer about what follows and about nothing above it. ==="
)


def _flatten_prompt(messages: list[dict[str, Any]]) -> str:
    """The non-system turns as one string, with the examples marked as examples.

    Leading user/assistant *pairs* are few-shots by construction — that is the
    only thing `build_messages` puts there — and whatever remains after them is
    the request. A caller that sends a single user turn (`guards.check_canary`,
    any ad-hoc probe) has no examples to disambiguate from, so it gets no markers
    at all and the prompt is byte-identical to what this provider sent before.
    """
    turns = [m for m in messages if m.get("role") != "system"]

    blocks: list[str] = []
    examples = 0
    index = 0
    while (
        index + 1 < len(turns)
        and turns[index].get("role") == "user"
        and turns[index + 1].get("role") == "assistant"
    ):
        examples += 1
        blocks.append(_EXAMPLE_INPUT_MARK.format(n=examples))
        blocks.append(str(turns[index].get("content", "")))
        blocks.append(_EXAMPLE_REPLY_MARK.format(n=examples))
        blocks.append(str(turns[index + 1].get("content", "")))
        index += 2

    rest = [str(m.get("content", "")) for m in turns[index:]]
    if examples and rest:
        blocks.append(_REQUEST_MARK)
    blocks.extend(rest)
    return "\n\n".join(blocks)


class ClaudeCliCallError(RuntimeError):
    """The `claude` CLI could not be run, or answered in a shape we cannot read."""


class ClaudeCliProvider(ModelProvider):
    """Claude via the local `claude` binary in headless (`-p`) mode."""

    provider_id = "claude-cli"

    def __init__(
        self,
        model: str | None = None,
        *,
        binary: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,   # accepted and ignored; see below
    ) -> None:
        self.model = model or os.environ.get("SYNAPSE_CLAUDE_CLI_MODEL", DEFAULT_MODEL)
        self.binary = binary or os.environ.get("SYNAPSE_CLAUDE_CLI_BIN", "claude")
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("SYNAPSE_CLAUDE_CLI_TIMEOUT", DEFAULT_TIMEOUT)
        )
        # `max_tokens` exists so this provider is constructible from the same
        # config as the others (config.provider.max_tokens is 900). The CLI has
        # no equivalent flag, so honouring it would mean pretending to cap
        # something we cannot cap — worse than ignoring it visibly.
        self.max_tokens = max_tokens

    @property
    def capabilities(self) -> ProviderCapabilities:
        # No schema is enforced end to end: `-p` returns whatever the model
        # wrote. Prompt-instructed JSON plus tolerant parsing, exactly like the
        # NPU arm — and for the same reason, this must stay False so
        # `_parse_json_tolerantly` keeps running.
        return ProviderCapabilities(native_structured_output=False, streaming=False)

    def _argv(self, prompt: str, system: str | None) -> list[str]:
        argv = [
            self.binary, "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            # An agent with tools could read a file or run a command and fold
            # the result into a Finding the Segment never contained.
            "--allowedTools", "",
            "--max-turns", "1",
        ]
        if system:
            argv += ["--append-system-prompt", system]
        return argv

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        if shutil.which(self.binary) is None:
            raise ClaudeCliCallError(
                f"{self.binary!r} is not on PATH. This arm runs Claude through the "
                f"CLI on your own subscription; install it, or use "
                f"SYNAPSE_DISTILLER=npu."
            )

        # `build_messages` emits the OpenAI shape — system first, then user.
        # The CLI takes one prompt argument plus an optional appended system
        # prompt, so the roles are split rather than concatenated: folding the
        # system text into the prompt would put the pack's instructions where
        # the Segment's content belongs. The remaining turns are flattened with
        # their roles preserved as text — see `_flatten_prompt`.
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        prompt = _flatten_prompt(messages)

        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *self._argv(prompt, system or None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, err = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            process.kill()
            await process.wait()
            raise ClaudeCliCallError(
                f"`{self.binary} -p` did not answer within {self.timeout}s"
            ) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if process.returncode != 0:
            detail = (err or b"").decode(errors="replace").strip()[:300]
            raise ClaudeCliCallError(
                f"`{self.binary} -p` exited {process.returncode}: {detail or '(no stderr)'}"
            )

        try:
            payload = json.loads(raw.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise ClaudeCliCallError(
                f"`{self.binary} -p --output-format json` did not return JSON. "
                f"The CLI's output shape is not a stable contract; check its "
                f"version. First 200 bytes: {raw[:200]!r}"
            ) from exc

        missing = [f for f in _REQUIRED_FIELDS if f not in payload]
        if missing:
            raise ClaudeCliCallError(
                f"`{self.binary} -p` JSON is missing {', '.join(missing)} — the CLI's "
                f"output shape changed. Keys present: {sorted(payload)[:12]}"
            )
        if payload.get("is_error"):
            raise ClaudeCliCallError(
                f"`{self.binary} -p` reported an error: "
                f"{str(payload.get('result'))[:200]}"
            )

        text = payload["result"] or ""
        data: Any = text
        schema_valid = True
        if response_schema is not None:
            from synapse_providers.openai_compat import _parse_json_tolerantly

            parsed = _parse_json_tolerantly(text)
            schema_valid = parsed is not None
            data = parsed if parsed is not None else text

        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=_true_prompt_tokens(payload["usage"]),
                output_tokens=int(payload["usage"].get("output_tokens", 0) or 0),
            ),
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )


def _true_prompt_tokens(usage: dict[str, Any]) -> int:
    """The whole prompt, cached parts included.

    See this module's docstring, point 1: `input_tokens` alone is the uncached
    remainder, which on a CLI that ships a large cached system context is a tiny
    fraction of what the model actually read. `assert_prompt_conditioned` is
    asking "did the prompt reach the model at all", so it has to be given the
    number that answers that question.
    """
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )
