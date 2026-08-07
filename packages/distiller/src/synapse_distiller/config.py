"""Configuration: model choice, prompt pack, and segment budget.

Resolution order, lowest to highest: built-in defaults → config/synapse.toml →
environment variables. Env override exists so a bake-off can sweep models or
prompt packs across runs without editing a tracked file.

The budget is *resolved*, never read. `SynapseConfig.segment_budget` runs the
capability record and the prompt pack's derived overhead through the same
validation every time, so an unusable combination fails at startup rather than
producing truncated prompts at call time.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from synapse_distiller.capability import (
    BUILTIN_RECORDS,
    CapabilityError,
    CapabilityRecord,
)
from synapse_distiller.prompt import (
    DEFAULT_KINDS,
    DEFAULT_RENDER_STYLE,
    VALID_KINDS,
    VALID_RENDER_STYLES,
)
from synapse_distiller.promptpack import PromptPack, load_pack_by_name

ENV_PREFIX = "SYNAPSE_"


class ConfigError(ValueError):
    """A configuration that would not produce a usable distiller."""


def _parse_kinds(raw: object) -> tuple[str, ...]:
    """Accept a TOML list or a comma-separated env string, and validate.

    Validated eagerly because a typo'd kind is otherwise invisible: it would
    silently narrow the input rather than raise, and a distiller quietly fed
    less than intended is exactly the failure this whole layer exists to avoid.
    """
    if raw is None:
        return DEFAULT_KINDS
    kinds = (
        tuple(k.strip() for k in raw.split(",") if k.strip())
        if isinstance(raw, str)
        else tuple(str(k).strip() for k in raw)
    )
    if not kinds:
        raise ConfigError(
            "distil_kinds is empty; the distiller would receive no segment "
            f"content at all. Valid kinds: {sorted(VALID_KINDS)}."
        )
    unknown = [k for k in kinds if k not in VALID_KINDS]
    if unknown:
        raise ConfigError(
            f"Unknown distil_kinds {unknown}. Valid kinds are "
            f"{sorted(VALID_KINDS)} — these mirror AgentEvent.kind."
        )
    return kinds


def _parse_render_style(raw: object) -> str:
    if raw is None:
        return DEFAULT_RENDER_STYLE
    style = str(raw).strip()
    if style not in VALID_RENDER_STYLES:
        raise ConfigError(
            f"Unknown render_style {style!r}. Valid: {sorted(VALID_RENDER_STYLES)}. "
            f"'content' strips role labels and is only coherent when distil_kinds "
            f"is homogeneous; 'labelled' is required once tool events are included, "
            f"since the tool name carries which step failed."
        )
    return style


@dataclass(frozen=True)
class WorkerConfig:
    """The periodic capture loop.

    poll_interval_seconds is cheap to lower: a tick with no new bytes costs one
    stat call, because the follower gates on size before opening anything. The
    real cost is distillation, which only runs when a turn has actually closed.

    idle_flush_seconds exists because a turn is only known to be complete once
    the next one starts. Without it, the final turn of a conversation would sit
    in the buffer forever after the developer stops typing.
    """

    poll_interval_seconds: float = 30.0
    idle_flush_seconds: float = 120.0
    state_dir: str = ".synapse"
    sink: str = "file"  # "file" | "http"
    upstream_url: str = "http://127.0.0.1:8787/producer/findings"
    sink_file: str = ".synapse/upstream.jsonl"
    attach_at_end: bool = True
    # The producer endpoint flushes to the Service before it answers, and that
    # flush includes synthesis — a model call on the HOST's machine. 30s was
    # short enough that a normal push timed out on the first try on every batch
    # and only landed on the write-ahead log's retry. Nothing was lost, but each
    # batch paid the full timeout first. This is the cheap half of the fix; the
    # other half is the endpoint not flushing synchronously at all.
    upstream_timeout_s: float = 120.0

    # The WORKER -> PROVIDER seam's three bounds. Rationale for each number is
    # in synapse_worker.limiter (which owns the defaults; these mirror them so
    # a config file is a complete description of the runtime). Nothing bounded
    # this seam before 2026-08-06 -- one tick distilled every drained segment
    # back to back, and the in-flight count was one only because `tick()`
    # happens to await in sequence.
    max_calls_per_tick: int = 4
    max_concurrent_calls: int = 1
    max_deferred_segments: int = 64


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = "http://127.0.0.1:18181/v1"
    max_tokens: int = 900
    temperature: float = 0.0
    timeout_s: float = 300.0


@dataclass(frozen=True)
class SynapseConfig:
    model: str
    prompt_pack_name: str
    max_seconds_per_call: float
    provider: ProviderConfig
    records: dict[str, CapabilityRecord]
    distil_kinds: tuple[str, ...] = DEFAULT_KINDS
    render_style: str = DEFAULT_RENDER_STYLE
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    segment_budget_override: int | None = None
    source_path: Path | None = None

    @property
    def prompt_pack(self) -> PromptPack:
        return load_pack_by_name(self.prompt_pack_name)

    @property
    def record(self) -> CapabilityRecord:
        try:
            return self.records[self.model]
        except KeyError:
            known = ", ".join(sorted(self.records)) or "(none)"
            raise CapabilityError(
                f"No capability record for model {self.model!r}. Add a "
                f"[capability.\"{self.model}\"] block to config/synapse.toml with "
                f"MEASURED values. Known models: {known}."
            ) from None

    @property
    def effective_max_tokens(self) -> int:
        """`provider.max_tokens`, capped at what the budget actually left room for.

        `segment_budget` is derived as `usable_context - overhead -
        response_reserve`, which makes the reserve a promise: the segmenter
        packed prompts on the assumption that no more than that many output
        tokens would be requested. Asking for more overruns the context on
        precisely the segments that used their whole budget.

        Clamped rather than raised. A CapabilityError here would refuse to start
        on every config that already carries the (widespread) 900, and the
        correct behaviour is available without failing: honour the reserve. The
        caller logs when the clamp bites, so it is visible rather than silent.
        """
        return min(self.provider.max_tokens, self.record.response_reserve)

    @property
    def segment_budget(self) -> int:
        """Resolve the budget from the record, the pack, and any override."""
        return self.record.segment_budget(
            self.prompt_pack.overhead_tokens,
            max_seconds_per_call=self.max_seconds_per_call,
            override=self.segment_budget_override,
        )

    def describe(self) -> str:
        """One block naming every input to the budget. Print it with any result —
        a number without its prompt pack and model cannot be compared later."""
        pack = self.prompt_pack
        calibration = (
            f"calibrated against {pack.calibrated_against}"
            if pack.is_calibrated
            else "ESTIMATED (run scripts/calibrate_prompt.py)"
        )
        return (
            f"model            {self.model}\n"
            f"prompt_pack      {pack.name}  ({calibration})\n"
            f"distil_kinds     {', '.join(self.distil_kinds)}  (render: {self.render_style})\n"
            f"usable_context   {self.record.usable_context}\n"
            f"prompt_overhead  {pack.overhead_tokens}\n"
            f"response_reserve {self.record.response_reserve}\n"
            f"segment_budget   {self.segment_budget}"
            + (" (overridden)" if self.segment_budget_override else " (derived)")
        )


@lru_cache(maxsize=1)
def default_config_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "synapse.toml"
        if candidate.is_file():
            return candidate
    # Installed from a wheel there is no repo above this file to walk up to,
    # so the wheel carries a copy of config/synapse.toml as package data (the
    # release build force-includes it — see packages/distiller/pyproject.toml).
    # The walk-up stays first so a checkout's live-edited config always wins
    # over the frozen copy in an editable install.
    packaged = Path(__file__).resolve().parent / "data" / "synapse.toml"
    if packaged.is_file():
        return packaged
    return None


def _env(name: str) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}")


def load_config(path: str | Path | None = None) -> SynapseConfig:
    resolved = Path(path) if path is not None else default_config_path()
    raw: dict = {}
    if resolved is not None and resolved.is_file():
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))

    distiller = raw.get("distiller", {})
    provider_raw = raw.get("provider", {})
    worker_raw = raw.get("worker", {})

    records = dict(BUILTIN_RECORDS)
    for model, block in raw.get("capability", {}).items():
        records[model] = CapabilityRecord(
            model=model,
            usable_context=int(block["usable_context"]),
            prefill_toks_per_sec=float(block["prefill_toks_per_sec"]),
            response_reserve=int(block["response_reserve"]),
            notes=block.get("notes", ""),
        )

    override_raw = _env("SEGMENT_BUDGET") or distiller.get("segment_budget")

    return SynapseConfig(
        model=_env("MODEL") or distiller.get("model") or next(iter(records)),
        prompt_pack_name=_env("PROMPT_PACK") or distiller.get("prompt_pack", "v2-hardened"),
        max_seconds_per_call=float(
            _env("MAX_SECONDS_PER_CALL") or distiller.get("max_seconds_per_call", 30.0)
        ),
        provider=ProviderConfig(
            base_url=_env("BASE_URL") or provider_raw.get("base_url", ProviderConfig.base_url),
            max_tokens=int(_env("MAX_TOKENS") or provider_raw.get("max_tokens", 900)),
            temperature=float(_env("TEMPERATURE") or provider_raw.get("temperature", 0.0)),
            timeout_s=float(_env("TIMEOUT_S") or provider_raw.get("timeout_s", 300.0)),
        ),
        records=records,
        worker=WorkerConfig(
            poll_interval_seconds=float(
                _env("POLL_INTERVAL") or worker_raw.get("poll_interval_seconds", 30.0)
            ),
            idle_flush_seconds=float(
                _env("IDLE_FLUSH") or worker_raw.get("idle_flush_seconds", 120.0)
            ),
            state_dir=_env("STATE_DIR") or worker_raw.get("state_dir", ".synapse"),
            sink=_env("SINK") or worker_raw.get("sink", "file"),
            upstream_url=_env("UPSTREAM_URL")
            or worker_raw.get("upstream_url", WorkerConfig.upstream_url),
            sink_file=_env("SINK_FILE")
            or worker_raw.get("sink_file", WorkerConfig.sink_file),
            attach_at_end=bool(worker_raw.get("attach_at_end", True)),
            upstream_timeout_s=float(
                _env("UPSTREAM_TIMEOUT")
                or worker_raw.get("upstream_timeout_s", WorkerConfig.upstream_timeout_s)
            ),
            max_calls_per_tick=int(
                _env("MAX_CALLS_PER_TICK")
                or worker_raw.get("max_calls_per_tick", WorkerConfig.max_calls_per_tick)
            ),
            max_concurrent_calls=int(
                _env("MAX_CONCURRENT_CALLS")
                or worker_raw.get("max_concurrent_calls",
                                  WorkerConfig.max_concurrent_calls)
            ),
            max_deferred_segments=int(
                _env("MAX_DEFERRED_SEGMENTS")
                or worker_raw.get("max_deferred_segments",
                                  WorkerConfig.max_deferred_segments)
            ),
        ),
        distil_kinds=_parse_kinds(_env("DISTIL_KINDS") or distiller.get("distil_kinds")),
        render_style=_parse_render_style(
            _env("RENDER_STYLE") or distiller.get("render_style")
        ),
        segment_budget_override=int(override_raw) if override_raw else None,
        source_path=resolved,
    )
