"""AnthropicProvider — Claude Opus 5 via the Messages API.

Exists so several developers can run the full distiller loop in parallel
without contending for the single NPU box or the Cirrascale key's
~20-req/hour ceiling (see aic100.py's module docstring for that limit).

STANDALONE ModelProvider, deliberately NOT an OpenAICompatibleProvider
subclass. Four structural breaks make subclassing wrong:

  1. Auth is `x-api-key` + `anthropic-version: 2023-06-01` headers, not a
     Bearer token. OpenAICompatibleProvider._headers() unconditionally
     builds `Authorization: Bearer <key>`; the Messages API does not accept
     that shape at all. (Moot at the header level here -- the official SDK
     sets these headers itself -- but it is still why a header-swap override
     on the base class would not have worked either.)
  2. OpenAICompatibleProvider._build_body sends `temperature` unconditionally,
     which is a hard 400 on Opus-tier models (Opus 5, 4.8, 4.7, and Fable 5
     all reject sampling parameters outright). There is no override point
     that removes a field the base class always adds -- it has to never be
     added in the first place.
  3. The system prompt is a top-level `system` field on the request, not
     `messages[0]`. `synapse_distiller.prompt.build_messages` DOES emit
     `{"role": "system", ...}` as the first message -- matching the OpenAI
     chat-completions shape every other provider in this package speaks --
     so this provider must hoist it out before calling the API.
  4. The response is `response.content`, a list of content blocks (text /
     thinking / tool_use / ...), not `choices[0].message.content`.

We use the official `anthropic` SDK (AsyncAnthropic) rather than raw httpx --
see the `anthropic` dependency in pyproject.toml. The SDK gets retries, typed
exceptions, and the exact wire shape (including the two headers in point 1)
right for free; re-deriving that over httpx would just recreate the SDK.

Six corrections applied here, each commented at its call site below:

  1. RESPONSE_SCHEMA (synapse_distiller.prompt) has no `additionalProperties:
     false` on its object nodes -- nothing needed it before, because every
     other provider in this package is either prompt-instructed (NPU) or
     silently ignores response_format (AI-100). Anthropic's structured-output
     enforcement (`output_config.format`) 400s on an object schema missing it.
     `_tighten_schema` recursively inserts it.
  2. Default model is `claude-opus-5`.
  3. No `temperature` is sent at all -- 400 on Opus 5 / 4.8 / 4.7 / Fable 5.
     `max_tokens` is generous (16000): on Opus 5, thinking is on by default
     and `max_tokens` is a hard cap on thinking PLUS the final text
     together, so a tight budget (this repo's NPU-tuned
     `provider.max_tokens = 900`) would truncate the JSON mid-object.
     `output_config={"effort": "low"}` keeps reasoning shallow -- faithful
     compression is not a hard reasoning task, so there is no reason to pay
     for deep thinking on every segment.
  4. `stop_reason == "refusal"`: Anthropic returns HTTP 200 with empty or
     partial content when a safety classifier declines the request, rather
     than raising. Developer transcripts routinely contain security tooling,
     credentials, and exploit-shaped text, so this WILL happen in practice.
     Handled by returning schema_valid=False honestly -- never by raising --
     so an unhandled refusal cannot crash the worker loop.
  5. ModelUsage.input_tokens reports
     `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`,
     not the SDK's bare `usage.input_tokens`. See the loud comment at the
     call site -- this one is subtle and load-bearing.
  6. `output_config.effort` is sent ONLY to models that accept it. It errors
     on Haiku 4.5 and Sonnet 4.5, and claude-haiku-4-5 is exactly the arm the
     4096 pin below exists for -- sent unconditionally it 400s that arm on its
     first request. `output_config.format` is NOT gated with it: structured
     output is supported on Haiku 4.5, so the gate is on the key rather than
     on the container. See `supports_effort`.

capabilities.native_structured_output=True is purely descriptive here: the
ONLY runtime reader of ProviderCapabilities is
`OpenAICompatibleProvider._build_body`'s
`if response_schema is not None and self.capabilities.native_structured_output`
gate, and this class never goes through that code path -- it builds its own
request body (see break #2/#3 above). So the flag documents an invariant
("this provider's structured output must actually be schema-valid") rather
than switching any behaviour; a schema violation surfacing at runtime is a
bug in this file, not a silently no-op'd capability flag.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from synapse_contracts import ModelResult, ModelUsage

from synapse_providers.base import ModelProvider, ProviderCapabilities

DEFAULT_MODEL = "claude-opus-5"

# Correction #3. On Opus 5, thinking is on by default and max_tokens caps
# thinking + text together (not text alone, as on every OpenAI-shaped
# provider this package otherwise talks to) -- see the module docstring.
DEFAULT_MAX_TOKENS = 16000

# Haiku's cap is OURS, not the endpoint's. claude-haiku-4-5 serves a 200K
# context and will happily return up to 64K output tokens; 4096 is a spend and
# shape decision, and saying so here is the point -- a number that looks like a
# platform limit stops being questioned, and this one should be.
#
# Why 4096 specifically: Haiku is the arm someone reaches for to run the loop
# cheaply and often, and a distil response is a small findings object. 16000 is
# sized for Opus 5, where max_tokens caps thinking PLUS text and thinking is on
# by default; Haiku has no such thinking overhead to cover, so the same ceiling
# is ~4x more room than the task can use and 4x the exposure when a small model
# degenerates into a repetition loop -- the failure this repo already
# distinguishes by name (distiller.classify_drop). A cap is the only thing that
# bounds a loop's cost, so it should be near what the task needs.
#
# Matched on a SUBSTRING rather than an exact id: the arm is selected by
# SYNAPSE_ANTHROPIC_MODEL, a free-text env var, and both `claude-haiku-4-5` and
# `claude-haiku-4-5-20251001` are in use in this repo (scripts/serve_local.py).
# An exact-match table would silently fall back to 16000 for whichever spelling
# it did not list -- failing OPEN on the number that costs money.
HAIKU_MAX_TOKENS = 4096
_MODEL_MAX_TOKENS: tuple[tuple[str, int], ...] = (("haiku", HAIKU_MAX_TOKENS),)

# The operator override. Named to match INFERENCE_CLOUD_MAX_TOKENS on
# AIC100Provider, which exists for exactly this reason and records it: the
# service and the worker both construct their provider with NO arguments
# (`worker/cli.py`, `orchestrator/cli.py`, `service/cli.py`), so a constructor
# default is reachable only from Python and cannot be changed on a running
# deployment. Config that cannot reach the code is not config.
MAX_TOKENS_ENV = "SYNAPSE_ANTHROPIC_MAX_TOKENS"


def default_max_tokens_for(model: str) -> int:
    """The per-model output cap, before any explicit argument or env override.

    Falls back to DEFAULT_MAX_TOKENS for anything unlisted, so adding a model
    is never a prerequisite for using one.
    """
    lowered = model.lower()
    for fragment, cap in _MODEL_MAX_TOKENS:
        if fragment in lowered:
            return cap
    return DEFAULT_MAX_TOKENS


# Correction #3. A distiller doing faithful compression is not a hard
# reasoning task; "low" keeps the (always-on, by default) thinking shallow
# rather than paying for depth nothing here needs.
DEFAULT_EFFORT = "low"

# Correction #6. Which models accept `output_config.effort` AT ALL. Not a
# tuning question -- a 400. `effort` is accepted on Opus 4.5 and later, Sonnet
# 5, Fable 5 and Mythos 5, and ERRORS on Sonnet 4.5 and Haiku 4.5.
#
# That matters here more than anywhere else in this file: the distiller arm the
# 4096 pin above was written for IS claude-haiku-4-5. Sent unconditionally, the
# field meant that arm 400s on its first request and the pin governed an arm
# that had never run -- see docs/overnight/w6-live-stress.md section 5, which
# is where this was found.
#
# Substring match, same shape as _MODEL_MAX_TOKENS and for the same reason: the
# model arrives as free text through SYNAPSE_ANTHROPIC_MODEL, and this repo
# already carries two Haiku spellings.
#
# But it falls back the OTHER way, and the asymmetry is deliberate. The
# max-tokens table falls back to the generous default, so an unlisted model
# keeps working. This table falls back to OMITTING the field, because omitting
# `effort` is never an error while sending it to a model that rejects it always
# is. An unlisted model loses a tuning knob -- cheap, and visible in the request
# body. The other direction loses the arm.
_EFFORT_MODELS: tuple[str, ...] = (
    "opus-4-5", "opus-4-6", "opus-4-7", "opus-4-8", "opus-5",
    "sonnet-5", "fable", "mythos",
)


def supports_effort(model: str) -> bool:
    """Whether `output_config.effort` can be sent to this model without a 400.

    See _EFFORT_MODELS for why this fails closed while the max-tokens table
    fails open. Note the fragments are version-specific on purpose: a bare
    `"opus"` would match `claude-opus-4-1`, which predates the parameter, and a
    bare `"sonnet"` would match `claude-sonnet-4-5`, which rejects it outright.
    """
    lowered = model.lower()
    return any(fragment in lowered for fragment in _EFFORT_MODELS)


def _tighten_schema(node: Any) -> Any:
    """Correction #1: recursively insert `additionalProperties: false` into
    every object node of a JSON Schema.

    Anthropic's structured-output enforcement (`output_config.format`) 400s
    on an object schema node that lacks it; `synapse_distiller.prompt.
    RESPONSE_SCHEMA` doesn't set it, because nothing before this provider
    needed it. Generic over shape rather than hand-walking `properties` /
    `items` / `$defs` as separate cases: every dict value and every list
    item is recursed into uniformly, so arbitrary nesting (including
    `anyOf`/`oneOf`/`allOf` members and `$defs`) is covered by the same two
    lines. Returns a new structure and never mutates the input --
    RESPONSE_SCHEMA is a module-level constant shared by every ModelProvider,
    including ones that must never see this rewritten.
    """
    if isinstance(node, dict):
        tightened = {key: _tighten_schema(value) for key, value in node.items()}
        if tightened.get("type") == "object":
            # setdefault, not assignment: an author who already declared
            # additionalProperties (even `true`, deliberately) is respected.
            tightened.setdefault("additionalProperties", False)
        return tightened
    if isinstance(node, list):
        return [_tighten_schema(item) for item in node]
    return node


def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Structural break #3: `build_messages()` emits `{"role": "system"}` as
    `messages[0]` -- the OpenAI chat-completions shape every other provider in
    this package speaks -- but the Messages API takes the system prompt as a
    top-level `system` string, not a message in the array. Hoist it out.
    Multiple system messages (not produced by build_messages today, but not
    forbidden by the ModelProvider contract either) are joined so nothing is
    silently dropped.
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system_parts.append(str(message.get("content", "")))
        else:
            rest.append(message)
    return "\n\n".join(system_parts), rest


def _first_text(response: Any) -> str:
    """First text content block. "" for a response with none -- notably, a
    pre-output refusal's `content` array is empty."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


