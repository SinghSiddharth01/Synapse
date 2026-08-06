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

TWO THINGS THAT MAKE THIS DIFFERENT FROM EVERY OTHER PROVIDER HERE
------------------------------------------------------------------

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
        # the Segment's content belongs.
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")

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
