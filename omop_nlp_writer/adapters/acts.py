"""Adapter: ACTS `review_state.json` -> ExtractionRecords.

Written against docs/ACTS_OUTPUT_FORMAT.md, which was measured over 42 real
review files and 921 field assessments — not inferred from the criteria docs.
Several of this adapter's earlier assumptions were wrong, and the corrections are
the interesting part:

  * The answers live under `field_assessments`, not `answers`.
  * One file per (session x patient x task), NOT per note. There is no
    document-level note_id; the note is named only inside each citation.
  * `patient_id` and `note_id` are STRINGS — a patient slug and a note *filename*.
    The writer resolves both through *_source_value.
  * Booleans are the strings "1"/"0", and `cdr_global` is a string that includes
    "0.5" — casting it to int silently loses the value.
  * Computed fields are marked `source: "derived"` in the data, so they no longer
    have to be recognised from a hardcoded list.
  * `answer: null` means "applicable, not documented" and MUST NOT become 0. The
    rubric is explicit that 0 is a real, severe score.
  * `[]` on an entity list means "affirmatively none" (NKDA) — a negative
    finding, which is different again from null.
  * 856 of 921 assessments carry NO citation. Without one there is no note and no
    date, so the fact cannot be attached to anything; those are reported rather
    than invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

# Assessment statuses whose answer is settled enough to load.  `pending` has not
# been decided and `not_applicable` was excluded by the rubric's own gating.
LOADABLE_STATUS = frozenset({"approved", "agent_proposed", "overridden"})

# File-level review states that represent an unvalidated draft.  Loading these is
# allowed but warned about: they can still change.
UNVALIDATED_REVIEW_STATUS = frozenset({"draft", "in_progress", "agent_complete"})

# Fields the platform computes from another field.  `source: "derived"` is the
# authoritative signal; this set is a backstop for files that predate it.
DERIVED_FIELDS = frozenset({
    "apoe2", "apoe3", "apoe4",
    "mmse_severity", "moca_severity", "cdr_severity",
    "vaccine_category",
})

# Answers to read as numbers.  cdr_global and gds_stage arrive as STRINGS but are
# scores ("0.5", "3"), so they parse as floats rather than being treated as
# categories.
NUMERIC_FIELDS = frozenset({
    "mmse_score", "moca_score", "mattis_drs", "tics_score", "hachinski_score",
    "gds_depression_score", "cornell_csdd", "npi_total",
    "education_years", "pack_year", "pack_per_day", "smoking_duration",
    "cdr_global", "gds_stage",
})

# Answers asserting presence or absence, as the strings "1"/"0". A "0" is a
# NEGATIVE finding — cognition documented normal — and must never become a
# positive clinical fact.
BOOLEAN_FIELDS = frozenset({"impaired_cognition", "postmenopause"})

CATEGORICAL_FIELDS = frozenset({"smoking_status", "apoe_genotype"})
DATE_FIELDS = frozenset({"lmp_date", "quit_time"})

# Entity lists: arrays of objects whose keys are PascalCase, unlike the snake_case
# field ids.  The substance is part of the identity, so each element is a separate
# fact needing its own concept.
ENTITY_LIST_FIELDS: dict[str, str] = {
    "allergen": "Allergen",
    "vaccine_name": "Vaccine_Name",
}

CONFIDENCE = {"high": 0.95, "medium": 0.7, "low": 0.4}

TRUTHY = {"1", "true", "yes", "y", "present"}
FALSY = {"0", "false", "no", "n", "absent"}


class ActsFormatError(ValueError):
    """The input does not look like an ACTS review_state file."""


def read_answers(path: Path) -> Iterator[dict[str, Any]]:
    """Yield partial ExtractionRecord dicts from review_state / agent_draft files.

    One record per (assessment, citation) pair, plus one per entity-list element.
    """
    for file in _json_files(path):
        doc = json.loads(file.read_text())
        assessments = doc.get("field_assessments")
        if assessments is None:
            raise ActsFormatError(
                f"{file}: no 'field_assessments' key — is this a review_state.json "
                f"or agent_draft.json? NER span output goes through "
                f"adapters/chart_review.py instead."
            )

        patient_id = doc.get("patient_id")
        if not patient_id:
            raise ActsFormatError(f"{file}: no patient_id")

        review_status = doc.get("review_status", "")
        source_meta = {
            "pipeline": "chart-review-platform",
            # manifest.model is documented as unreliable, so the rubric version is
            # the honest provenance to carry.
            "version": doc.get("task_version") or doc.get("schema_version"),
            "model": None,
        }

        for assessment in assessments:
            field_id = assessment.get("field_id")
            if not field_id:
                raise ActsFormatError(f"{file}: an assessment has no field_id")

            # The data says which fields are computed; trust it over our own list.
            if assessment.get("source") == "derived" or field_id in DERIVED_FIELDS:
                continue

            status = assessment.get("status", "approved")
            if status not in LOADABLE_STATUS:
                continue

            raw = assessment.get("answer")

            # null = applicable but not documented. NOT zero, NOT "none".
            # ("" is not in the observed format, but is treated the same; note
            # that [] is NOT caught here — it means affirmatively none.)
            if raw is None or raw == "":
                continue

            all_evidence = assessment.get("evidence") or []
            citations = [
                e for e in all_evidence if (e.get("source") or "note") == "note"
            ]
            if all_evidence and not citations:
                # Evidenced, but only from structured CDM rows — the fact is
                # already in the database. Dropped outright, which is different
                # from "answered with no citation at all".
                continue

            confidence = _confidence(assessment)
            common = {
                "person_id": patient_id,
                "evidence_source": "note",
                "concept_confidence": confidence,
                "source": dict(source_meta),
                "review_status": review_status,
            }

            entity_key = ENTITY_LIST_FIELDS.get(field_id)
            if entity_key is not None:
                yield from _entity_records(
                    file, field_id, entity_key, raw, assessment, citations, common
                )
                continue

            if not citations:
                # No citation means no note and no date, so the fact cannot be
                # attached to anything. Reported by the caller, never invented.
                yield {
                    **common,
                    "_uncited": True,
                    "acts_field_id": field_id,
                    "raw_answer": raw,
                }
                continue

            value, term_exists, note = _typed_value(field_id, raw)
            for citation in citations:
                yield {
                    **common,
                    **_from_citation(citation, file, field_id),
                    "term_exists": term_exists,
                    "term_modifiers": _modifiers(field_id, raw, assessment, note),
                    "value": value,
                    "raw_answer": raw,
                    "source_term": field_id,
                    "acts_field_id": field_id,
                }


def _entity_records(
    file: Path,
    field_id: str,
    entity_key: str,
    raw: Any,
    assessment: dict[str, Any],
    citations: list[dict[str, Any]],
    common: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Entity lists: one record per element, because the substance is the concept.

    OMOP puts the substance inside the concept — "Allergy to penicillin" is one
    concept — so a list of two allergens is two facts, not one fact with a list
    value.
    """
    if not isinstance(raw, list):
        raise ActsFormatError(
            f"{file}: {field_id} answer should be a list of objects, got {type(raw).__name__}"
        )

    if not raw:
        # [] is affirmatively none documented (NKDA) — a negative finding, and
        # distinct from null, which means nobody looked.
        for citation in citations or [None]:
            record = {**common, "acts_field_id": field_id, "raw_answer": []}
            if citation is None:
                yield {**record, "_uncited": True}
                continue
            yield {
                **record,
                **_from_citation(citation, file, field_id),
                "term_exists": False,
                "term_modifiers": f"acts_field={field_id},answer=[],note=affirmatively none documented",
                "value": {},
                "source_term": field_id,
            }
        return

    for element in raw:
        if not isinstance(element, dict):
            raise ActsFormatError(
                f"{file}: {field_id} element should be an object, got {element!r}"
            )
        substance = element.get(entity_key)
        if not substance:
            raise ActsFormatError(
                f"{file}: {field_id} element has no {entity_key}: {element!r}"
            )
        # Entity elements carry Supporting_Evidence as free text with NO offsets,
        # so a span can only come from the assessment-level citations.
        supporting = element.get("Supporting_Evidence")
        modifiers = _entity_modifiers(field_id, entity_key, element)

        for citation in citations or [None]:
            record = {
                **common,
                "acts_field_id": field_id,
                # The substance is what needs a concept, not the field.
                "source_term": f"{field_id}.{substance}",
                "raw_answer": substance,
            }
            if citation is None:
                yield {**record, "_uncited": True, "_detail": f"{substance!r}"}
                continue
            yield {
                **record,
                **_from_citation(citation, file, field_id),
                "lexical_variant": str(substance),
                "snippet": supporting or citation.get("verbatim_quote"),
                "term_exists": _entity_exists(element),
                "term_modifiers": modifiers,
                "value": {},
            }


