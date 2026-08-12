"""Adapter: ACTS rubric output -> ExtractionRecords.

ACTS is a chart-review rubric rather than a span extractor: it answers 29 typed
questions about a patient ("what is the documented MMSE total score?") and cites
the note text each answer came from.  So the shape differs from NER output in two
ways that matter:

  * The CONCEPT comes from the field, not from the text.  `mmse_score` identifies
    the concept; the answer `22` is the value.  A reviewed field->concept table in
    concept-normalizer supplies the concept id.
  * The ANSWER has a type, and the type decides which OMOP value column it lands
    in.  Getting this generic would quietly put a date in a numeric column or a
    negative finding in the wrong place, so each answer type is handled
    explicitly.

Native shape assumed:

    {
      "task_id": "acts", "note_id": "...", "person_id": ...,
      "answers": [
        {"field_id": "mmse_score", "answer": 22, "confidence": "high",
         "evidence": [{"source": "note", "note_id": "...",
                       "span_offsets": [130, 145],
                       "verbatim_quote": "MMSE 22/30 today"}]}
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

# Fields the rubric COMPUTES from another field.  Inserting them would record the
# same fact twice — mmse_severity carries nothing mmse_score does not.
DERIVED_FIELDS = frozenset({
    "apoe2", "apoe3", "apoe4",                       # from apoe_genotype
    "mmse_severity", "moca_severity", "cdr_severity",  # from their scores
    "vaccine_category",                              # from vaccine_name
})

# Answers that are a number, including the numeric-valued enums (CDR global is
# 0/0.5/1/2/3; GDS stage is 1-7) — both are scores, not categories.
NUMERIC_FIELDS = frozenset({
    "mmse_score", "moca_score", "mattis_drs", "tics_score", "hachinski_score",
    "gds_depression_score", "cornell_csdd", "npi_total",
    "education_years", "pack_year", "pack_per_day", "smoking_duration",
    "cdr_global", "gds_stage",
})

# Answers that assert presence or absence.  A `0` here is a NEGATIVE finding — the
# note says the patient is not impaired — so it must become term_exists=false and
# never a positive clinical fact.  This is the case most likely to be got wrong by
# generic handling, and the consequence is a fabricated diagnosis.
BOOLEAN_FIELDS = frozenset({"impaired_cognition", "postmenopause"})

# Answers that are a category. The value needs its own concept (e.g. "former"
# smoker) which the field-level mapping does not supply, so it is carried as a
# string until an answer-level mapping exists — visible rather than guessed.
CATEGORICAL_FIELDS = frozenset({"smoking_status", "apoe_genotype"})

# Answers that are a date or time expression. Not a measurement value: OMOP has no
# date-valued column, so these need a modelling decision (event date vs a string
# value) and are carried as strings meanwhile.
DATE_FIELDS = frozenset({"lmp_date", "quit_time"})

# Answers that are lists. Each element is a separate clinical fact needing its own
# concept ("allergy to penicillin"), which the field-level mapping cannot express.
LIST_FIELDS = frozenset({"allergen", "vaccine_name"})

# ACTS confidence is categorical, unlike the normalizer's numeric score.
CONFIDENCE = {"high": 0.95, "medium": 0.7, "low": 0.4}

TRUTHY = {"1", "true", "yes", "y", "present"}
FALSY = {"0", "false", "no", "n", "absent"}


class ActsFormatError(ValueError):
    """The input does not look like ACTS rubric output."""


def read_answers(path: Path) -> Iterator[dict[str, Any]]:
    """Yield partial ExtractionRecord dicts — one per (answer, evidence) pair.

    An answer with several evidence citations becomes several records: each cites
    a different span, and NOTE_NLP is one row per span.  They share a concept, so
    the writer's ledger keys them apart by span.
    """
    for file in _json_files(path):
        doc = json.loads(file.read_text())
        answers = doc.get("answers")
        if answers is None:
            raise ActsFormatError(
                f"{file}: no 'answers' key — is this ACTS rubric output? NER span "
                f"output goes through adapters/chart_review.py instead."
            )
        pipeline = doc.get("pipeline", "chart-review-platform")
        version = doc.get("version")
        model = doc.get("model")
        doc_note_id = doc.get("note_id")
        doc_person_id = doc.get("person_id", doc.get("patient_id"))

        for answer in answers:
            field_id = answer.get("field_id")
            if not field_id:
                raise ActsFormatError(f"{file}: an answer has no field_id")
            if field_id in DERIVED_FIELDS:
                continue
            raw = answer.get("answer")
            if raw is None or raw == "":
                continue  # not documented in this chart

            evidence = [
                e for e in (answer.get("evidence") or [])
                if (e.get("source") or "note") == "note"
            ]
            if not evidence:
                # No note-derived citation: either unevidenced, or cited only from
                # structured data. Neither is insertable, and the writer's own
                # guard would reject the latter anyway.
                continue

            for ev in evidence:
                note_id = ev.get("note_id", doc_note_id)
                person_id = ev.get("person_id", doc_person_id)
                if note_id is None or person_id is None:
                    raise ActsFormatError(
                        f"{file}: {field_id} evidence has no note_id/person_id"
                    )
                span = _span(ev)
                value, term_exists, notes = _typed_value(field_id, raw)

                yield {
                    "record_id": f"acts:{note_id}:{field_id}:{span['start']}-{span['end']}",
                    "note_id": note_id,
                    "person_id": person_id,
                    "span": span,
                    # The concept comes from the field; the quote is the evidence.
                    "lexical_variant": ev.get("verbatim_quote") or field_id,
                    "snippet": ev.get("verbatim_quote"),
                    "note_datetime": doc.get("note_datetime") or ev.get("note_datetime"),
                    "evidence_source": "note",
                    "term_exists": term_exists,
                    "term_temporal": answer.get("temporality"),
                    "term_modifiers": _modifiers(field_id, raw, answer, notes),
                    "value": value,
                    "concept_confidence": CONFIDENCE.get(
                        str(answer.get("confidence", "")).lower()
                    ),
                    # The normalizer resolves this against the reviewed ACTS table.
                    "source_term": field_id,
                    "acts_field_id": field_id,
                    "source": {"pipeline": pipeline, "version": version, "model": model},
                }


def _span(ev: dict[str, Any]) -> dict[str, int]:
    offsets = ev.get("span_offsets") or ev.get("offsets")
    if isinstance(offsets, list) and len(offsets) >= 2:
        return {"start": int(offsets[0]), "end": int(offsets[1])}
    span = ev.get("span") or {}
    if "start" in span and "end" in span:
        return {"start": int(span["start"]), "end": int(span["end"])}
    raise ActsFormatError(
        "evidence has no span_offsets — spans are required for NOTE_NLP.offset and "
        "to key records apart when one field is evidenced several times"
    )


def _typed_value(field_id: str, raw: Any) -> tuple[dict[str, Any], bool, str]:
    """Route the answer to the right OMOP value slot for its type.

    Returns (value dict, term_exists, note). `term_exists=False` means the note
    asserted ABSENCE, which the writer keeps as evidence and never as a fact.
    """
    empty: dict[str, Any] = {}

    if field_id in BOOLEAN_FIELDS:
        s = str(raw).strip().lower()
        if s in FALSY:
            # "impaired_cognition = 0" means NOT impaired. Recording it as a
            # positive finding would fabricate a diagnosis.
            return empty, False, "negative finding"
        if s in TRUTHY:
            return empty, True, ""
        return empty, True, f"unrecognised boolean {raw!r} — treated as present"

    if field_id in NUMERIC_FIELDS:
        try:
            return {"as_number": float(raw)}, True, ""
        except (TypeError, ValueError):
            # A non-numeric answer in a numeric field is a data problem, not
            # something to coerce. Keep it visible as a string.
            return ({"as_string": str(raw)}, True,
                    f"expected a number, got {raw!r}")

    if field_id in DATE_FIELDS:
        return ({"as_string": str(raw)}, True,
                "date-valued answer; OMOP has no date value column — modelling "
                "decision pending")

    if field_id in CATEGORICAL_FIELDS:
        return ({"as_string": str(raw)}, True,
                "categorical answer; needs an answer-level concept mapping for "
                "value_as_concept_id")

    if field_id in LIST_FIELDS:
        return ({"as_string": ", ".join(map(str, raw)) if isinstance(raw, list)
                 else str(raw)}, True,
                "list answer; each element needs its own concept")

    # Unknown field: carry the answer rather than dropping it, and say so.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"as_number": float(raw)}, True, f"unknown field {field_id!r}"
    return {"as_string": str(raw)}, True, f"unknown field {field_id!r}"


def _modifiers(field_id: str, raw: Any, answer: dict[str, Any], note: str) -> str:
    parts = [f"acts_field={field_id}", f"answer={raw}"]
    if answer.get("confidence"):
        parts.append(f"confidence={answer['confidence']}")
    if note:
        parts.append(f"note={note}")
    return ",".join(parts)


def _json_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        if not files:
            raise ActsFormatError(f"no .json files in {path}")
        return files
    if not path.exists():
        raise ActsFormatError(f"path does not exist: {path}")
    return [path]
