"""Transcript adapters. One per agent product; everything downstream is agent-blind."""

from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sources.codex import CodexSource

__all__ = ["ClaudeCodeSource", "CodexSource"]