def _from_citation(citation: dict[str, Any], file: Path, field_id: str) -> dict[str, Any]:
    note_id = citation.get("note_id")
    if not note_id:
        raise ActsFormatError(f"{file}: {field_id} citation has no note_id")
    offsets = citation.get("span_offsets")
    if not (isinstance(offsets, list) and len(offsets) >= 2):
        raise ActsFormatError(
            f"{file}: {field_id} citation has no span_offsets — required for "
            f"NOTE_NLP.offset and to key records apart"
        )
    start, end = int(offsets[0]), int(offsets[1])
    quote = citation.get("verbatim_quote")
    out = {
        "record_id": f"acts:{note_id}:{field_id}:{start}-{end}",
        "note_id": note_id,
        "span": {"start": start, "end": end},
        "lexical_variant": quote or field_id,
        "snippet": quote,
        # No note date anywhere in the file: ACTS is patient-level, and the note
        # filename encodes the service date but parsing it here would be guessing.
        # The writer takes the date from the NOTE row it resolves.
        "note_datetime": None,
        "doc_type": citation.get("doc_type"),
        "author_role": citation.get("author_role"),
    }
    return out


def _confidence(assessment: dict[str, Any]) -> float | None:
    """Confidence is absent on 918/921 assessments.

    Once a reviewer touches a field the reviewer's assessment carries none, and
    the agent's original value survives in original_agent_snapshot.
    """
    raw = assessment.get("confidence")
    if raw is None:
        snapshot = assessment.get("original_agent_snapshot") or {}
        raw = snapshot.get("confidence")
    return CONFIDENCE.get(str(raw).lower()) if raw else None


