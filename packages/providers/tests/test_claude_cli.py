"""ClaudeCliProvider — Claude through the local binary, on a subscription.

The subprocess is faked throughout: these pin the contract with the CLI's JSON
and the two things that make this arm unlike the others (cached-token
accounting, and an agent restricted to a pure text transform), not the CLI
itself.
"""

from __future__ import annotations

import json

import pytest
from synapse_providers.claude_cli_provider import (
    ClaudeCliCallError,
    ClaudeCliProvider,
    _flatten_prompt,
    _true_prompt_tokens,
)

MESSAGES = [
    {"role": "system", "content": "You compress agent sessions."},
    {"role": "user", "content": "SEGMENT:\nthe decode fails past 40 ms"},
]
SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self._out, self._err, self.returncode = stdout, stderr, returncode

    async def communicate(self):
        return self._out, self._err

    def kill(self):  # pragma: no cover - only reached on the timeout path
        pass

    async def wait(self):  # pragma: no cover
        pass


def _cli_json(result: str, **usage) -> bytes:
    base = {"input_tokens": 10, "cache_creation_input_tokens": 13456,
            "cache_read_input_tokens": 17536, "output_tokens": 91}
    return json.dumps({"is_error": False, "result": result,
                       "usage": {**base, **usage}}).encode()


