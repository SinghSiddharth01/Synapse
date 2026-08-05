"""The arrival briefing — composed from the watermark, carried by `instructions`.

Hard-capped and headline-only by design: counts and types, never finding
bodies (context economy — bodies grow with session length, headlines do not).
FAIL OPEN: any error yields the default unbound text. A briefing that can
break an agent's session start is worse than no briefing.

#### Post-review amendment (2026-08-04)

Round 2 found this module's promises weren't actually enforced:

1. The narrow `except (httpx.HTTPError, OSError, ValueError, TypeError,
   AttributeError, KeyError)` did not cover every exception class the
   string-composition code could raise, so an unforeseen one would escape
   this function and — since `build_briefing` runs in `cli.main` before
   `uvicorn.serve` starts — take the whole orchestrator process down with
   it. The guard is now a blanket `except Exception`, covering the HTTP
   round trip, the JSON parse, AND the string composition as one unit.
2. There was no hard cap. `by_type` is watermark-response content (E3, not
   this package) interpolated directly into `instructions` — the highest-
   trust text surface a connecting agent sees (Task 1's sentinel probe).
   An oversized or adversarial `by_type` map rode straight through. The
   composed string is now truncated to `_MAX_BRIEFING_CHARS` with an
   ellipsis.
3. Service-supplied values (by_type keys/values, in particular) were
   interpolated raw. A key containing embedded newlines could read like a
   new instruction block appended after the real briefing text. `_clean`
   now collapses control characters/newlines out of every service-supplied
   value before it is interpolated.
"""

from __future__ import annotations

import logging
import re

import httpx

from synapse_contracts import LocalBinding

from synapse_orchestrator.server import _DEFAULT_INSTRUCTIONS, SENTINEL

logger = logging.getLogger(__name__)

# Control characters (including \r, \n, \t) collapsed to a single space
# before a service-supplied value is interpolated into agent-facing text —
# a newline sequence must not be able to read like a new instruction block
# appended after the real briefing.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

# Hard cap on the composed briefing string, ellipsis-truncated past this.
# Headlines only, by design (module docstring) — this is the enforcement.
_MAX_BRIEFING_CHARS = 1200


def _clean(value: object) -> str:
    """Collapse control characters/newlines out of a service-supplied value
    before it is interpolated into `instructions`."""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()


async def build_briefing(binding: LocalBinding | None, service_url: str, *,
                         timeout: float = 2.0,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    if binding is None:
        return _DEFAULT_INSTRUCTIONS
    url = (f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/watermark")
    # FAIL OPEN, unconditionally: the HTTP round trip, the JSON parse, AND
    # the string composition below are ALL inside this one guard. E3 is not
    # merged as of this writing, so the watermark response's shape is an
    # unverified assumption — a 200 whose JSON doesn't match it (a list
    # instead of a dict, "by_type" holding something un-summable, a key
    # missing, or any other surprise) must fail open exactly like a downed
    # service, never raise out of here and take the whole orchestrator
    # process down with it (this runs in cli.main before uvicorn.serve
    # starts).
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            resp = await client.get(url, params={"agent_session": binding.agent_session_id})
            resp.raise_for_status()
            w = resp.json()

        if not isinstance(w, dict):
            raise ValueError(f"watermark response was not an object: {w!r}")
        by_type = w.get("by_type", {})
        if not isinstance(by_type, dict):
            raise ValueError(f"'by_type' was not an object: {by_type!r}")

        total = sum(int(v) for v in by_type.values())
        types = ", ".join(f"{_clean(k)}: {int(v)}" for k, v in sorted(by_type.items()))
        version = int(w.get("version", 0))
        new_since = int(w.get("new_since", 0))
        conflicts = int(w.get("conflicts", 0))

        # `topics` MISSING is a pre-E5 service: render the rest. `topics`
        # MALFORMED is a shape nobody should trust, and this is the highest-
        # trust text surface a connecting agent sees -- fail open.
        topics_clause = ""
        raw_topics = w.get("topics")
        if raw_topics is not None:
            if not isinstance(raw_topics, list):
                raise ValueError(f"'topics' was not a list: {raw_topics!r}")
            labels = []
            for topic in raw_topics:
                if not isinstance(topic, dict):
                    raise ValueError(f"'topics' held a non-object: {topic!r}")
                label = topic.get("label")
                if not isinstance(label, str):
                    raise ValueError(f"topic label was not a string: {label!r}")
                labels.append(_clean(label))
            if labels:
                topics_clause = (" The team is working on: "
                                 + ", ".join(f"“{label}”" for label in labels) + ".")

        text = (
            f"{SENTINEL} You are in Synapse Shared Session {_clean(binding.shared_id)} as "
            f"{_clean(binding.contributor)}. Team memory holds {total} findings ({types}), "
            f"{conflicts} conflict(s), at version v{version} — "
            f"{new_since} new since you last looked. Call the `query` tool "
            "before exploring an unfamiliar subsystem, when debugging something a "
            "teammate may also be working on, or before concluding something is a "
            "dead end. Call `contribute` when you learn something non-obvious a "
            "teammate would benefit from."
            f"{topics_clause}"
        )
    except Exception as exc:  # FAIL OPEN: nothing escapes this function, ever
        logger.info("Briefing fail-open (%s)", exc.__class__.__name__)
        return _DEFAULT_INSTRUCTIONS

    if len(text) > _MAX_BRIEFING_CHARS:
        text = text[: _MAX_BRIEFING_CHARS - 1].rstrip() + "…"
    return text
