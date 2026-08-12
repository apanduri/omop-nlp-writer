"""Adapters translating each upstream pipeline into the ExtractionRecord contract.

`build_records` is the join point: NER mentions supply the span and the value,
the normalizer supplies the concept_id, and the pair is matched on
(note_id, span.start, span.end).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..record import ExtractionRecord
from . import acts, chart_review, normalizer


def build_acts_records(
    acts_path: Path,
    normalizer: object | None = None,
) -> tuple[list[ExtractionRecord], list[str]]:
    """ACTS rubric output -> ExtractionRecords, resolving each field's concept.

    `normalizer` is a NormalizerClient. Without one, records still come through
    carrying no concept — they land in NOTE_NLP as evidence rather than being
    dropped, which is the same degradation as a missing normalizer anywhere else.
    """
    warnings: list[str] = []
    records: list[ExtractionRecord] = []
    seen_terms: dict[str, str] = {}

    for partial in acts.read_answers(acts_path):
        term = partial.pop("source_term")
        field_id = partial.pop("acts_field_id")

        if normalizer is not None:
            resolved = normalizer.resolve(term)
            partial["concept_id"] = resolved.concept_id
            if resolved.concept_id is None and term not in seen_terms:
                # Report once per field, not once per answer: a 29-field rubric
                # over many notes would otherwise bury the signal.
                seen_terms[term] = resolved.status
                if resolved.is_reviewed_nonmapping:
                    warnings.append(
                        f"{field_id}: reviewed as having no suitable concept "
                        f"({resolved.detail}) — evidence only, by decision"
                    )
                else:
                    warnings.append(
                        f"{field_id}: not resolved ({resolved.status}: "
                        f"{resolved.detail}) — evidence only"
                    )
        elif term not in seen_terms:
            seen_terms[term] = "no-normalizer"
            warnings.append(
                f"{field_id}: no normalizer available — evidence only"
            )

        partial["record_id"] = f"{partial['record_id']}:{partial.get('concept_id') or 0}"
        records.append(ExtractionRecord.from_dict(partial))

    return records, warnings


def build_records(
    chart_review_path: Path,
    normalizer_path: Path | None = None,
    vocab: object | None = None,
) -> tuple[list[ExtractionRecord], list[str]]:
    """Join the two pipelines' outputs into ExtractionRecords.

    Returns (records, warnings).  A mention with no normalizer entry still
    becomes a record with concept_id=None — it lands in NOTE_NLP as evidence
    rather than being silently dropped.
    """
    warnings: list[str] = []
    norm_index = (
        normalizer.read_normalizations(normalizer_path) if normalizer_path else {}
    )
    matched: set[tuple[int, int, int]] = set()

    records: list[ExtractionRecord] = []
    for partial in chart_review.read_entities(chart_review_path):
        key = (partial["note_id"], partial["span"]["start"], partial["span"]["end"])
        norm = norm_index.get(key)
        if norm is None:
            if norm_index:
                warnings.append(
                    f"no normalizer output for {partial['lexical_variant']!r} "
                    f"at note {key[0]} offset {key[1]}-{key[2]} — NOTE_NLP only"
                )
        else:
            matched.add(key)
            partial["concept_id"] = norm.concept_id
            partial["concept_confidence"] = norm.score
            partial["source"]["normalizer"] = norm.get("normalizer")
            if norm.concept_id is None:
                warnings.append(
                    f"normalizer returned no candidate for {partial['lexical_variant']!r} "
                    f"at note {key[0]} offset {key[1]}-{key[2]} — NOTE_NLP only"
                )
            elif (norm.get("candidate_count") or 0) > 1:
                warnings.append(
                    f"{partial['lexical_variant']!r} at note {key[0]}: "
                    f"{norm['candidate_count']} candidates, took top score {norm.score}"
                )
        # No separate normalizer output? Resolve the BSO-AD pair the NER stage
        # already emits against the registered custom vocabulary.  This is the
        # whole "target OMOP" path: a deterministic registry lookup, then the
        # writer follows 'Maps to' to a standard concept where one exists.
        if partial.get("concept_id") is None and vocab is not None:
            et, cn = partial.get("entity_type"), partial.get("concept_name")
            if et and cn:
                resolved = vocab.concept_id_for_source(et, cn)
                if resolved is None:
                    warnings.append(
                        f"{cn!r} (entity_type {et}) is not in the registered "
                        f"vocabulary — register it with concept-normalizer, or it is a "
                        f"new novel_candidate not yet promoted into the ontology"
                    )
                else:
                    partial["concept_id"] = resolved
                    partial["concept_confidence"] = (
                        None if partial.get("match_status") == "mapped_uncertain" else 1.0
                    )

        # record_id includes the concept so re-normalizing to a different concept
        # is a new record rather than a silent no-op on re-run.
        partial["record_id"] = f"{partial['record_id']}:{partial.get('concept_id') or 0}"
        records.append(ExtractionRecord.from_dict(partial))

    orphans = set(norm_index) - matched
    for key in sorted(orphans):
        warnings.append(
            f"normalizer produced a mapping for note {key[0]} offset {key[1]}-{key[2]} "
            f"with no matching NER mention — dropped (join key mismatch?)"
        )

    return records, warnings


def records_to_json(records: list[ExtractionRecord]) -> list[dict[str, Any]]:
    """Serialize back to the CONTRACT.md wire format (for --emit)."""
    out = []
    for r in records:
        out.append(
            {
                "record_id": r.record_id,
                "source": {
                    "pipeline": r.source.pipeline,
                    "version": r.source.version,
                    "model": r.source.model,
                    "normalizer": r.source.normalizer,
                },
                "note_id": r.note_id,
                "person_id": r.person_id,
                "note_datetime": r.note_datetime,
                "span": {"start": r.span.start, "end": r.span.end},
                "lexical_variant": r.lexical_variant,
                "snippet": r.snippet,
                "section_concept_id": r.section_concept_id,
                "term_exists": r.term_exists,
                "term_temporal": r.term_temporal,
                "term_modifiers": r.term_modifiers,
                "concept_id": r.concept_id,
                "concept_confidence": r.concept_confidence,
                "evidence_source": r.evidence_source,
                "value": {
                    "as_number": r.value.as_number,
                    "as_string": r.value.as_string,
                    "as_concept_id": r.value.as_concept_id,
                    "unit_concept_id": r.value.unit_concept_id,
                    "unit_source_value": r.value.unit_source_value,
                    "operator_concept_id": r.value.operator_concept_id,
                },
                "start_date": r.start_date,
            }
        )
    return out


__all__ = ["acts", "build_acts_records", "build_records", "records_to_json",
           "chart_review", "normalizer"]
