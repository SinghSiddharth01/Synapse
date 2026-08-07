"""Prompt packs — the distiller prompt as swappable, versioned data.

Plan B Task B.2 says the prompt is explicitly first-pass and will change
mid-week, and that the eval loop is how. A prompt frozen into a module constant
cannot be A/B'd without a code change, so it lives in TOML instead.

Two properties this file exists to guarantee:

  1. **Overhead is derived, never configured.** The segment budget is
     `usable_context - prompt_overhead - response_reserve`. If overhead were a
     number someone typed next to the prompt, the first prompt edit would make
     it wrong, the budget would come out too large, and segments would be
     silently truncated at the context boundary — the same failure class as a
     dropped prompt, just partial. So a pack computes its own overhead from its
     own text.

  2. **Every eval result names the pack that produced it.** Otherwise a tuning
     run cannot be attributed and the numbers are noise.

Token counting: `estimated_overhead_tokens` is a chars/4 approximation, which is
deliberately *conservative* (it over-counts for English prose, so the budget
comes out smaller rather than larger). `scripts/calibrate_prompt.py` replaces it
with a real count read back from the model's own tokenizer via `prompt_tokens`.
Until a pack is calibrated, `is_calibrated` is False and callers should say so
in any reported number.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Over-counting is safe (smaller budget); under-counting silently truncates.
# 3.5 chars/token errs toward over-counting for English prose.
_CHARS_PER_TOKEN = 3.5


class PromptPackError(ValueError):
    """A prompt pack that cannot be loaded or would produce unusable prompts."""


@dataclass(frozen=True)
class FewShot:
    input: str
    output: str


@dataclass(frozen=True)
class PromptPack:
    name: str
    description: str
    system: str
    few_shots: tuple[FewShot, ...]
    task_suffix: str = ""
    contaminated_fixtures: tuple[str, ...] = ()
    # Whether this pack asks the model to decide what is worth keeping. False
    # for compression-only packs, where that judgment lives upstream in triage
    # and downstream in synthesis. The eval harness reads it so an all-noise
    # fixture is not reported as a distiller failure under a pack that was never
    # asked to filter.
    judges_durability: bool = True
    calibrated_overhead_tokens: int | None = None
    calibrated_against: str = ""
    source_path: Path | None = field(default=None, compare=False)

    @property
    def is_calibrated(self) -> bool:
        return self.calibrated_overhead_tokens is not None

    @property
    def estimated_overhead_tokens(self) -> int:
        """Fixed prompt cost: system + every few-shot + the task suffix.

        Everything except the rendered Segment, which is what the budget is for.
        """
        chars = len(self.system) + len(self.task_suffix)
        for shot in self.few_shots:
            chars += len(shot.input) + len(shot.output)
        return int(chars / _CHARS_PER_TOKEN) + 1

    @property
    def overhead_tokens(self) -> int:
        """Measured overhead if calibrated, conservative estimate otherwise."""
        if self.calibrated_overhead_tokens is not None:
            return self.calibrated_overhead_tokens
        return self.estimated_overhead_tokens

    @property
    def example_finding_texts(self) -> tuple[str, ...]:
        """Every finding text this pack's own few-shots teach the model to emit.

        The corpus the parse-time echo guard measures against
        (`distiller.is_example_echo`). Derived from the few-shots rather than
        listed beside them on purpose: a new example added to the TOML is
        covered the moment it is added, and a hand-kept list would have gone
        stale at exactly the edit that most needs it. W7 F1 was six echoes of
        two few-shots that a hand-kept list would have had to anticipate.

        Tolerant of a few-shot whose output is not the findings shape at all —
        `load_pack` rejects non-JSON, but a pack built in a test or a future
        pack teaching a different schema must simply contribute nothing here
        rather than break loading.
        """
        texts: list[str] = []
        for shot in self.few_shots:
            try:
                payload = json.loads(shot.output)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            findings = payload.get("findings")
            if not isinstance(findings, list):
                continue
            for item in findings:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if text:
                    texts.append(text)
        return tuple(texts)

    def is_contaminated_for(self, fixture_id: str) -> bool:
        """Whether scoring this fixture with this pack produces a void number.

        A pack whose few-shots duplicate a fixture leaks the answer into the
        prompt. The eval harness must refuse to report such a score rather than
        print an inflated one.
        """
        return fixture_id in self.contaminated_fixtures


def load_pack(path: str | Path) -> PromptPack:
    path = Path(path)
    if not path.is_file():
        raise PromptPackError(f"Prompt pack not found: {path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PromptPackError(f"Prompt pack {path} is not valid TOML: {exc}") from exc

    for required in ("name", "system"):
        if not raw.get(required):
            raise PromptPackError(f"Prompt pack {path} is missing required key {required!r}")

    few_shots: list[FewShot] = []
    for index, shot in enumerate(raw.get("few_shots", [])):
        if "input" not in shot or "output" not in shot:
            raise PromptPackError(
                f"Prompt pack {path} few_shots[{index}] needs both 'input' and 'output'"
            )
        # The assistant side of a few-shot is what teaches the output shape. If
        # it is not valid JSON the pack is teaching the model to emit something
        # the parser will reject.
        try:
            json.loads(shot["output"])
        except json.JSONDecodeError as exc:
            raise PromptPackError(
                f"Prompt pack {path} few_shots[{index}].output is not valid JSON "
                f"({exc}). A few-shot that models malformed output trains the "
                f"model to produce it."
            ) from exc
        few_shots.append(
            FewShot(input=shot["input"].strip(), output=shot["output"].strip())
        )

    calibration = raw.get("calibration", {})
    overhead = calibration.get("overhead_tokens")
    if overhead is not None and overhead <= 0:
        # A zero or negative overhead reads as "calibrated" and inflates the
        # segment budget by the full size of the prompt, which truncates every
        # segment silently. Reject it rather than let a placeholder through.
        raise PromptPackError(
            f"Prompt pack {path} declares calibration.overhead_tokens = {overhead}. "
            f"A prompt cannot cost zero or fewer tokens; this would oversize the "
            f"segment budget and truncate every prompt. Remove the [calibration] "
            f"block to fall back to the estimate, or run scripts/calibrate_prompt.py."
        )

    return PromptPack(
        name=raw["name"],
        description=raw.get("description", ""),
        system=raw["system"].strip(),
        few_shots=tuple(few_shots),
        task_suffix=raw.get("task_suffix", ""),
        contaminated_fixtures=tuple(raw.get("contaminated_fixtures", [])),
        judges_durability=bool(raw.get("judges_durability", True)),
        calibrated_overhead_tokens=calibration.get("overhead_tokens"),
        calibrated_against=calibration.get("measured_against", ""),
        source_path=path,
    )


@lru_cache(maxsize=1)
def packs_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "prompts"
        if candidate.is_dir():
            return candidate
    # No repo above this file — installed from a wheel. The wheel carries
    # config/prompts/ as package data (force-included at build time; see
    # packages/distiller/pyproject.toml). Checkout wins when both exist.
    packaged = Path(__file__).resolve().parent / "data" / "prompts"
    if packaged.is_dir():
        return packaged
    raise PromptPackError(
        "Could not locate config/prompts/ above the package, and this "
        "install carries no packaged copy. Reinstall synapse-distiller from "
        "a release wheel, or run from a checkout.")


def load_pack_by_name(name: str) -> PromptPack:
    return load_pack(packs_dir() / f"{name}.toml")


def available_packs() -> list[str]:
    return sorted(p.stem for p in packs_dir().glob("*.toml"))
