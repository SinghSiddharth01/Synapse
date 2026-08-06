"""Static-content checks for the rest of the Claude Code awareness pack
(`packs/claude-code/`) — the skill (signal ④), the hook wiring, and the
install doc. `tests/test_awareness_pack.py` covers the freshness pointer
hook's runtime behavior; this file covers everything that ships alongside
it and does not run as a subprocess.

Plan D.6 / Chain C's "Done when": "skill description matches CONTEXT.md
vocabulary" and "the pack is a shipped artifact, not installed into this
repo's own `.claude/`."
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "claude-code"
SKILL_MD = PACK / "skills" / "synapse-shared-memory" / "SKILL.md"
SETTINGS_SNIPPET = PACK / "settings-snippet.json"
INSTALL_MD = PACK / "INSTALL.md"
HOOK = PACK / "hooks" / "freshness_pointer.py"


def _frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-frontmatter reader for the two flat string keys a
    SKILL.md needs (`name`, `description`) — no PyYAML dependency pulled in
    just to read two lines in a test."""
    assert text.startswith("---\n"), "SKILL.md must open with a --- frontmatter block"
    _, frontmatter, _body = text.split("---\n", 2)
    fields: dict[str, str] = {}
    current_key = None
    for line in frontmatter.splitlines():
        if line.startswith((" ", "\t")):  # a continuation line of the previous key
            assert current_key is not None
            fields[current_key] += " " + line.strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current_key = key.strip()
            fields[current_key] = value.strip()
    return fields


# ---------------------------------------------------------------------------
# The skill — signal ④, architecture.html's frontmatter draft.
# ---------------------------------------------------------------------------

def test_skill_frontmatter_has_the_trigger_voiced_description():
    fields = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    assert fields["name"] == "synapse-shared-memory"

    description = fields["description"]
    # The four trigger scenarios from architecture.html's own draft --
    # dropping any of them is dropping a scenario the skill was designed to
    # catch, not a wording nit.
    for phrase in (
        "unfamiliar subsystem",
        "debugging",
        "already be known",
        "dead end",
    ):
        assert phrase in description, f"skill description dropped trigger phrase {phrase!r}"
    assert "Checks what the team already learned" in description


def test_skill_description_uses_context_md_vocabulary_not_its_avoid_list():
    """CONTEXT.md: Shared Session's `_Avoid_` list is 'session (unqualified),
    room, channel, team session'; Contributor's is 'user, member, author,
    participant.' The architecture.html draft this skill's description is
    built from says 'a joined Synapse session' -- bare, exactly what
    CONTEXT.md's vocabulary asks the rest of the repo to avoid. The
    Done-when criterion is vocabulary compliance, not a byte-for-byte copy
    of the draft, so this pins the corrected term rather than the literal
    HTML string."""
    fields = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    description = fields["description"]
    assert "Shared Session" in description
    assert "a joined Synapse session" not in description
    lowered = description.lower()
    for avoided in ("room", "channel", "user", "member", "participant"):
        assert re.search(rf"\b{avoided}\b", lowered) is None, (
            f"description uses avoided term {avoided!r}")


def test_skill_body_tells_the_agent_to_say_plainly_when_memory_had_nothing():
    """architecture.html: 'That last instruction matters — an agent that
    silently finds nothing is indistinguishable from one that never
    looked.'

    Pin the sentence, not two stopwords. `"nothing" in body.lower()` and
    `"say" in body.lower()` each survive on their own elsewhere in this same
    body ('There is nothing to attach to...', 'both tools **say** so
    plainly') even with instruction 3 deleted outright -- verified by
    mutation before writing this fix. Scoping to the numbered instruction
    block the way
    `tests/test_vocabulary.py::test_the_tombstone_ENTRY_says_derived_condition_not_just_the_notes`
    scopes to one entry is what actually requires the sentence to exist."""
    text = SKILL_MD.read_text(encoding="utf-8")
    body = text.split("---\n", 2)[2]
    match = re.search(r"^3\.(.+?)(?=^\d+\.|\Z)", body, re.DOTALL | re.MULTILINE)
    assert match is not None, "SKILL.md's body must have a numbered instruction 3"
    instruction_three = match.group(1).lower()
    assert "returns nothing" in instruction_three
    assert "plainly" in instruction_three


