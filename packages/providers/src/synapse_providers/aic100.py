"""Hosted Cirrascale Cloud AI 100 — the synthesis target (amendment A + G).

Standalone ModelProvider on purpose, not an OpenAICompatibleProvider subclass:
both probed gotchas make its schema path structurally different —
  * response_format is silently IGNORED (a 200 proves nothing; control-probed)
  * /chat/completions EATS emitted JSON into empty tool_calls
so schema calls flatten the messages into one prompt and use POST /completions,
then tolerantly extract and structurally validate a JSON object. One retry
that changes the prompt and temperature so a deterministic decode isn't just
resent, then honest schema_valid=False. max_tokens is bounded on every call
— shared credit pool.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from synapse_contracts import ModelResult, ModelUsage
from synapse_providers.base import ModelProvider, ProviderCapabilities

logger = logging.getLogger(__name__)

# The Indonesian instance, not aisuite.cirrascale.com. Probed 2026-08-05: the
# team key now gets `500 Invalid API key, no user found` from the .com host and
# a clean 200 from this one. The .com value shipped as the default since
# 2026-08-03, so anything that did not set INFERENCE_CLOUD_BASE_URL has been
# pointed at a host we cannot authenticate against.
DEFAULT_BASE_URL = "https://aisuite-indonesia.cirrascale.com/apis/v2"


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Find the FIRST balanced, string-aware JSON object in free-form text.

    A Llama-3.1-8B answering a /completions prompt that ends in "JSON:"
    routinely narrates around the object, shows a code snippet with its own
    braces, or appends a trailing example -- any of which puts a second,
    unrelated brace pair in the response. Reaching for the OUTERMOST
    `{`..`}` span (as openai_compat._parse_json_tolerantly's fallback does)
    is poisoned by that second pair; a naive brace-counter is poisoned by an
    unbalanced `}` inside a *string* value (routine free prose about code).
    `json.JSONDecoder.raw_decode` is used instead: it is string-aware (a `}`
    inside a quoted value never closes the object early) and, tried at every
    `{` in the text, stops at the FIRST index that parses -- matching what a
    balanced-brace scanner intends without either failure mode.
    """
    stripped = text.strip()
    if not stripped:
        return None

    # The common, cheap case: the whole response IS the JSON object.
    try:
        whole = json.loads(stripped)
        if isinstance(whole, dict):
            return whole
    except json.JSONDecodeError:
        pass

    # A fenced ```json block, with or without the language tag.
    if "```" in stripped:
        for chunk in stripped.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                try:
                    fenced = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(fenced, dict):
                    return fenced

    # Scan for the first index where a balanced object decodes, string
    # escaping respected by the stdlib tokenizer rather than hand-rolled.
    decoder = json.JSONDecoder()
    idx = stripped.find("{")
    while idx != -1:
        try:
            candidate, _end = decoder.raw_decode(stripped, idx)
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass
        idx = stripped.find("{", idx + 1)
    return None


def _satisfies_schema(data: Any, schema: dict[str, Any]) -> bool:
    """Structural check, not full JSON Schema: confirms the parsed object
    plausibly matches response_schema rather than merely being *some*
    parseable JSON. 'It parsed' proves nothing was satisfied -- the same
    false-confidence trap this module exists to catch one level up (a 200
    proves nothing was enforced).

    Deliberately requires only ONE of the schema's declared `required` keys
    to be present, not all of them. synapse_service.synthesis validates the
    verdict via its _SynthesisVerdicts model with every field defaulted, so an
    8B dropping one key (e.g. omitting "conflicts" on a round with nothing
    to report) doesn't cost the rest of an otherwise-usable verdict; an
    all-required gate here would silently discard that whole round before
    synthesis's own tolerance ever got a chance to run. A response matching
    NONE of the declared keys is the real false-confidence case this check
    exists to catch -- some parseable JSON that has nothing to do with the
    schema it was asked for."""
    if schema.get("type") == "object" and not isinstance(data, dict):
        return False
    if isinstance(data, dict):
        required = schema.get("required", [])
        if required and not any(key in data for key in required):
            return False
    return True


def _example_from_schema(schema: dict[str, Any]) -> Any:
    """A minimal concrete instance of `schema` — what the prompt shows the model.

    An 8B shown a JSON *schema* copies the schema (probed live 2026-08-05: the
    synthesis call returned the schema literal with values mashed in, failing
    verdict validation every round). Shown an *example instance*, it fills one
    in. So the prompt never contains the schema itself.
    """
    stype = schema.get("type")
    if stype == "object":
        return {k: _example_from_schema(v) for k, v in schema.get("properties", {}).items()}
    if stype == "array":
        return [_example_from_schema(schema.get("items", {"type": "string"}))]
    if stype == "boolean":
        return True
    if stype in ("integer", "number"):
        return 0
    return "..."


