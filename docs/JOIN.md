# Joining the team's Synapse — start here

Ten minutes, five steps. You end up with your coding agent able to ask what
the team already learned, and able to put what *you* learn back.

**What you are joining.** One person (currently Siddsing) hosts the **Synapse
Service** on the LAN — that is the shared memory. Everyone else runs their own
**orchestrator** locally. That split is not a preference: the orchestrator
stamps your identity onto everything it sends, so if you point your agent at
someone else's, your findings get credited to them and hidden from you.

Ask the host for two things before you start: their **service URL** (looks
like `http://192.168.4.44:8899`) and the current **shared-id** (looks like
`sh-bbe76a56` — it changes when they restart, because the store is in memory).

---

## 1. Get the code running

```bash
git clone https://github.com/SinghSiddharth01/Synapse.git && cd Synapse
uv sync
uv run pytest -q          # expect all green — if not, stop and say so
```

**On Windows/ARM64 (the X Elite box), pin the interpreter or you get an
x86 one under emulation and a Rust build error:**

```powershell
uv sync --python "$env:LOCALAPPDATA\Programs\Python\Python312-arm64\python.exe"
```

Do not "upgrade" `mcp` off `1.9.4` — newer pulls `cryptography`, which has no
ARM64-Windows wheel.

## 2. Start your half, pointed at the host

```bash
uv run python scripts/serve_local.py \
  --service-url http://<host-ip>:8899 \
  --shared-id <shared-id> \
  --contributor <your-name>
```

This starts only what belongs to you: a model seam and your own orchestrator
on `127.0.0.1:8787`. It does **not** start a service — you are using theirs.

Add `--npu` if you have `geniex serve` running. Otherwise a stand-in answers
locally: fine for wiring, but it only knows this repo's fixture corpus, so
your own words will not distil into findings. Ask the host for `--live` (a
real model behind the seam) when you want to test that for real.

## 3. Connect Claude Code

From whatever project you want shared memory in:

```bash
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

Then start a **new** Claude Code session and **approve** the server when it
asks. `claude mcp list` should say `✔ Connected  synapse`.

Note the URL is your **own** localhost, not the host's machine. See the rule
at the bottom.

## 4. Install the awareness pack

Without this your agent has the tools but rarely reaches for them on its own —
another loaded skill will simply take over.

```bash
mkdir -p ~/.claude/skills
cp -r packs/claude-code/skills/synapse-shared-memory ~/.claude/skills/
```

User scope, not per-project, so it applies wherever you work. Restart Claude
Code. (`packs/claude-code/INSTALL.md` also has the freshness hook if you want
signal ③.)

## 5. Check it works

In a new session, ask something the team would know without naming the tool —
for example *"has anyone hit Cirrascale rejecting an API key?"* You should see
it call `synapse` and answer with a teammate's name attached.

Then push something back:

> "Contribute this to team memory: <a real thing you just learned>"

You should get `N finding(s) shared with the team.` Watch it land on the host's
dashboard at `http://<host-ip>:8899/debug`.

---

## Two rules

**Never point Claude Code at the host's `:8787`.** One orchestrator per laptop.
Theirs stamps their binding — their contributor, their agent session — so your
findings would be credited to them and suppressed from you, which is the
opposite of what this system is for. Your MCP URL is always `127.0.0.1:8787`.

**Never commit `secrets.jsonc` or `api-1.json`.** They are gitignored. Do not
paste keys into a chat with an agent either — anything in a transcript is in
that transcript for good, and we are already rotating keys after the 7th for
exactly that reason.

## When something looks broken

| symptom | cause |
|---|---|
| `/mcp` says failed, "unable to connect" | your orchestrator isn't running — step 2 |
| both tools say "not joined" | no binding; re-run step 2 with `--shared-id` |
| `contribute` says "nothing durable extracted" | the stand-in only knows the fixture corpus — you need `--live` or an NPU |
| queries return nothing at all | the host restarted; memory is in-process, so it is genuinely empty |
| agent has the tools but never uses them | the pack from step 4 isn't installed |

Full detail: [`packs/claude-code/INSTALL.md`](../packs/claude-code/INSTALL.md).
On-hardware NPU work: [`docs/NPU-RUNBOOK.md`](./NPU-RUNBOOK.md).