def test_skill_teaches_the_agent_to_pass_its_own_agent_session_id():
    """W2 (2026-08-06): one agent invocation is one Agent Session, so a
    second Claude Code window on the same machine is a different
    participant. Nothing in the MCP protocol tells the server which
    conversation is calling -- the tool argument is the only channel there
    is. A skill that still said "there is no session id to pass" (its
    wording before this) would leave every multi-window machine falling
    back to the most recently joined binding, which is a guess: it either
    hides a sibling window's findings or replays the caller's own back as
    team knowledge, silently, with no error either way.

    Pins the mechanism, not the prose: the argument name, where the agent
    gets its value, and that it is asked for on the two tools the skill
    drives."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "agent_session_id" in text
    assert "CLAUDE_CODE_SESSION_ID" in text
    body = text.split("---\n", 2)[2]
    section = body.split("## Say which conversation you are", 1)
    assert len(section) == 2, "SKILL.md must have the section that teaches the id"
    section_text = section[1]
    assert "mcp__synapse__query" in section_text
    assert "mcp__synapse__contribute" in section_text
    # The skill is a shipped artifact installed on machines whose Synapse
    # may be older than it -- the same reason the worker keeps writing the
    # legacy binding mirror. It must not instruct an agent into a hard
    # failure against a server that predates the argument.
    assert "without the argument" in section_text


def test_install_md_documents_the_two_window_check():
    """The pack's own verification section is where a teammate finds out
    whether per-conversation identity actually works on their machine --
    two binding files, and each window seeing the other's contribution but
    not its own. Both halves are checkable in a minute and neither is
    discoverable from the hook's output, which is silent either way."""
    text = INSTALL_MD.read_text(encoding="utf-8")
    assert "### Verify two windows are two participants" in text
    assert "ls .synapse/bindings/claude-code/" in text
    # The stale limitation this replaced -- if it comes back, the doc is
    # telling teammates the opposite of what the code now does.
    assert "One binding per Agent product per machine." not in text


def test_skill_names_the_real_mcp_tools_not_a_rejected_attach_mechanism():
    """Plan D.3: 'There is no attach(shared_id).' An earlier version of the
    orchestrator's own MCP surface built exactly that, then deleted it
    (server.py's module docstring) -- a skill that reinvents it would repeat
    a mistake this repo has already made and recorded once."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "mcp__synapse__query" in text
    assert "mcp__synapse__contribute" in text
    assert "attach(" not in text


# ---------------------------------------------------------------------------
# The hook wiring.
# ---------------------------------------------------------------------------

def test_settings_snippet_is_valid_json_wiring_userpromptsubmit_to_the_hook():
    data = json.loads(SETTINGS_SNIPPET.read_text(encoding="utf-8"))
    prompt_hooks = data["hooks"]["UserPromptSubmit"]
    assert isinstance(prompt_hooks, list) and prompt_hooks

    commands = [
        entry["command"]
        for group in prompt_hooks
        for entry in group["hooks"]
        if entry.get("type") == "command"
    ]
    assert any("freshness_pointer.py" in command for command in commands)
    # Portable across install locations -- an install doc that told someone
    # to hardcode a path would break the moment the repo moves.
    assert any("$CLAUDE_PROJECT_DIR" in command for command in commands)


# ---------------------------------------------------------------------------
# The pack is a shipped artifact, never installed into this repo's own
# .claude/ (Chain C's own scope line).
# ---------------------------------------------------------------------------

def test_install_md_exists_and_documents_where_the_pack_is_copied_to():
    text = INSTALL_MD.read_text(encoding="utf-8")
    assert ".claude" in text
    assert "settings.json" in text


def test_install_md_qualifies_the_running_claude_code_window_as_agent_session():
    """CONTEXT.md: Agent Session's `_Avoid_` list is 'session (unqualified),
    chat, conversation, local session.' INSTALL.md's restart step and its
    Troubleshooting section both talked about 'a session'/'whichever
    session' where the thing meant is precisely what CONTEXT.md defines as
    an Agent Session (a single running Claude Code conversation, identified
    by its own id) -- the same defect the hook's injected line had for
    Shared Session, on the sibling term, in the doc a teammate reads before
    ever seeing the hook speak."""
    text = INSTALL_MD.read_text(encoding="utf-8")
    assert "(or start a new Agent Session)" in text
    assert "(or start a new session)" not in text
    assert "*whichever* Claude Code Agent Session it finds live" in text
    assert "*whichever* Claude Code session it finds live" not in text
    assert "whichever Agent Session is live in *that*" in text
    assert "whichever session is live in *that*" not in text


def test_this_repos_own_claude_dir_is_not_where_the_pack_self_installed():
    """Chain C: 'The pack is a shipped artifact, not installed into this
    repo's own `.claude/` — installing it on a dev machine is INSTALL.md's
    job.' A stray `.claude/` here would mean this task installed itself
    into its own repo rather than shipping something a teammate installs
    elsewhere.

    Deliberately NOT `assert not (ROOT / ".claude").exists()`: that pins a
    property of the developer's *working tree*, not of the shipped
    artifact. A teammate who approves any Claude Code permission in this
    repo gets a `.claude/settings.local.json` — real, gitignored by the
    user's *global* gitignore (`~/.config/git/ignore:
    **/.claude/settings.local.json`), so `git status` clean is not evidence
    it is absent, and it has nothing to do with whether this task
    self-installed. Instead pin the two things the scope line actually
    means: none of the paths INSTALL.md's own `mkdir -p` step would create
    exist here, and nothing under `.claude/` is tracked by this repo's git
    -- which is what "not installed into this repo" actually cashes out
    to."""
    claude_dir = ROOT / ".claude"
    # The exact paths `INSTALL.md`'s `mkdir -p .claude/skills
    # .claude/synapse-pack` step creates on a teammate's machine.
    assert not (claude_dir / "skills" / "synapse-shared-memory").exists()
    assert not (claude_dir / "synapse-pack").exists()

    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", ".claude"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert tracked.strip() == "", (
        f"unexpected files under .claude/ tracked by this repo's git: {tracked!r}")


def test_the_hook_file_the_snippet_and_install_doc_point_at_actually_exists():
    assert HOOK.is_file()
