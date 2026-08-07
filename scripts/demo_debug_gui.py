"""Run the service debug GUI with preloaded, realistic state -- no model, no
network, no config.

    .venv/bin/python scripts/demo_debug_gui.py            # http://127.0.0.1:8991/
    .venv/bin/python scripts/demo_debug_gui.py --port 9000
    .venv/bin/python scripts/demo_debug_gui.py --throttled  # synthesis key at 0

Boots `build_app` on a FakeProvider, seeds one shared session with the seg-005
golden pair (two contributors, one merge), registers members in every state the
participants table can show (active, listening, left, unregistered), and runs
one query. With `--throttled` the provider reports 0 requests remaining, so the
governor defers the merge and the brain page shows the LIMITED synthesis key --
the exact silent-failure scenario the rate-limit panel exists to make visible.

Pages: /  (home) · /debug  (brain) · /debug/log  (log)
"""

from __future__ import annotations

import argparse
import threading
import time

import httpx
import uvicorn
from synapse_providers import FakeProvider
from synapse_providers.ratelimit import RateLimitSnapshot
from synapse_service.api import build_app

MERGE_SCRIPT = {
    "working_memory": (
        "Team is chasing a decode failure: a ~40 ms timing window between the "
        "two DMA writes, load-dependent. Reproduces only when background "
        "traffic pushes the inter-write gap past the threshold; the bench rig "
        "needs synthetic load to trigger it reliably."
    ),
    "merges": [{
        "source_ids": ["f-005a-01", "f-005b-01"],
        "text": "The decode failure is a ~40 ms timing window between the two "
                "DMA writes, and it only manifests under load.",
        "type": "learning",
    }],
    "trivial_ids": [],
    "conflicts": [],
}


def _finding(fid: str, contributor: str, agent_session: str, agent: str,
             text: str) -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": contributor,
                              "agent_session": agent_session, "agent": agent}],
            "ts": "2026-08-06T22:40:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8991)
    parser.add_argument("--throttled", action="store_true",
                        help="report 0 requests remaining so the merge defers "
                             "and the brain page shows the LIMITED key")
    args = parser.parse_args()

    # A synthesis verdict with no merges, for the second session's
    # single-finding push. Extra scripts are harmless when unconsumed
    # (throttled mode defers pushes before the provider is reached).
    empty_verdict = {"working_memory": "Per-worker template caches drift.",
                     "merges": [], "trivial_ids": [], "conflicts": []}
    provider = FakeProvider(scripts=[MERGE_SCRIPT, {"ranked": [0]},
                                     empty_verdict, {"ranked": [0]},
                                     empty_verdict])
    if args.throttled:
        provider.last_rate_limit = RateLimitSnapshot(
            requests_remaining=0, tokens_remaining=1180, reset_seconds=41.0)

    app = build_app(provider)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.0)

    c = httpx.Client(base_url=f"http://127.0.0.1:{args.port}")
    sid = c.post("/v1/sessions", json={
        "purpose": "Chasing the FEC decode failure on the bench rig",
        "created_by": "sid"}).json()["shared_id"]

    c.post(f"/v1/sessions/{sid}/members",
           json={"contributor": "sid", "agent_session": "as-sid-window-01"})
    c.post(f"/v1/sessions/{sid}/members",
           json={"contributor": "aditya", "agent_session": "as-fixture-005a"})
    c.post(f"/v1/sessions/{sid}/members", json={"contributor": "meera"})
    c.request("DELETE", f"/v1/sessions/{sid}/members/meera")

    pair = [
        _finding("f-005a-01", "aditya", "as-fixture-005a", "claude-code",
                 "The decode failure is a timing window: it occurs only when "
                 "the gap between the two DMA writes exceeds roughly 40 ms."),
        _finding("f-005b-01", "akhil", "as-fixture-005b", "codex",
                 "The decode failure reproduces only under load, when "
                 "background traffic pushes the delay past about 40 ms."),
    ]
    push = c.post(f"/v1/sessions/{sid}/findings", json={"findings": pair}).json()
    c.post(f"/v1/sessions/{sid}/query",
           json={"query": "what do we know about the decode failure",
                 "contributor": "sid", "agent_session": "as-sid-window-01"})

    # A second session so the sidebar shows the real hierarchy: sessions are
    # the top-level entity, each with its own brain/log/memory subpages.
    sid2 = c.post("/v1/sessions", json={
        "purpose": "Moving the template cache to the shared store",
        "created_by": "meera"}).json()["shared_id"]
    c.post(f"/v1/sessions/{sid2}/members",
           json={"contributor": "meera", "agent_session": "as-meera-01"})
    c.post(f"/v1/sessions/{sid2}/findings", json={"findings": [
        _finding("f-cache-01", "meera", "as-meera-01", "claude-code",
                 "Per-worker template caches drift: worker 3 served a stale "
                 "page 40 minutes after the template changed."),
    ]})

    mode = "throttled (merge deferred)" if args.throttled else "normal"
    print(f"session {sid} seeded · synthesis {mode}"
          f" · synthesized={push.get('synthesized')}")
    print(f"  home   http://127.0.0.1:{args.port}/")
    print(f"  brain  http://127.0.0.1:{args.port}/debug")
    print(f"  log    http://127.0.0.1:{args.port}/debug/log")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
