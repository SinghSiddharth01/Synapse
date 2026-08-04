"""Transcript adapters. One per agent product; everything downstream is agent-blind."""

from synapse_worker.sources.claude_code import ClaudeCodeSource

__all__ = ["ClaudeCodeSource"]
