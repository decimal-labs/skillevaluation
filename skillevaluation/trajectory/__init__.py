"""Canonical trajectory text rendering.

The trajectory format is the canonicalization step that LLM-judge interop
depends on — same role JCS-SHA256 plays for manifest hashing in AgentVersion.
Different runners must produce byte-equal output for the same input so
their judge scores are comparable.

See ``spec/trajectory-format.md`` for the canonical specification.
"""

from skillevaluation.trajectory.format_v1 import FORMAT_VERSION, build_transcript_v1

__all__ = ["build_transcript_v1", "FORMAT_VERSION"]