@pytest.fixture
def spawn(monkeypatch):
    """Capture argv and hand back a scripted process."""
    captured: dict = {}

    def install(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        async def fake_exec(*argv, **kwargs):
            captured["argv"] = list(argv)
            return _FakeProcess(stdout, returncode, stderr)

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
        return captured

    return install


async def test_reports_the_whole_prompt_including_the_cached_part(spawn):
    """THE defect this arm could have shipped with.

    Measured on CLI 2.1.223: a ~31,000-token prompt reports
    `input_tokens = 10`, because that field is the UNCACHED REMAINDER and the
    CLI ships a large cached system context on every call.
    `guards.assert_prompt_conditioned` fails a call at `input_tokens <= 1`
    precisely because a model that dropped its prompt invents schema-plausible
    findings from nothing. Passing 10 through would leave that guard
    technically green and functionally decorative on this arm.
    """
    spawn(_cli_json('{"findings": []}'))
    result = await ClaudeCliProvider().complete(MESSAGES, response_schema=SCHEMA)

    assert result.usage.input_tokens == 10 + 13456 + 17536
    assert result.usage.output_tokens == 91


def test_true_prompt_tokens_tolerates_missing_and_null_cache_fields():
    assert _true_prompt_tokens({"input_tokens": 7}) == 7
    assert _true_prompt_tokens({"input_tokens": 7, "cache_read_input_tokens": None}) == 7
    assert _true_prompt_tokens({}) == 0


async def test_runs_as_a_pure_text_transform_not_an_agent(spawn):
    """`claude -p` is the full agent. Left unrestricted it can read files, run
    commands and search the web mid-answer — any of which could fold content
    into a Finding that the Segment never contained."""
    captured = spawn(_cli_json("{}"))
    await ClaudeCliProvider().complete(MESSAGES, response_schema=SCHEMA)
    argv = captured["argv"]

    assert "--allowedTools" in argv and argv[argv.index("--allowedTools") + 1] == ""
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1"
    assert argv[1] == "-p"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"


async def test_the_system_prompt_is_kept_out_of_the_users_turn(spawn):
    """`build_messages` emits the OpenAI shape; the CLI takes one prompt plus an
    appended system prompt. Concatenating them would put the pack's
    instructions where the Segment's content belongs."""
    captured = spawn(_cli_json("{}"))
    await ClaudeCliProvider().complete(MESSAGES, response_schema=SCHEMA)
    argv = captured["argv"]

    assert argv[2] == "SEGMENT:\nthe decode fails past 40 ms"
    assert argv[argv.index("--append-system-prompt") + 1] == "You compress agent sessions."


async def test_prompt_instructed_json_goes_through_the_tolerant_parser(spawn):
    """No schema is enforced end to end, so this arm needs the same tolerant
    recovery as the NPU — including the truncation repair, since a CLI answer
    can stop a token early exactly like a local model's."""
    spawn(_cli_json('{"findings": [{"type": "learning", "text": "a 40 ms window"}]'))
    result = await ClaudeCliProvider().complete(MESSAGES, response_schema=SCHEMA)

    assert result.schema_valid is True
    assert result.data["findings"][0]["text"] == "a 40 ms window"


async def test_a_missing_binary_says_what_to_do_about_it(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ClaudeCliCallError, match="not on PATH"):
        await ClaudeCliProvider().complete(MESSAGES)


async def test_a_nonzero_exit_raises_rather_than_returning_empty(spawn):
    """Returning an empty ModelResult would read downstream as 'the model found
    nothing worth keeping', which is a different and much quieter claim than
    'the call failed'."""
    spawn(b"", returncode=1, stderr=b"not logged in")
    with pytest.raises(ClaudeCliCallError, match="exited 1"):
        await ClaudeCliProvider().complete(MESSAGES)


async def test_an_error_the_cli_reports_in_its_own_json_is_not_read_as_output(spawn):
    spawn(json.dumps({"is_error": True, "result": "usage limit reached",
                      "usage": {}}).encode())
    with pytest.raises(ClaudeCliCallError, match="usage limit reached"):
        await ClaudeCliProvider().complete(MESSAGES)


async def test_a_renamed_field_fails_loudly_because_the_shape_is_not_a_contract(spawn):
    """The CLI's JSON is a tool's output, not an API contract: an upgrade could
    rename `result` without anyone considering it breaking. That must surface as
    one clear error, not a distiller that quietly stops producing findings."""
    spawn(json.dumps({"is_error": False, "text": "hi", "usage": {}}).encode())
    with pytest.raises(ClaudeCliCallError, match="missing result"):
        await ClaudeCliProvider().complete(MESSAGES)


async def test_output_that_is_not_json_at_all_names_the_version_as_the_suspect(spawn):
    spawn(b"Welcome to Claude Code!\n")
    with pytest.raises(ClaudeCliCallError, match="not a stable contract"):
        await ClaudeCliProvider().complete(MESSAGES)


def test_schema_native_stays_false_so_tolerant_parsing_keeps_running(spawn):
    """`-p` returns whatever the model wrote; nothing enforces a schema. Setting
    this True would skip the recovery this arm depends on."""
    assert ClaudeCliProvider().capabilities.native_structured_output is False


# --- flattening a chat into one prompt argument -----------------------------------
#
# W7 F1 (docs/overnight/w7-live-evidence.md): joining every non-system turn with
# blank lines produced a prompt in which the few-shot inputs are labelled
# "developer:"/"agent:" exactly like the rendered Segment, the few-shot outputs are
# bare JSON with nothing marking them as answers, and the pack's suffix then says
# "rewrite the session above". Six of nine returned findings were the pack's own
# examples, attributed to a real engineer.

FEW_SHOT_CHAT = [
    {"role": "system", "content": "You compress agent sessions."},
    {"role": "user", "content": "agent: switched the queue to the durable broker"},
    {"role": "assistant", "content": '{"findings": [{"type": "decision", "text": "ex 1"}]}'},
    {"role": "user", "content": "agent: cached the templates in process memory"},
    {"role": "assistant", "content": '{"findings": [{"type": "dead_end", "text": "ex 2"}]}'},
    {"role": "user", "content": "agent: the decode fails past 40 ms\n\n---\nRewrite the session above."},
]


def test_the_few_shots_are_marked_as_examples_rather_than_read_as_the_session():
    """The fix for F1. Every example block is named, and the material to work on
    is fenced off from everything above it — because on this arm the wire format
    cannot carry that distinction and the text has to."""
    prompt = _flatten_prompt(FEW_SHOT_CHAT)

    assert "EXAMPLE 1 · INPUT" in prompt
    assert "EXAMPLE 1 · THE REPLY THAT WAS WANTED" in prompt
    assert "EXAMPLE 2 · INPUT" in prompt
    assert "EXAMPLE 2 · THE REPLY THAT WAS WANTED" in prompt
    assert "THE MATERIAL TO WORK ON" in prompt
    assert "never repeat or paraphrase its content" in prompt


def test_the_example_answers_are_no_longer_bare_json_adjacent_to_the_segment():
    """The precise shape that produced F1: an example's output sitting in the
    prompt with nothing between it and the real transcript to say it was an
    answer to a different question."""
    prompt = _flatten_prompt(FEW_SHOT_CHAT)
    before_segment, _, after = prompt.partition("agent: the decode fails past 40 ms")

    assert after, "the segment must still be in the prompt"
    for example_output in ('{"findings": [{"type": "decision", "text": "ex 1"}]}',
                           '{"findings": [{"type": "dead_end", "text": "ex 2"}]}'):
        assert example_output in before_segment
        head, _, _ = before_segment.partition(example_output)
        assert "THE REPLY THAT WAS WANTED" in head, (
            "an example output reached the model with no marker in front of it"
        )
    # And the last thing the model reads before the transcript is the fence.
    assert before_segment.rstrip().endswith(
        "Answer about what follows and about nothing above it. ==="
    )


def test_the_turn_order_and_content_survive_the_flattening():
    """Marking must not reorder or drop anything — the segment is the payload."""
    prompt = _flatten_prompt(FEW_SHOT_CHAT)
    positions = [
        prompt.index("switched the queue"),
        prompt.index('"text": "ex 1"'),
        prompt.index("cached the templates"),
        prompt.index('"text": "ex 2"'),
        prompt.index("the decode fails past 40 ms"),
    ]

    assert positions == sorted(positions)


def test_a_single_turn_call_is_byte_identical_to_what_this_arm_sent_before():
    """`guards.check_canary` and every ad-hoc probe send one user turn. There are
    no examples there to disambiguate from, so adding a fence would be pure noise
    in the one call whose whole job is to prove the model reads its prompt."""
    assert _flatten_prompt(MESSAGES) == "SEGMENT:\nthe decode fails past 40 ms"
    assert _flatten_prompt([{"role": "user", "content": "which host expired?"}]) == (
        "which host expired?"
    )


def test_an_unpaired_trailing_turn_still_reaches_the_model():
    """Defensive: the flattener decides what an example is by pairing, and a
    caller that sends something else must lose no content to that guess."""
    prompt = _flatten_prompt(
        [
            {"role": "user", "content": "<<shot-in>>"},
            {"role": "assistant", "content": "<<shot-out>>"},
            {"role": "user", "content": "<<tail-one>>"},
            {"role": "user", "content": "<<tail-two>>"},
        ]
    )

    for block in ("<<shot-in>>", "<<shot-out>>", "<<tail-one>>", "<<tail-two>>"):
        assert block in prompt
    assert prompt.index("<<tail-one>>") < prompt.index("<<tail-two>>")


async def test_the_prompt_the_cli_actually_receives_carries_the_markers(spawn):
    """End to end through argv, not just through the helper — the marking is
    worthless if `complete` does not use it."""
    captured = spawn(_cli_json('{"findings": []}'))
    await ClaudeCliProvider().complete(FEW_SHOT_CHAT, response_schema=SCHEMA)

    prompt = captured["argv"][2]

    assert "EXAMPLE 1 · INPUT" in prompt
    assert "THE MATERIAL TO WORK ON" in prompt
    assert "You compress agent sessions." not in prompt, (
        "the system prompt still belongs in --append-system-prompt"
    )