def _typed_value(field_id: str, raw: Any) -> tuple[dict[str, Any], bool, str]:
    """Route the answer to the right OMOP value slot for its type."""
    empty: dict[str, Any] = {}

    if field_id in BOOLEAN_FIELDS:
        s = str(raw).strip().lower()
        if s in FALSY:
            return empty, False, "negative finding"
        if s in TRUTHY:
            return empty, True, ""
        return empty, True, f"unrecognised boolean {raw!r} — treated as present"

    if field_id in NUMERIC_FIELDS:
        try:
            # float() and not int(): cdr_global arrives as the string "0.5".
            return {"as_number": float(raw)}, True, ""
        except (TypeError, ValueError):
            return {"as_string": str(raw)}, True, f"expected a number, got {raw!r}"

    if field_id in DATE_FIELDS:
        return ({"as_string": str(raw)}, True,
                "free-text date ('two weeks ago' occurs); OMOP has no date value "
                "column — modelling decision pending")

    if field_id in CATEGORICAL_FIELDS:
        return {"as_string": str(raw)}, True, ""

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"as_number": float(raw)}, True, f"unknown field {field_id!r}"
    return {"as_string": str(raw)}, True, f"unknown field {field_id!r}"


def _entity_exists(element: dict[str, Any]) -> bool:
    """An allergy refuted or entered in error is not a fact about the patient."""
    verification = str(element.get("Verification_Status", "")).lower()
    clinical = str(element.get("Clinical_Status", "")).lower()
    if verification in {"refuted", "entered-in-error"}:
        return False
    if clinical in {"resolved", "inactive"}:
        # Historical rather than false: kept as evidence, not asserted as current.
        return False
    return True


def _entity_modifiers(field_id: str, entity_key: str, element: dict[str, Any]) -> str:
    parts = [f"acts_field={field_id}", f"{entity_key.lower()}={element.get(entity_key)}"]
    for key in ("Category", "Type", "Reaction", "Severity", "Clinical_Status",
                "Verification_Status", "Administration_Date", "Disease"):
        if element.get(key):
            parts.append(f"{key.lower()}={element[key]}")
    return ",".join(parts)


def _modifiers(field_id: str, raw: Any, assessment: dict[str, Any], note: str) -> str:
    parts = [f"acts_field={field_id}", f"answer={raw}"]
    if assessment.get("source"):
        parts.append(f"assessed_by={assessment['source']}")
    if assessment.get("status"):
        parts.append(f"status={assessment['status']}")
    if assessment.get("confidence"):
        parts.append(f"confidence={assessment['confidence']}")
    if note:
        parts.append(f"note={note}")
    return ",".join(parts)


def _json_files(path: Path) -> list[Path]:
    if path.is_dir():
        # Reviews are nested <session>/<patient>/<task>/review_state.json.
        files = sorted(path.rglob("review_state.json")) + sorted(
            path.rglob("agent_draft.json")
        )
        if not files:
            files = sorted(path.glob("*.json"))
        if not files:
            raise ActsFormatError(f"no review_state.json or *.json under {path}")
        return files
    if not path.exists():
        raise ActsFormatError(f"path does not exist: {path}")
    return [path]
