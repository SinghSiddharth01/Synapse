"""A stand-in for both model endpoints, so the whole pipeline runs on a laptop.

Synapse talks to exactly two models, across two seams:

    worker distiller    -> NPUProvider    -> POST <base>/chat/completions
                                             (`geniex serve`, 127.0.0.1:18181)
    service synthesizer -> AIC100Provider -> POST <base>/completions
                                             (Cirrascale)

This server answers both, at one address. Nothing under `packages/` knows it
exists: the worker still builds an `NPUProvider`, the service still builds an
`AIC100Provider`, and each is pointed here with the very same environment
variable it would use to reach the real thing (`SYNAPSE_BASE_URL`,
`INFERENCE_CLOUD_BASE_URL`). The seam is where the hardware is, so a Mac with
no NPU and no cloud key can still run every other line of the system unchanged.

Two modes:

  --mode replay (default, offline, deterministic)
      Answers from this repo's own corpus, and never writes prose of its own.
      The canary answer is EXTRACTED from the prompt. A distil request is
      matched against `fixtures/segments/*.json` by verbatim event text and
      answered with that fixture's recorded goldens
      (`fixtures/findings/*.findings.json`). A synthesis request merges the
      corpus's known duplicate pair, and the merged text is the two sources'
      own words joined — clumsy on purpose, because writing the clean version
      is the synthesizer's job and a stub that seems to do it well is lying
      about which component did the work. Content that matches no fixture gets
      an EMPTY answer: a stub that invents findings reproduces exactly the
      failure `distiller/guards.py` exists to catch, and would quietly teach
      the pipeline to trust noise.

      Retrieval ranking is an IDENTITY function — every candidate, in listing
      order, with the query unread. Retrieval quality is not observable in
      replay mode; suppression (which candidates are offered at all) is, since
      that runs in real code before the model is consulted.

  --mode proxy
      Forwards both endpoints to a real OpenAI-compatible server, attaching an
      API key. The same run then exercises a real model with the worker and
      service code paths untouched. The host and key both come from
      `secrets.jsonc`'s `inference_cloud` block unless `--upstream` overrides
      the host — one instance's key must never be sent to another's host.

      WHICH INSTANCE MATTERS. Run live against `aisuite.cirrascale.com` on
      2026-08-05 and the distiller seam fails in a specific, silent-looking
      way: the canary passes, then every distil call returns EMPTY content
      while billing ~107 output tokens, twice (the distiller's one retry), and
      no finding is produced. That is this instance's `/chat/completions`
      eating emitted JSON into `tool_calls` — the same defect
      `aic100.py` routes around by using `/completions` for schema calls,
      observed here from the other side. The Indonesian instance
      (`aisuite-indonesia.cirrascale.com/apis/v2`) does not do this. If a live
      run shows zero findings and two calls per segment, this is why; it is
      not the prompt and not the pipeline.

WHAT THIS IS NOT: it is not a model, and it is not the NPU. No timing, tok/s,
or quality number from replay mode means anything. The measurements that
matter still exist only on the X Elite box — see
`docs/plans/exec/2026-08-05-e8-npu-testing.md`. This exists so the plumbing can
be *watched*, not so anything can be *measured*.

    uv run python scripts/local_model_server.py                  # replay, :18181
    uv run python scripts/local_model_server.py --mode proxy \
        --upstream https://<host>/apis/v2 --api-key-env INFERENCE_CLOUD_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SEGMENTS = REPO / "fixtures" / "segments"
GOLDENS = REPO / "fixtures" / "findings"

# guards.py's canary. The answer is EXTRACTED from the prompt (below), never
# emitted as a constant: a stub that recognised the question and replied with a
# memorised `api.internal` would be gaming the guard, and the guard's whole
# purpose is to prove the prompt was read.
CANARY_MARKER = "which hostname's certificate expired"
CANARY_HOST_PATTERN = re.compile(r"certificate for the host (\S+?)\s+expired", re.I)

# The corpus's one known duplicate pair (seg-005a and seg-005b): the same ~40 ms
# DMA timing window, found twice by two contributors 24 findings apart. Replay
# mode merges findings carrying this signature and nothing else — the rule is
# corpus-specific on purpose, because a general "merge things that look similar"
# stub would be a model, and this is not one.
#
# The two goldens are deliberately worded apart ("about 40 ms" vs "roughly 40
# milliseconds"), which is the whole difficulty a real synthesizer is there to
# handle. A lexical stub has to normalize the unit to see through it; that
# normalization is this stub's crutch, not a property of the system.
#
# ANCHORED, and paired with a subject word. A bare `"40 ms" in text` also
# matches "140 ms" and "1440 ms", so a teammate contributing "the retry backoff
# is 140 ms" into a still-running demo would have it silently merged into the
# DMA finding — a wrong merge that tombstones a real finding, live.
MERGE_SIGNATURE = re.compile(r"(?<!\d)40\s*ms\b")
# Stems, not whole words: the pair says "DMA writes" and "decoding fails", and
# "decoding" does not contain "decode". Matching on "decode" silently stopped
# the flagship merge from firing at all.
MERGE_SUBJECT = ("dma", "decod")


def _normalize(text: str) -> str:
    return (text.lower()
            .replace("milliseconds", "ms").replace("millisecond", "ms")
            .replace("msec", "ms"))


def _tokens(text: str) -> int:
    """A ~4-chars-per-token estimate, reported as `usage`.

    NO FLOOR, deliberately. An earlier version returned `max(2, ...)`, which is
    exactly calibrated to clear `assert_prompt_conditioned`'s `input_tokens > 1`
    — including for an EMPTY prompt. That would have made the one guard a
    stand-in can honestly exercise structurally unable to fire: any regression
    that emptied a prompt before it left the worker (a render bug, compaction
    truncating a segment to nothing, a `distil_kinds` misconfiguration) would
    report a comfortable non-zero size and read as "the model looked and found
    nothing". An empty prompt must count as zero tokens and trip the guard.
    """
    return len(text) // 4


class Corpus:
    """The repo's fixtures, indexed for matching a live prompt back to them."""

    def __init__(self) -> None:
        self.segments: list[tuple[str, list[str], list[dict[str, Any]]]] = []
        for path in sorted(SEGMENTS.glob("*.json")):
            segment = json.loads(path.read_text())
            texts = [
                str(e.get("content", "")).strip()
                for e in segment.get("events", [])
                if len(str(e.get("content", "")).strip()) > 40
            ]
            golden_path = GOLDENS / f"{segment['id']}.findings.json"
            goldens = json.loads(golden_path.read_text()) if golden_path.exists() else []
            self.segments.append((segment["id"], texts, goldens))

    def match(self, prompt: str) -> tuple[str, list[dict[str, Any]]] | None:
        """Which fixture segment is this prompt carrying?

        EVERY substantive line of the fixture must appear verbatim. A partial
        match returns None, because returning a fixture's whole golden set for
        a prompt that carried only part of it is the masking path this file
        exists to close: compaction truncating a segment, a render regression
        dropping tool_result content, or a segmenter split would all still
        produce a full set of findings for content the model never saw.
        """
        for seg_id, texts, goldens in self.segments:
            if texts and all(t in prompt for t in texts):
                return seg_id, goldens
        return None


def _parse_listing(prompt: str) -> list[tuple[str, str]]:
    """The `[id] (type) text` lines both the synthesis and retrieval prompts
    build. Returns (id, text) pairs in prompt order."""
    pairs = []
    for line in prompt.splitlines():
        m = re.match(r"^\[([^\]]+)\]\s+\(([a-z_]+)\)\s+(.*)$", line.strip())
        if m:
            pairs.append((m.group(1), m.group(3)))
    return pairs


class Replay:
    """Deterministic answers drawn from the corpus. Never invents content."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus

    def answer(self, prompt: str) -> tuple[str, str]:
        """-> (kind, response_text). `kind` is for the operator's log line."""
        low = prompt.lower()

        if CANARY_MARKER in low:
            found = CANARY_HOST_PATTERN.search(prompt)
            # No match means the canary prompt changed shape. Answering wrong
            # is correct here: the worker then refuses to start, which is what
            # should happen when the stand-in can no longer read the prompt.
            return "canary", found.group(1) if found else "(no hostname in prompt)"

        # Retrieval ranks a positional listing; synthesis reconciles an
        # id-keyed one. Both are asked for as JSON, and the key each wants is
        # named in its own prompt.
        if '"ranked"' in prompt:
            # Identity: every candidate, listing order, query unread. Named
            # `rank/identity` in the log so a run never looks like it ranked.
            count = len(_parse_listing(prompt))
            return "rank/identity", json.dumps({"ranked": list(range(count))})

        if "working memory" in low or '"merges"' in low:
            return "synthesis", self._synthesis(prompt)

        return self._distil(prompt)

    def _distil(self, prompt: str) -> tuple[str, str]:
        matched = self.corpus.match(prompt)
        if matched is None:
            # No fixture carries this text. An empty findings list is the
            # honest answer; anything else would be invention.
            return "distil/unmatched", json.dumps({"findings": []})
        seg_id, goldens = matched
        findings = [{"type": g["type"], "text": g["text"]} for g in goldens]
        return f"distil/{seg_id}", json.dumps({"findings": findings})

    def _synthesis(self, prompt: str) -> str:
        """The merge verdict — with the merged TEXT built out of the sources.

        An earlier version returned a hand-written sentence here ("The decode
        failure is a ~40 ms timing window between the two DMA writes..."). That
        sentence appears in no fixture: it was prose invented by this file,
        landing in a real SYNTHESIZED Finding, printed by the demo and shown on
        the service dashboard as a reconciliation — fluent enough to read as
        model output. It contradicted this module's own no-invention rule at
        exactly the point the demo draws the eye to.

        So the merged text is now the sources' own words, joined. It reads
        clumsily on purpose: rewriting two findings into one clean sentence is
        the synthesizer's job, and a stub that appears to do it well is lying
        about which component did the work.
        """
        listed = _parse_listing(prompt)
        duplicates = [
            (fid, text) for fid, text in listed
            if MERGE_SIGNATURE.search(_normalize(text))
            and any(word in _normalize(text) for word in MERGE_SUBJECT)
        ]
        merges = []
        # Exactly the pair this rule was written for. Three matches means
        # something arrived that the rule was never designed to judge, and
        # folding N sources into one text would drop whatever the extra ones
        # added — the thing invariant 5 forbids.
        if len(duplicates) == 2:
            merges.append({
                "source_ids": [fid for fid, _ in duplicates],
                "text": " ".join(text for _, text in duplicates),
                "type": "learning",
            })
        return json.dumps({
            "working_memory": (
                f"{len(listed)} findings under review; "
                f"{len(merges)} merged by the replay stub's corpus rule."
            ),
            "merges": merges,
            "trivial_ids": [],
            "conflicts": [],
        })


class Proxy:
    """Forward verbatim to a real OpenAI-compatible server.

    The two endpoints can carry different models, because in production they
    are different machines: `/chat/completions` is the worker's distiller (a
    small model, on the NPU) and `/completions` is the service's synthesizer
    (a large model, in the cloud). Pointing both at one model would proxy a
    shape the system never actually runs.
    """

    def __init__(self, upstream: str, api_key: str | None,
                 chat_model: str | None, completions_model: str | None) -> None:
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.chat_model = chat_model
        self.completions_model = completions_model

    def forward(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        model = self.chat_model if path.endswith("/chat/completions") else self.completions_model
        if model:
            body = {**body, "model": model}
        request = urllib.request.Request(
            f"{self.upstream}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())


def _eaten_json(payload: dict[str, Any]) -> str | None:
    """Did the upstream swallow the model's JSON into `tool_calls`?

    The signature, observed live on `aisuite.cirrascale.com` 2026-08-05: empty
    `content`, a non-trivial `completion_tokens`, and (usually) a `tool_calls`
    field. The model emitted its JSON; the server's tool-call parser consumed
    it. See this module's proxy-mode note and `aic100.py`'s docstring.
    """
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") if "message" in choice else choice.get("text")) or ""
    produced = int((payload.get("usage") or {}).get("completion_tokens", 0) or 0)
    if content.strip() or produced < 2:
        return None
    where = "tool_calls" if message.get("tool_calls") else "an empty response"
    return (f"upstream returned {produced} completion tokens but empty content "
            f"({where}) — this endpoint eats emitted JSON; use the instance "
            f"whose /completions route the synthesizer already relies on")


def _completion_envelope(model: str, text: str, prompt: str, chat: bool) -> dict[str, Any]:
    """The OpenAI response shape both providers read. `usage` is truthful about
    the prompt it actually received — the prompt-drop guard depends on it."""
    message = (
        {"message": {"role": "assistant", "content": text}} if chat else {"text": text}
    )
    return {
        "id": "local-stand-in",
        "object": "chat.completion" if chat else "text_completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", **message}],
        "usage": {
            "prompt_tokens": _tokens(prompt),
            "completion_tokens": _tokens(text),
            "total_tokens": _tokens(prompt) + _tokens(text),
        },
    }


def build_handler(args: argparse.Namespace, replay: Replay, proxy: Proxy | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:  # noqa: A003 - silence the default
            pass

        def _send(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path.rstrip("/").endswith("/models"):
                self._send({"object": "list", "data": [{"id": args.model}]})
            else:
                self._send({"error": f"no route {self.path}"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send({"error": "body is not JSON"}, status=400)
                return

            route = self.path.rstrip("/")
            chat = route.endswith("/chat/completions")
            if not chat and not route.endswith("/completions"):
                self._send({"error": f"no route {self.path}"}, status=404)
                return

            prompt = (
                "\n".join(str(m.get("content", "")) for m in body.get("messages", []))
                if chat else str(body.get("prompt", ""))
            )

            started = time.perf_counter()
            if proxy is not None:
                try:
                    payload = proxy.forward(
                        "/chat/completions" if chat else "/completions",
                        body, args.timeout,
                    )
                    kind = "proxy"
                    if (eaten := _eaten_json(payload)) is not None:
                        # An upstream that swallowed the model's JSON into
                        # tool_calls returns HTTP 200 with empty content, which
                        # the distiller can only read as "the model said
                        # nothing". Surfacing it as a 502 names the real cause
                        # instead of letting the segment drop silently.
                        self._send({"error": eaten}, status=502)
                        print(f"  [{time.strftime('%H:%M:%S')}] proxy ATE THE JSON: {eaten}",
                              file=sys.stderr, flush=True)
                        return
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    # Surface upstream failure as a 502 rather than a stub
                    # answer: a silent fallback to replay would look like the
                    # real model succeeded.
                    self._send({"error": f"upstream failed: {exc}"}, status=502)
                    print(f"  [{time.strftime('%H:%M:%S')}] proxy FAILED: {exc}",
                          file=sys.stderr, flush=True)
                    return
            else:
                kind, text = replay.answer(prompt)
                if args.simulate_npu_tok_per_sec:
                    # Opt-in, and labelled at startup: makes the worker
                    # dashboard's live counter visibly tick. Simulated, so it
                    # measures nothing.
                    time.sleep(_tokens(text) / args.simulate_npu_tok_per_sec)
                payload = _completion_envelope(args.model, text, prompt, chat)

            elapsed = int((time.perf_counter() - started) * 1000)
            endpoint = "chat" if chat else "text"
            print(
                f"  [{time.strftime('%H:%M:%S')}] {endpoint:<4} {kind:<20} "
                f"prompt={len(prompt):>6}ch  {elapsed:>5}ms",
                file=sys.stderr, flush=True,
            )
            self._send(payload)

    return Handler


def _credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """-> (api_key, base_url). Environment first, then the gitignored
    `secrets.jsonc`, whose credentials live under an `inference_cloud` block
    (`api_key`, `base_url`, `model`); a bare top-level `api_key` is accepted
    too, since that is the shape `rehearse_demo.py` matches.

    The key is never printed, logged, echoed, or written anywhere — only
    whether one was found. A commented-out block in `secrets.jsonc` is not a
    credential: comments are stripped before parsing, so an instance the team
    has commented out stays unused rather than being silently resurrected here.
    """
    key = os.environ.get(args.api_key_env)
    base_url = os.environ.get("INFERENCE_CLOUD_BASE_URL")
    secrets = REPO / "secrets.jsonc"
    if (not key or not base_url) and secrets.exists():
        stripped = re.sub(r"^\s*//.*$", "", secrets.read_text(), flags=re.MULTILINE)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = {}
        block = data.get("inference_cloud") or {}
        key = key or block.get("api_key") or data.get("api_key")
        base_url = base_url or block.get("base_url")
    return (str(key) if key else None, str(base_url) if base_url else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18181)
    parser.add_argument("--mode", choices=("replay", "proxy"), default="replay")
    parser.add_argument("--upstream", help="proxy mode: the real OpenAI-compatible base URL")
    parser.add_argument("--api-key-env", default="INFERENCE_CLOUD_API_KEY")
    parser.add_argument("--model", default="local-stand-in",
                        help="what to report in /v1/models and echo as the response model")
    parser.add_argument("--upstream-model",
                        help="proxy mode: the model name sent upstream on both endpoints")
    parser.add_argument("--distiller-model",
                        help="proxy mode: model for /chat/completions (the worker's "
                             "distiller — the NPU's seat). Defaults to --upstream-model.")
    parser.add_argument("--synthesizer-model",
                        help="proxy mode: model for /completions (the service's "
                             "synthesizer — the cloud's seat). Defaults to --upstream-model.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--simulate-npu-tok-per-sec", type=float, default=0.0,
                        help="replay mode: pace responses to make the dashboard tick. "
                             "SIMULATED — measures nothing.")
    args = parser.parse_args(argv)

    proxy = None
    if args.mode == "proxy":
        key, configured_url = _credentials(args)
        upstream = args.upstream or configured_url
        if not key:
            print(f"--mode proxy found no key in ${args.api_key_env} or "
                  f"secrets.jsonc's inference_cloud block", file=sys.stderr)
            return 2
        if not upstream:
            print("--mode proxy needs --upstream (or a base_url in secrets.jsonc)",
                  file=sys.stderr)
            return 2
        args.upstream = upstream
        proxy = Proxy(upstream, key,
                      args.distiller_model or args.upstream_model,
                      args.synthesizer_model or args.upstream_model)

    replay = Replay(Corpus())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(args, replay, proxy))

    print(f"local model stand-in on http://127.0.0.1:{args.port}/v1  (mode: {args.mode})")
    if proxy is not None:
        print(f"  proxying to {args.upstream} with a key from "
              f"${args.api_key_env}/secrets.jsonc (never logged)")
        print(f"  distiller (chat):     {proxy.chat_model or '(caller-supplied)'}")
        print(f"  synthesizer (text):   {proxy.completions_model or '(caller-supplied)'}")
    else:
        print(f"  replaying {len(replay.corpus.segments)} corpus fixtures — "
              f"NOT a model, NOT the NPU; no number from this run is a measurement")
        if args.simulate_npu_tok_per_sec:
            print(f"  pacing at a SIMULATED {args.simulate_npu_tok_per_sec} tok/s")
    print("  point the worker here with SYNAPSE_BASE_URL, the service with "
          "INFERENCE_CLOUD_BASE_URL\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