class AnthropicProvider(ModelProvider):
    """Claude Opus 5 (or another Anthropic model) via the Messages API.

    See the module docstring for why this is not an OpenAICompatibleProvider
    subclass and for the five corrections applied relative to the
    OpenAI-shaped assumptions the rest of this package makes.
    """

    provider_id = "anthropic"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        effort: str = DEFAULT_EFFORT,
        timeout: float | None = None,
        client: Any | None = None,
    ) -> None:
        # `client` is an injection point for tests (and for anyone who wants
        # a pre-configured AsyncAnthropic, e.g. pointed at a proxy) -- it
        # sidesteps constructing a real SDK client entirely, matching how
        # aic100.py accepts `transport=` for the same reason.
        if client is not None:
            self._client = client
        else:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as e:  # pragma: no cover -- dependency declared in pyproject.toml
                raise ImportError(
                    "anthropic SDK not installed -- it is a declared dependency of "
                    "synapse-providers; run `uv sync`"
                ) from e
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if timeout is not None:
                kwargs["timeout"] = timeout
            self._client = AsyncAnthropic(**kwargs)

        self._model = model or os.environ.get("SYNAPSE_ANTHROPIC_MODEL", DEFAULT_MODEL)
        # Resolution order, and the reason for it:
        #   env  >  explicit argument  >  per-model pin  >  DEFAULT_MAX_TOKENS
        #
        # `max_tokens=None` (the default) means "decide from the model" rather
        # than "use 16000" -- which is what lets a Haiku arm get 4096 through
        # the three call sites that pass no arguments at all.
        #
        # Env beating an explicit argument is deliberate and matches
        # AIC100Provider's `int(os.environ.get(...) or max_tokens)`. The whole
        # purpose of the variable is to change the number on a deployment
        # whose code you are not editing; a hard-coded argument winning over it
        # would leave exactly the call sites that most need overriding
        # unreachable. It is also the same precedence SYNAPSE_ANTHROPIC_MODEL
        # already has one line above.
        resolved = max_tokens if max_tokens is not None else default_max_tokens_for(
            self._model
        )
        override = os.environ.get(MAX_TOKENS_ENV)
        # PUBLIC (2026-08-06): `SynthesisBudget.for_provider` reads `max_tokens`
        # off whichever provider is wired into synthesis, so a provider that
        # hides its cap behind a private name is silently budgeted at
        # DEFAULT_OUTPUT_TOKENS=800 -- a 16000-token cap spent as 800, with
        # the 500-word memory this provider can easily afford asked for as
        # 270. Named like AIC100Provider's and OpenAICompatibleProvider's for
        # exactly that reason: the budget reads one attribute across all of
        # them or it reads none of them reliably. It is also why the Haiku pin
        # belongs HERE rather than in config/synapse.toml: synthesis reads this
        # attribute and never reads a capability record, so a config-only cap
        # would be inert on the arm it was written for (decisions/006).
        self.max_tokens = int(override) if override else resolved
        self._effort = effort

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Purely descriptive here -- see the module docstring's closing note.
        return ProviderCapabilities(native_structured_output=True, streaming=False)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResult:
        # Structural break #3 / correction #1: hoist the system message(s)
        # build_messages() put at messages[0] into the top-level `system`
        # field; everything else goes into `messages` unchanged.
        system, rest = _split_system(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "messages": rest,
            # Correction #3: no `temperature` key at all -- deliberately
            # absent, not set to a default. Sending it (even 0.0) is a 400
            # on Opus 5 / 4.8 / 4.7 / Fable 5. Do not add it back.
        }
        if system:
            kwargs["system"] = system

        # Correction #6: `effort` only for models that accept it (see
        # supports_effort) -- but `format` regardless, because structured
        # output IS supported on Haiku 4.5. The two share a container, so the
        # gate has to be on the KEY, not on `output_config` as a whole:
        # dropping the container for Haiku would take schema enforcement with
        # it and hand the distiller un-enforced JSON on the one arm this
        # provider pins a cap for.
        output_config: dict[str, Any] = {}
        if supports_effort(self._model):
            output_config["effort"] = self._effort
        if response_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                # Correction #1: tightened, or the request 400s.
                "schema": _tighten_schema(response_schema),
            }
        # Omitted entirely when empty rather than sent as `{}` -- an empty
        # object is not what "no output configuration" looks like on the wire.
        if output_config:
            kwargs["output_config"] = output_config

        started = time.perf_counter()
        response = await self._client.messages.create(**kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)

        return self._to_result(response, response_schema, latency_ms)

    def _to_result(
        self,
        response: Any,
        response_schema: dict[str, Any] | None,
        latency_ms: int,
    ) -> ModelResult:
        text = _first_text(response)

        # Correction #4: a safety-classifier decline is a normal HTTP 200
        # with stop_reason == "refusal" -- not an exception, and `content` is
        # empty (declined before any output) or partial (declined mid-
        # stream). Developer transcripts contain security tooling and
        # credentials, so this WILL happen. Report schema_valid=False
        # honestly -- whether or not a schema was requested, "the model
        # refused" is never usable data -- and never raise: an uncaught
        # refusal must not crash the worker loop.
        if getattr(response, "stop_reason", None) == "refusal":
            data: Any = text
            schema_valid = False
        elif response_schema is not None:
            try:
                data = json.loads(text)
                schema_valid = True
            except json.JSONDecodeError:
                # output_config.format shouldn't produce invalid JSON, but if
                # it ever does, hand the raw text back so a tolerant-parsing
                # caller (e.g. Distiller's retry) has something to work with.
                data = text
                schema_valid = False
        else:
            data = text
            schema_valid = True

        usage = response.usage
        # Correction #5 -- LOUD, load-bearing, do not "simplify" this away.
        #
        # Anthropic's `usage.input_tokens` is the UNCACHED remainder only.
        # Tokens served from prompt caching are reported separately:
        #   cache_creation_input_tokens -- tokens newly written to cache
        #   cache_read_input_tokens     -- tokens served FROM cache
        # The TRUE prompt size -- "did the prompt actually reach the model"
        # -- is the SUM of all three, not `usage.input_tokens` alone.
        #
        # synapse_distiller.guards.assert_prompt_conditioned and check_canary
        # both gate on ModelUsage.input_tokens meaning exactly that: "a
        # prompt of length 1 is the signature of the dropped-prompt path."
        # If anyone later enables prompt caching on this package's 809-token
        # fixed prefix (the v4-condense prompt pack's calibrated overhead --
        # see synapse_distiller.capability / config/synapse.toml), a cache
        # hit would report a near-zero `usage.input_tokens` on its own and
        # falsely trip the dropped-prompt guard on a call that was
        # perfectly healthy. Summing all three is what keeps that guard
        # honest regardless of whether caching is ever turned on.
        true_input_tokens = (
            int(getattr(usage, "input_tokens", 0) or 0)
            + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        )

        return ModelResult(
            data=data,
            usage=ModelUsage(
                input_tokens=true_input_tokens,
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )
