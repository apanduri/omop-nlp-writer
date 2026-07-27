"""Adapter: normalizer output -> concept_id, keyed for joining to NER mentions.

Stands in for Xuguang's normalization pipeline.  Native shape assumed:

    {"pipeline": "bso-normalizer", "version": "0.2.0",
     "mentions": [
       {"mention_id": "...", "note_id": 1001, "span": {"start": 412, "end": 416},
        "text": "MMSE",
        "candidates": [{"concept_id": 42869861, "concept_name": "...",
                        "vocabulary_id": "LOINC", "score": 0.97}]}
     ]}

The join key is (note_id, span.start, span.end) — see CONTRACT.md open question
#2.  If the real normalizer works on bare strings with no offsets, this join
becomes ambiguous for any term appearing twice in a note, and we need Xuguang to
echo the span back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

JoinKey = tuple[int, int, int]


class Normalization(dict):
    """A resolved mapping: concept_id + score + provenance."""

    @property
    def concept_id(self) -> int | None:
        return self.get("concept_id")

    @property
    def score(self) -> float | None:
        return self.get("score")


def read_normalizations(path: Path) -> dict[JoinKey, Normalization]:
    """Index normalizer output by (note_id, start, end), best candidate first."""
    index: dict[JoinKey, Normalization] = {}
    for file in _json_files(path):
        doc = json.loads(file.read_text())
        pipeline = doc.get("pipeline", "normalizer")
        version = doc.get("version")
        for mention in doc.get("mentions", []):
            note_id = mention.get("note_id")
            span = mention.get("span") or {}
            if note_id is None or "start" not in span or "end" not in span:
                raise ValueError(
                    f"{file}: mention {mention.get('mention_id')} lacks note_id/span — "
                    f"cannot be joined to an NER mention"
                )
            key: JoinKey = (int(note_id), int(span["start"]), int(span["end"]))
            best = _best_candidate(mention.get("candidates") or [])
            index[key] = Normalization(
                concept_id=best.get("concept_id") if best else None,
                concept_name=best.get("concept_name") if best else None,
                vocabulary_id=best.get("vocabulary_id") if best else None,
                score=best.get("score") if best else None,
                candidate_count=len(mention.get("candidates") or []),
                normalizer=f"{pipeline}@{version}" if version else pipeline,
            )
    return index


def _best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Highest-scoring candidate wins; ties keep the pipeline's own ordering.

    We take exactly one.  Writing multiple candidate concepts into a domain table
    would fabricate clinical facts the note does not support.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.get("score") is not None, c.get("score") or 0.0))


def _json_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob("*.json"))
    return [path]
