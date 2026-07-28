"""Adapter: chart-review-platform NER output -> partial ExtractionRecords.

Written against the *dummy* fixtures in fixtures/chart_review_output/, which
stand in for github.com/yuhangjiang22/chart-review-platform until we have real
output files.  Everything format-specific lives in this file on purpose: when
Yuhang shares real output, only this adapter changes.

Native shape assumed:
    {
      "pipeline": "chart-review-platform", "version": "...", "model": "...",
      "note_id": 1001, "patient_id": 1, "note_datetime": "...",
      "entities": [
        {"entity_id": "...", "text": "MMSE", "label": "...",
         "span": {"start": 412, "end": 416}, "quote": "...",
         "assertion": "present", "temporality": "current",
         "value": "22", "value_unit": null, "confidence": 0.94}
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

# How an NER assertion maps to NOTE_NLP.term_exists.  Anything mapping to False
# is kept as evidence in NOTE_NLP but never written to a domain table.
#
# OPEN POLICY QUESTION for the group: "planned" currently counts as existing, so
# "start donepezil" produces a DRUG_EXPOSURE row.  That may be wrong — a planned
# medication is not an exposure.  Flip it here once the group decides; the
# assertion is preserved in term_modifiers either way, so nothing is lost.
ASSERTION_TO_TERM_EXISTS: dict[str, bool] = {
    "present": True,
    "positive": True,
    "confirmed": True,
    "planned": True,
    "possible": True,
    "uncertain": True,
    "absent": False,
    "negated": False,
    "denied": False,
    "hypothetical": False,
    "family_history": False,
}


def read_entities(path: Path) -> Iterator[dict[str, Any]]:
    """Yield partial ExtractionRecord dicts (no concept_id yet)."""
    for file in _json_files(path):
        doc = json.loads(file.read_text())
        pipeline = doc.get("pipeline", "chart-review-platform")
        version = doc.get("version")
        model = doc.get("model")
        note_id = doc.get("note_id")
        person_id = doc.get("patient_id", doc.get("person_id"))
        note_datetime = doc.get("note_datetime")
        if note_id is None or person_id is None:
            raise ValueError(f"{file}: missing note_id / patient_id")

        for entity in doc.get("entities", []):
            span = entity.get("span") or {}
            if "start" not in span and isinstance(entity.get("span_offsets"), list):
                # Rubric-answer evidence uses span_offsets: [start, end].
                start_end = entity["span_offsets"]
                span = {"start": start_end[0], "end": start_end[1]}
            if "start" not in span or "end" not in span:
                raise ValueError(
                    f"{file}: entity {entity.get('entity_id')} has no character span — "
                    f"spans are required to join with the normalizer and to fill "
                    f"NOTE_NLP.offset"
                )
            assertion = (entity.get("assertion") or "present").lower()
            if assertion not in ASSERTION_TO_TERM_EXISTS:
                raise ValueError(
                    f"{file}: unknown assertion {assertion!r} — add it to "
                    f"ASSERTION_TO_TERM_EXISTS rather than guessing"
                )

            yield {
                "record_id": f"cr:{note_id}:{span['start']}-{span['end']}",
                # Passed through with its type intact: the platform emits string
                # note ids like "2024-12-04__pathology_report", which the writer
                # resolves via NOTE.note_source_value.  Do not coerce to int here
                # — that would turn a source value into a bogus CDM key.
                "note_id": note_id,
                "person_id": person_id,
                # "omop"-sourced evidence cites a structured row the extractor
                # read; the writer refuses to insert those.
                "evidence_source": entity.get("source", "note"),
                "span": {"start": int(span["start"]), "end": int(span["end"])},
                "lexical_variant": entity.get("text"),
                "snippet": entity.get("quote"),
                "note_datetime": note_datetime,
                "term_exists": ASSERTION_TO_TERM_EXISTS[assertion],
                "term_temporal": entity.get("temporality"),
                "term_modifiers": _modifiers(entity, assertion),
                "value": _value(entity),
                "source": {"pipeline": pipeline, "version": version, "model": model},
            }


def _modifiers(entity: dict[str, Any], assertion: str) -> str:
    """NOTE_NLP.term_modifiers is free text; keep it parseable as key=value."""
    parts = [f"assertion={assertion}"]
    if entity.get("label"):
        parts.append(f"label={entity['label']}")
    if entity.get("confidence") is not None:
        parts.append(f"ner_confidence={entity['confidence']}")
    return ",".join(parts)


def _value(entity: dict[str, Any]) -> dict[str, Any]:
    """Split the extracted value into OMOP's numeric/string slots.

    The unit stays in unit_source_value: mapping "%" to unit_concept_id 8554
    requires the vocabulary and is the normalizer's job, not ours.  Leaving it
    unmapped is visible; guessing it would not be.
    """
    raw = entity.get("value")
    value: dict[str, Any] = {
        "as_number": None,
        "as_string": None,
        "unit_source_value": entity.get("value_unit"),
    }
    if raw is None or raw == "":
        return value
    try:
        value["as_number"] = float(raw)
    except (TypeError, ValueError):
        value["as_string"] = str(raw)
    return value


def _json_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.glob("*.json"))
    return [path]
