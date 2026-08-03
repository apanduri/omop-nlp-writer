#!/usr/bin/env python3
"""Generate dummy outputs for BOTH upstream pipelines, plus synthetic notes.

Everything here is fabricated.  No real patient data — per Hongyu's instruction
to develop against synthetic data only.

Character offsets are computed from the note text at generation time rather than
hand-written, so the fixtures can never drift out of sync with the notes.

Emits:
  fixtures/notes/notes.json               -> seeds PERSON + NOTE in the CDM
  fixtures/chart_review_output/*.json     -> stand-in for Yuhang's NER output
  fixtures/normalizer_output/*.json       -> stand-in for Xuguang's normalizer
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

CHART_REVIEW_VERSION = "0.3.1"
CHART_REVIEW_MODEL = "claude-opus-4-5"
NORMALIZER_VERSION = "0.2.0"

# --------------------------------------------------------------------------
# Synthetic patients + notes
# --------------------------------------------------------------------------

PERSONS = [
    {"person_id": 1, "gender_concept_id": 8532, "year_of_birth": 1948, "person_source_value": "SYNTH-001"},
    {"person_id": 2, "gender_concept_id": 8507, "year_of_birth": 1941, "person_source_value": "SYNTH-002"},
]

NOTES = [
    {
        "note_id": 1001,
        "person_id": 1,
        "note_date": "2025-03-14",
        "note_datetime": "2025-03-14T09:20:00",
        "note_title": "Memory clinic follow-up",
        "note_text": (
            "SYNTHETIC NOTE - NOT REAL PATIENT DATA.\n"
            "Ms. A is a 77-year-old woman returning to the memory clinic with her daughter.\n"
            "Cognition: MMSE 22/30 today, down from 26 eighteen months ago. Clock drawing impaired.\n"
            "Assessment: findings are consistent with dementia, likely Alzheimer's type.\n"
            "Labs: HbA1c 7.8 % (up from 7.1). No evidence of delirium at this visit.\n"
            "Plan: start donepezil 5 mg nightly, recheck in 3 months.\n"
        ),
    },
    {
        "note_id": 1002,
        "person_id": 2,
        "note_date": "2025-04-02",
        "note_datetime": "2025-04-02T14:05:00",
        "note_title": "Geriatrics intake",
        "note_text": (
            "SYNTHETIC NOTE - NOT REAL PATIENT DATA.\n"
            "Mr. B, 84, brought in by his son after a mechanical fall at home last week.\n"
            "Cognitive screen: MMSE 29/30 - within normal limits for education level.\n"
            "There is no evidence of dementia on today's assessment.\n"
            "He was seen in an inpatient visit last year for pneumonia.\n"
            "Frailty noted; gait speed markedly reduced.\n"
        ),
    },
    {
        # Deliberately a STRING id, matching the chart-review platform's real
        # shape ("2024-12-04__pathology_report").  init-cdm allocates a CDM
        # integer key and records this in NOTE.note_source_value; extractions
        # arriving with the string resolve through that column.
        "note_id": "2025-05-02__memory_clinic",
        "person_id": 1,
        "note_date": "2025-05-02",
        "note_datetime": "2025-05-02T11:00:00",
        "note_title": "Memory clinic interim",
        "note_text": (
            "SYNTHETIC NOTE - NOT REAL PATIENT DATA.\n"
            "Interim call. Daughter reports she is tolerating donepezil well.\n"
            "Repeat MMSE 21/30 - broadly unchanged since March.\n"
        ),
    },
]

# --------------------------------------------------------------------------
# Mentions.  `concept_id` here is what the NORMALIZER will claim; the
# chart-review side never sees it.  None => normalizer found no mapping.
# --------------------------------------------------------------------------

MENTIONS = [
    # --- note 1001: the email's worked example ----------------------------
    {
        "note_id": 1001,
        "term": "MMSE",
        "label": "cognitive_assessment",
        "value": "22",
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.94,
        "concept_id": 42869861,
        "concept_name": "Mini-Mental State Examination [MMSE]",
        "vocabulary_id": "LOINC",
        "norm_score": 0.97,
    },
    {
        "note_id": 1001,
        "term": "dementia",
        "label": "diagnosis",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.91,
        "concept_id": 4182210,
        "concept_name": "Dementia",
        "vocabulary_id": "SNOMED",
        "norm_score": 0.95,
    },
    {
        "note_id": 1001,
        "term": "HbA1c",
        "label": "lab",
        "value": "7.8",
        "value_unit": "%",
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.98,
        "concept_id": 3004410,
        "concept_name": "Hemoglobin A1c/Hemoglobin.total in Blood",
        "vocabulary_id": "LOINC",
        "norm_score": 0.99,
    },
    {
        "note_id": 1001,
        "term": "donepezil",
        "label": "medication",
        "value": None,
        "value_unit": None,
        "assertion": "planned",
        "temporal": "current",
        "ner_confidence": 0.96,
        "concept_id": 715997,
        "concept_name": "donepezil",
        "vocabulary_id": "RxNorm",
        "norm_score": 0.93,
    },
    {
        # Negated: must land in NOTE_NLP as evidence, but NOT in a domain table.
        "note_id": 1001,
        "term": "delirium",
        "label": "diagnosis",
        "value": None,
        "value_unit": None,
        "assertion": "absent",
        "temporal": "current",
        "ner_confidence": 0.88,
        "concept_id": 4182210,
        "concept_name": "Dementia",  # deliberately a real concept; negation is what blocks it
        "vocabulary_id": "SNOMED",
        "norm_score": 0.42,
    },
    # --- note 1002 --------------------------------------------------------
    {
        "note_id": 1002,
        "term": "MMSE",
        "label": "cognitive_assessment",
        "value": "29",
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.95,
        "concept_id": 42869861,
        "concept_name": "Mini-Mental State Examination [MMSE]",
        "vocabulary_id": "LOINC",
        "norm_score": 0.97,
    },
    {
        "note_id": 1002,
        "term": "fall",
        "label": "event",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "history",
        "ner_confidence": 0.89,
        "concept_id": 436583,
        "concept_name": "Fall",
        "vocabulary_id": "SNOMED",
        "norm_score": 0.90,
    },
    {
        # Normalizes to a Visit concept -> DOMAIN_NOT_ROUTED (writing this would
        # corrupt visit accounting).
        "note_id": 1002,
        "term": "inpatient visit",
        "label": "encounter",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "history",
        "ner_confidence": 0.85,
        "concept_id": 9201,
        "concept_name": "Inpatient Visit",
        "vocabulary_id": "Visit",
        "norm_score": 0.88,
    },
    {
        # Normalizer returns nothing -> NOTE_NLP only, concept 0.
        "note_id": 1002,
        "term": "Frailty",
        "label": "finding",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.79,
        "concept_id": None,
        "concept_name": None,
        "vocabulary_id": None,
        "norm_score": None,
    },
    {
        # Low normalizer confidence -> gated out by --min-confidence.
        "note_id": 1002,
        "term": "gait speed",
        "label": "finding",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.72,
        "concept_id": 436583,
        "concept_name": "Fall",  # a bad mapping the score correctly distrusts
        "vocabulary_id": "SNOMED",
        "norm_score": 0.31,
    },
    {
        # Evidence read out of the CDM, not found in a note.  Rubric answers can
        # cite structured rows this way; re-inserting them would double-count
        # existing EHR data, so the writer must refuse it outright.
        "note_id": 1002,
        "term": "pneumonia",
        "label": "diagnosis",
        "value": None,
        "value_unit": None,
        "assertion": "present",
        "temporal": "history",
        "ner_confidence": 0.99,
        "evidence_source": "omop",
        "concept_id": 4182210,
        "concept_name": "Dementia",
        "vocabulary_id": "SNOMED",
        "norm_score": 0.99,
    },
    # --- note "2025-05-02__memory_clinic": the string-note-id crosswalk -------
    {
        "note_id": "2025-05-02__memory_clinic",
        "term": "MMSE",
        "label": "cognitive_assessment",
        "value": "21",
        "value_unit": None,
        "assertion": "present",
        "temporal": "current",
        "ner_confidence": 0.93,
        "concept_id": 42869861,
        "concept_name": "Mini-Mental State Examination [MMSE]",
        "vocabulary_id": "LOINC",
        "norm_score": 0.97,
    },
]


def locate(text: str, term: str) -> tuple[int, int]:
    idx = text.find(term)
    if idx < 0:
        raise SystemExit(f"fixture bug: term {term!r} not present in note text")
    return idx, idx + len(term)


def snippet_around(text: str, start: int, end: int, pad: int = 45) -> str:
    lo, hi = max(0, start - pad), min(len(text), end + pad)
    return ("..." if lo > 0 else "") + text[lo:hi].replace("\n", " ") + ("..." if hi < len(text) else "")


def main() -> int:
    notes_by_id = {n["note_id"]: n for n in NOTES}

    # 1. notes + persons ---------------------------------------------------
    notes_dir = FIXTURES / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "notes.json").write_text(
        json.dumps({"persons": PERSONS, "notes": NOTES}, indent=2) + "\n"
    )

    # 2. chart-review (Yuhang) output — grouped per note -------------------
    cr_dir = FIXTURES / "chart_review_output"
    cr_dir.mkdir(parents=True, exist_ok=True)
    norm_dir = FIXTURES / "normalizer_output"
    norm_dir.mkdir(parents=True, exist_ok=True)

    for note in NOTES:
        text = note["note_text"]
        entities = []
        mentions_out = []
        for mention in [m for m in MENTIONS if m["note_id"] == note["note_id"]]:
            start, end = locate(text, mention["term"])
            entities.append(
                {
                    "entity_id": f"{note['note_id']}-{start}-{end}",
                    "text": mention["term"],
                    "label": mention["label"],
                    "span": {"start": start, "end": end},
                    "quote": snippet_around(text, start, end),
                    "assertion": mention["assertion"],
                    "temporality": mention["temporal"],
                    "source": mention.get("evidence_source", "note"),
                    "value": mention["value"],
                    "value_unit": mention["value_unit"],
                    "confidence": mention["ner_confidence"],
                }
            )
            # The normalizer echoes note_id + span; that pairing is the join key.
            candidates = []
            if mention["concept_id"] is not None:
                candidates.append(
                    {
                        "concept_id": mention["concept_id"],
                        "concept_name": mention["concept_name"],
                        "vocabulary_id": mention["vocabulary_id"],
                        "score": mention["norm_score"],
                    }
                )
            mentions_out.append(
                {
                    "mention_id": f"{note['note_id']}-{start}-{end}",
                    "note_id": note["note_id"],
                    "span": {"start": start, "end": end},
                    "text": mention["term"],
                    "candidates": candidates,
                }
            )

        (cr_dir / f"note_{note['note_id']}.json").write_text(
            json.dumps(
                {
                    "pipeline": "chart-review-platform",
                    "version": CHART_REVIEW_VERSION,
                    "model": CHART_REVIEW_MODEL,
                    "review_kind": "ner",
                    "note_id": note["note_id"],
                    "patient_id": note["person_id"],
                    "note_datetime": note["note_datetime"],
                    "entities": entities,
                },
                indent=2,
            )
            + "\n"
        )
        (norm_dir / f"note_{note['note_id']}.json").write_text(
            json.dumps(
                {
                    "pipeline": "bso-normalizer",
                    "version": NORMALIZER_VERSION,
                    "mentions": mentions_out,
                },
                indent=2,
            )
            + "\n"
        )

    total = len(MENTIONS)
    print(f"[fixtures] {len(PERSONS)} persons, {len(NOTES)} notes, {total} mentions")
    print(f"[fixtures] wrote {notes_dir}/notes.json")
    print(f"[fixtures] wrote {cr_dir}/ and {norm_dir}/")
    assert notes_by_id  # sanity
    return 0


if __name__ == "__main__":
    sys.exit(main())
