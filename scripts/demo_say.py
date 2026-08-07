"""Push work into a RUNNING walkthrough, by hand, one thing at a time.

`demo_local.py` drives itself. This is the other half: once it is up and
sitting at "everything is still running", you use this to be the developer —
type a turn into someone's transcript and watch the two dashboards react, at
whatever pace you like.

    # take a look at what's available
    uv run python scripts/demo_say.py --list

    # send one of the corpus segments as Aditya, and watch 8790
    uv run python scripts/demo_say.py --as aditya --segment seg-003

    # ask shared memory something, as somebody who contributed nothing
    uv run python scripts/demo_say.py --ask "what do we know about timing?"

    # ask as one of the contributors, and watch suppression bite
    uv run python scripts/demo_say.py --ask "what about timing?" --as sid

    # your own words
    uv run python scripts/demo_say.py --as aditya --say "the retry backoff of
        250 ms is what was masking the DMA gap"

WHAT `--say` DOES OFFLINE, HONESTLY: your words reach the worker for real —
they are read, segmented and triaged like anything else — but the offline
stand-in only knows how to answer for this repo's fixtures, so it returns an
empty finding list for anything else rather than inventing one. You will see
triage keep it and the model return nothing. To have your own words actually
distilled, run the walkthrough with `--live`, where a real model answers.

Everything here talks to the same localhost ports `demo_local.py` started;
it starts nothing and stops nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_local import (  # noqa: E402
    DEMO,
    FEED,
    SERVICE_URL,
    append_segment,
    http,
    service_stats,
)

SEGMENTS = Path(__file__).resolve().parent.parent / "fixtures" / "segments"


def running_sessions() -> dict[str, str]:
    """contributor -> agent_session_id, read off the transcripts on disk."""
    found = {}
    for path in sorted((DEMO / "transcripts").glob("as-*.jsonl")):
        contributor = path.stem.split("-")[1]
        found[contributor] = path.stem
    return found


def require_running() -> tuple[dict[str, str], str]:
    sessions = running_sessions()
    if not sessions:
        raise SystemExit(
            "No walkthrough is running (nothing in .demo/transcripts).\n"
            "Start one first:  uv run python scripts/demo_local.py"
        )
    try:
        payload = http("GET", f"{SERVICE_URL}/debug/stats.json", timeout=5.0) or {}
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SystemExit(
            f"The service is not answering on {SERVICE_URL}. The walkthrough may "
            f"have been stopped; start it again with scripts/demo_local.py"
        ) from None
    listed = payload.get("sessions") or []
    if not listed:
        raise SystemExit("The service is up but holds no Shared Session yet.")
    return sessions, listed[0]["shared_id"]


def show_state(shared_id: str) -> None:
    stats = service_stats(shared_id)
    view = stats.get("view") or {}
    print(f"\n  shared memory now: {view.get('visible')} visible · "
          f"{view.get('superseded')} superseded · "
          f"{len(stats.get('log_tail') or [])} log entries")
    for entry in (stats.get("log_tail") or [])[-4:]:
        print(f"    {entry['kind']:<16} {(entry.get('summary') or '')[:84]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true",
                        help="show the available segments and who is running")
    # No default: sending work needs a contributor (defaulted below), but ASKING
    # must default to an outsider. Defaulting `--ask` to a real contributor made
    # both halves of the suppression contrast identical — the interesting case
    # is the one where you have to name yourself to make it disappear.
    parser.add_argument("--as", dest="who", default=None,
                        help=f"which contributor ({' or '.join(FEED)}); for --ask, "
                             f"omit to ask as somebody who contributed nothing")
    parser.add_argument("--segment", help="send a corpus segment, e.g. seg-003")
    parser.add_argument("--say", help="send your own words as one user turn")
    parser.add_argument("--ask", help="query shared memory instead of adding to it")
    args = parser.parse_args(argv)

    if args.list:
        print("\nsegments in the corpus:")
        triage = json.loads((SEGMENTS.parent / "triage.json").read_text())
        for path in sorted(SEGMENTS.glob("*.json")):
            sid = path.stem
            note = triage.get(sid, {})
            first = json.loads(path.read_text())["events"][0]["content"][:52]
            print(f"  {sid:<10} triage: {note.get('expected', '?'):<5} "
                  f"— {first}…")
        sessions = running_sessions()
        print("\nrunning right now:" if sessions else "\nnothing running.")
        for who, agent_session in sessions.items():
            port = FEED.get(who, {}).get("port")
            print(f"  {who:<8} {agent_session}   dashboard "
                  f"http://127.0.0.1:{port}/debug")
        return 0

    sessions, shared_id = require_running()

    if args.ask:
        # An agent_session nobody used means nothing is suppressed; naming a
        # real contributor shows suppression from that person's side.
        if args.who and args.who not in sessions:
            raise SystemExit(f"No worker is running for {args.who!r}. "
                             f"Running: {', '.join(sessions) or 'nobody'}")
        asker = sessions[args.who] if args.who else "as-observer"
        whose = f"as {args.who}" if args.who else "as an outsider who contributed nothing"
        try:
            answer = http("POST", f"{SERVICE_URL}/v1/sessions/{shared_id}/query",
                          {"query": args.ask, "agent_session": asker})
        except urllib.error.HTTPError as exc:
            # ⟨decision 008⟩ /query answers 503 `retrieval_unavailable` when the
            # ranking model is not answering. `urlopen` raises on that, so this
            # script — the one a presenter drives BY HAND, in front of people —
            # used to end the sentence in a urllib traceback. The outage is the
            # honest result and it deserves to read like one; the traceback
            # reads like the demo tool is broken.
            detail = {}
            try:
                detail = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                pass
            if exc.code == 503 and detail.get("error") == "retrieval_unavailable":
                print(f"\n  asked {whose}: {args.ask!r}")
                print(f"  SHARED MEMORY IS DOWN, NOT EMPTY — its model backend "
                      f"({detail.get('provider', 'unknown')}) is not answering, "
                      f"so the findings are all still there and cannot be "
                      f"searched.")
                print("  This is the loud failure working: before decision 008 "
                      "this same outage came back as an empty list and a 200.")
                print("  If serve_local.py is supervising the seam it restarts "
                      "within ~2 minutes — see .synapse/logs/supervisor.log.")
                return 1
            raise SystemExit(
                f"the service answered HTTP {exc.code} to that query "
                f"({detail or 'no body'}).") from None
        findings = answer.get("findings") or []
        print(f"\n  asked {whose}: {args.ask!r}")
        print(f"  {len(findings)} finding(s) came back")
        for finding in findings:
            who = ", ".join(a["contributor"] for a in finding.get("attributions", []))
            print(f"    → ({finding['type']}, from {who or 'synthesis'}) {finding['text']}")
        if not findings and asker != "as-observer":
            print("    nothing — either shared memory is empty, or everything in "
                  "it is already yours (invariant 3)")
        return 0

    args.who = args.who or next(iter(FEED))   # sending work needs somebody to be
    if args.who not in sessions:
        raise SystemExit(f"No worker is running for {args.who!r}. "
                         f"Running: {', '.join(sessions) or 'nobody'}")
    agent_session = sessions[args.who]
    transcript = DEMO / "transcripts" / f"{agent_session}.jsonl"

    if args.segment:
        if not (SEGMENTS / f"{args.segment}.json").exists():
            raise SystemExit(f"No such segment {args.segment!r}. Try --list.")
        append_segment(transcript, args.segment, agent_session)
        print(f"\n  sent {args.segment} as {args.who} → {transcript.name}")
    elif args.say:
        from datetime import datetime, timezone
        line = json.dumps({
            "type": "user",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sessionId": agent_session,
            "cwd": str(DEMO),
            "gitBranch": "main",
            "message": {"role": "user", "content": [{"type": "text", "text": args.say}]},
        })
        with transcript.open("a") as handle:
            handle.write(line + "\n")
        print(f"\n  sent your words as {args.who} → {transcript.name}")
        print("  offline note: the stand-in only answers for corpus fixtures, so "
              "expect triage to KEEP this and the model to return nothing. "
              "Run the walkthrough with --live to have your own words distilled.")
    else:
        parser.error("give me something to do: --segment, --say, --ask or --list")

    port = FEED[args.who]["port"]
    print(f"  watch http://127.0.0.1:{port}/debug — the worker polls every few "
          f"seconds and closes the segment once the turn goes quiet (~15s)")
    show_state(shared_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