class AIC100Provider(ModelProvider):
    provider_id = "aic100"

    # Llama-3.3-70B, not the 8B. Bake-off 2026-08-05 on the four models this key
    # can reach: the 8B does not merge semantically, it concatenates with "and" --
    # it stapled two unrelated cache properties into one fabricated claim, and on
    # the near-duplicate case it kept one side and dropped the other, which is the
    # discard-one failure adr/0002 exists to forbid. The 70B distinguished "same
    # fact" from "two facts about one component" correctly, and costs ~7x FEWER
    # output tokens doing it (56-87 vs the 8B hitting its 600 cap every call).
    # Its rate limits are tighter -- 429s with a ~36s cooldown under load, which
    # this provider does not yet handle.
    def __init__(self, base_url: str | None = None, model: str = "Llama-3.3-70B",
                 api_key: str | None = None, max_tokens: int = 800,
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (base_url or os.environ.get("INFERENCE_CLOUD_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        # INFERENCE_CLOUD_MODEL exists so a rehearsal can A/B synthesis models
        # (8B on .com vs 70B on the Indonesian instance) without a code change.
        self.model = os.environ.get("INFERENCE_CLOUD_MODEL", model)
        # A comma-separated INFERENCE_CLOUD_API_KEYS pool rotates on 429 —
        # the hosted instances rate-limit per key (~20 req/hour observed), and
        # one live rehearsal alone is ~11 calls. Singular key stays supported.
        pool = os.environ.get("INFERENCE_CLOUD_API_KEYS", "")
        self.api_keys = ([k.strip() for k in pool.split(",") if k.strip()]
                         or [api_key or os.environ.get("INFERENCE_CLOUD_API_KEY", "")])
        self._key_index = 0
        self.api_key = self.api_keys[0]  # kept for back-compat introspection
        # INFERENCE_CLOUD_MAX_TOKENS / _TIMEOUT exist for the same reason
        # INFERENCE_CLOUD_MODEL does: these are the numbers an operator must be
        # able to change on a RUNNING deployment, and until 2026-08-06 both were
        # constructor defaults reachable only from Python. `synapse_service.cli.
        # _provider()` builds this with no arguments, so the service had no way
        # to set either one.
        #
        # max_tokens is the number that decides whether a synthesis verdict
        # FITS. It is not an endpoint limit -- probed live 2026-08-06, this host
        # accepts max_tokens=3000 -- it is a bound on the shared credit pool.
        # Raise it deliberately, against the measured budget, not by taste.
        #
        # timeout travels with it and must be raised TOGETHER: a bigger cap
        # means a longer generation, and a ReadTimeout is caught by
        # synthesis.py's `except Exception` and reported as "findings are
        # landed, memory unchanged" -- the identical symptom truncation
        # produces, from a different cause. Two syntheses measured at 48.5s and
        # 51.7s against the 60.0s default on 2026-08-06 were already one slow
        # round from that cliff.
        self.max_tokens = int(os.environ.get("INFERENCE_CLOUD_MAX_TOKENS") or max_tokens)
        self.timeout = float(os.environ.get("INFERENCE_CLOUD_TIMEOUT") or timeout)
        self._transport = transport

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_structured_output=False, streaming=False)

    async def _post_rotating(self, client: httpx.AsyncClient, url: str,
                             payload: dict[str, Any]) -> httpx.Response:
        """POST, rotating through the key pool on 429. Each key tried at most
        once per request; a non-429 answer (success or failure) returns
        immediately with whichever key gave it."""
        last: httpx.Response | None = None
        for _ in range(len(self.api_keys)):
            key = self.api_keys[self._key_index]
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {key}"})
            if resp.status_code != 429:
                return resp
            last = resp
            self._key_index = (self._key_index + 1) % len(self.api_keys)
        return last  # every key rate-limited; caller's raise_for_status reports it

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_key}"})

    async def complete(self, messages: list[dict[str, Any]],
                       response_schema: dict[str, Any] | None = None) -> ModelResult:
        started = time.perf_counter()
        async with self._client() as client:
            if response_schema is None:
                resp = await self._post_rotating(client, f"{self.base_url}/chat/completions", {
                    "model": self.model, "messages": messages,
                    "max_tokens": self.max_tokens, "temperature": 0.0})
                resp.raise_for_status()
                payload = resp.json()
                return self._result(payload["choices"][0]["message"]["content"],
                                    payload, started, schema_valid=True)

            base_prompt = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
            base_prompt += ("\n\nReturn ONLY a JSON object shaped exactly like this example, "
                            "with your real values in place of the example values:\n"
                            + json.dumps(_example_from_schema(response_schema)) + "\nJSON:")
            prompt = base_prompt
            payload: dict[str, Any] = {}
            for attempt in range(2):                        # one retry, exactly
                # temperature 0.0 is a deterministic decode: an unconditional
                # identical resend of attempt 0's request would be a
                # guaranteed-identical second failure, doubling consumption
                # of the shared credit pool for nothing. The retry (attempt
                # 1) echoes the bad response back and asks for a repair, and
                # nudges temperature off zero so it isn't a no-op resend.
                temperature = 0.0 if attempt == 0 else 0.2
                resp = await self._post_rotating(client, f"{self.base_url}/completions", {
                    "model": self.model, "prompt": prompt,
                    "max_tokens": self.max_tokens, "temperature": temperature})
                resp.raise_for_status()
                payload = resp.json()
                choice = payload["choices"][0]
                text = choice.get("text", "")
                parsed = extract_first_json_object(text)
                if parsed is not None and _satisfies_schema(parsed, response_schema):
                    return self._result(parsed, payload, started, schema_valid=True)
                if self._was_truncated(choice, payload):
                    # A response cut off by max_tokens takes the IDENTICAL
                    # path as unparseable garbage from here on (retry, then
                    # honest schema_valid=False) -- nothing in the
                    # ModelResult distinguishes "the model was wrong" from
                    # "we cut it off mid-answer". Log it distinctly so an
                    # operator watching logs (not just HTTP 200s) can tell
                    # the two apart and knows to raise max_tokens rather
                    # than blame the model or the prompt.
                    logger.warning("AIC100 response truncated at max_tokens=%d "
                                   "(completion_tokens=%s, finish_reason=%s) on attempt "
                                   "%d; a truncated response is indistinguishable from "
                                   "garbage downstream without this log line",
                                   self.max_tokens,
                                   payload.get("usage", {}).get("completion_tokens"),
                                   choice.get("finish_reason"), attempt)
                    # SHRINK THE ASK, do not echo the overflow. The generic
                    # repair below appends the bad response to the prompt --
                    # under a length failure that lengthens the input and gives
                    # the model no reason to emit less, so attempt 1 fails the
                    # same way attempt 0 did. Asking for "much shorter" is the
                    # only retry that can actually fit. Deliberately generic:
                    # this provider knows nothing about working memory or word
                    # counts. The CALLER owns its own content budget (see
                    # synapse_service.synthesis, which derives a word cap from
                    # this same max_tokens and states it in the prompt).
                    prompt = (base_prompt
                             + f"\n\nYour previous response exceeded the "
                               f"{self.max_tokens}-token limit and was cut off "
                               "mid-answer. Return the SAME JSON object, with every "
                               "required key present, but MUCH SHORTER -- at most half "
                               "the length. Prefer completeness of structure over "
                               "detail in any one field.\nJSON:")
                    continue
                prompt = (base_prompt
                         + "\n\nYour previous response did not match the schema:\n"
                         + text.strip()[:500]
                         + "\n\nReturn ONLY the corrected JSON object, nothing else.\nJSON:")
            return self._result(None, payload, started, schema_valid=False)

    def _was_truncated(self, choice: dict[str, Any], payload: dict[str, Any]) -> bool:
        """Whether the response was cut off at the token cap.

        `finish_reason == "length"` is the documented signal and is checked
        first, but it CANNOT BE RELIED ON HERE. Probed live 2026-08-06 against
        aisuite-indonesia: a /completions call with max_tokens=3000 returned
        completion_tokens=3000 and `finish_reason: "stop"`. The response was
        cut off at exactly the cap and the endpoint said it stopped naturally.

        That single wrong field is why synthesis failed silently for hours:
        every truncation took the unparseable-garbage path with no warning, so
        the logs showed nothing but HTTP 200s and a memory_version that would
        not move. The unit test covering the warning fabricated a
        finish_reason="length" fixture the host never sends, so it passed
        throughout.

        completion_tokens >= max_tokens is endpoint-independent: the model
        cannot emit more than the cap, so reaching it exactly means either a
        cut-off response or one that ended precisely on the boundary. Those are
        indistinguishable from here, and treating the second as complete is the
        unsafe direction -- a whole synthesis round is lost either way.
        """
        if choice.get("finish_reason") == "length":
            return True
        emitted = payload.get("usage", {}).get("completion_tokens")
        return isinstance(emitted, int) and emitted >= self.max_tokens

    def _result(self, data: Any, payload: dict[str, Any], started: float,
                *, schema_valid: bool) -> ModelResult:
        usage = payload.get("usage", {})
        return ModelResult(
            data=data,
            usage=ModelUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                             output_tokens=int(usage.get("completion_tokens", 0))),
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_id=self.provider_id,
            schema_valid=schema_valid,
        )
