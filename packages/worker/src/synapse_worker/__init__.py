"""Edge worker: follow a transcript, condense the delta on-device, push upstream."""

from synapse_worker.follower import FollowState, TranscriptFollower
from synapse_worker.loop import TickResult, WorkerLoop
from synapse_worker.producer import FileSink, FindingSink, HttpSink, Producer
from synapse_worker.segmenter import Segmenter, estimate_tokens, is_turn_boundary
from synapse_worker.sources.claude_code import ClaudeCodeSource

__all__ = [
    "ClaudeCodeSource",
    "FileSink",
    "FindingSink",
    "FollowState",
    "HttpSink",
    "Producer",
    "Segmenter",
    "TickResult",
    "TranscriptFollower",
    "WorkerLoop",
    "estimate_tokens",
    "is_turn_boundary",
]
