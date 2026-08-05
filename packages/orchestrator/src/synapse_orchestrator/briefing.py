"""The arrival briefing — composed from the watermark, carried by `instructions`.

Hard-capped and headline-only by design: counts and types, never finding
bodies (context economy — bodies grow with session length, headlines do not).
FAIL OPEN: any error yields the default unbound text. A briefing that can
break an agent's session start is worse than no briefing."""

from __future__ import annotations

import logging

import httpx

from synapse_contracts import LocalBinding

from synapse_orchestrator.server import _DEFAULT_INSTRUCTIONS, SENTINEL

logger = logging.getLogger(__name__)


async def build_briefing(binding: LocalBinding | None, service_url: str, *,
                         timeout: float = 2.0,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    if binding is None:
        return _DEFAULT_INSTRUCTIONS
    url = (f"{service_url.rstrip('/')}/v1/sessions/{binding.shared_id}/watermark")
    # The HTTP round trip AND the parsing of its body both live inside this
    # guard. E3 is not merged as of this writing, so the watermark response's
    # shape is an unverified assumption — a 200 whose JSON doesn't match it
    # (a list instead of a dict, "by_type" holding something un-summable, a
    # key missing) must fail open exactly like a downed service, not raise
    # out of here and take the whole orchestrator process down with it (this
    # runs in cli.main before uvicorn.serve starts).
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            resp = await client.get(url, params={"agent_session": binding.agent_session_id})
            resp.raise_for_status()
            w = resp.json()

        by_type = w.get("by_type", {})
        total = sum(by_type.values())
        types = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
        text = (
            f"{SENTINEL} You are in Synapse Shared Session {binding.shared_id} as "
            f"{binding.contributor}. Team memory holds {total} findings ({types}), "
            f"{w.get('conflicts', 0)} conflict(s), at version v{w.get('version', 0)} — "
            f"{w.get('new_since', 0)} new since you last looked. Call the `query` tool "
            "before exploring an unfamiliar subsystem, when debugging something a "
            "teammate may also be working on, or before concluding something is a "
            "dead end. Call `contribute` when you learn something non-obvious a "
            "teammate would benefit from."
        )
    except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        logger.info("Briefing fail-open (%s)", exc.__class__.__name__)
        return _DEFAULT_INSTRUCTIONS
    return text
