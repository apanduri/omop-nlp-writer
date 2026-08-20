"""The ExtractionRecord — the single input contract for this writer.

See CONTRACT.md.  Deliberately stdlib-only (dataclasses + json) so the package
runs anywhere without an install step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


class ContractError(ValueError):
    """Raised when an input record does not satisfy CONTRACT.md."""


@dataclass(slots=True)
class Span:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ContractError(f"invalid span {self.start}-{self.end}")

    @property
    def omop_offset(self) -> str:
        """NOTE_NLP.offset is a varchar(50) in CDM 5.4, not an integer.

        We store "start-end" so the span is round-trippable; the CDM only
        requires "character offset of the extracted term in the note text".
        """
        return f"{self.start}-{self.end}"


@dataclass(slots=True)
class Source:
    pipeline: str
    version: str | None = None
    model: str | None = None
    normalizer: str | None = None

    @property
    def nlp_system(self) -> str:
        """NOTE_NLP.nlp_system — free text naming the system + version."""
        parts = [self.pipeline]
        if self.version:
            parts.append(f"v{self.version}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.normalizer:
            parts.append(f"normalizer={self.normalizer}")
        return " ".join(parts)


@dataclass(slots=True)
class Value:
    as_number: float | None = None
    as_string: str | None = None
    as_concept_id: int | None = None
    unit_concept_id: int | None = None
    unit_source_value: str | None = None
    operator_concept_id: int | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.as_number is None
            and self.as_string is None
            and self.as_concept_id is None
        )


@dataclass(slots=True)
class ExtractionRecord:
    record_id: str
    # int  -> a CDM surrogate key, used directly.
    # str  -> an upstream identifier, resolved via NOTE.note_source_value /
    #         PERSON.person_source_value.  The chart-review platform emits string
    #         note ids like "2024-12-04__pathology_report", so both are accepted.
    #         Types are honoured as given: "1001" is a source value, 1001 is a key.
    note_id: int | str
    person_id: int | str
    span: Span
    lexical_variant: str
    source: Source
    snippet: str | None = None
    note_datetime: str | None = None
    section_concept_id: int | None = None
    term_exists: bool = True
    term_temporal: str | None = None
    term_modifiers: str | None = None
    concept_id: int | None = None
    concept_confidence: float | None = None
    value: Value = field(default_factory=Value)
    start_date: str | None = None
    # Where the fact came from.  "note" is the only insertable source: an "omop"
    # evidence entry cites a structured row the extractor *read*, so writing it
    # back would re-insert existing EHR data as NLP-derived.
    evidence_source: str | None = None

    # ---------------------------------------------------------------- parsing

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractionRecord:
        missing = [
            k
            for k in ("record_id", "note_id", "person_id", "span", "lexical_variant", "source")
            if data.get(k) is None
        ]
        if missing:
            raise ContractError(f"record missing required field(s): {', '.join(missing)}")

        span_raw = data["span"]
        if not isinstance(span_raw, dict) or "start" not in span_raw or "end" not in span_raw:
            raise ContractError(f"{data['record_id']}: span must be {{start, end}}")

        src_raw = data["source"]
        if isinstance(src_raw, str):
            src = Source(pipeline=src_raw)
        elif isinstance(src_raw, dict):
            if not src_raw.get("pipeline"):
                raise ContractError(f"{data['record_id']}: source.pipeline is required")
            src = Source(
                pipeline=src_raw["pipeline"],
                version=src_raw.get("version"),
                model=src_raw.get("model"),
                normalizer=src_raw.get("normalizer"),
            )
        else:
            raise ContractError(f"{data['record_id']}: source must be a string or object")

        val_raw = data.get("value") or {}
        if not isinstance(val_raw, dict):
            raise ContractError(f"{data['record_id']}: value must be an object")

        concept_id = data.get("concept_id")
        if concept_id is not None:
            try:
                concept_id = int(concept_id)
            except (TypeError, ValueError):
                raise ContractError(
                    f"{data['record_id']}: concept_id must be an integer, got {concept_id!r}"
                ) from None

        if "domain_id" in data:
            # Not an error — just make it loud that we ignore it on purpose.
            # The vocabulary is the single source of truth for domain routing.
            pass

        return cls(
            record_id=str(data["record_id"]),
            note_id=_reference(data["note_id"], data["record_id"], "note_id"),
            person_id=_reference(data["person_id"], data["record_id"], "person_id"),
            span=Span(int(span_raw["start"]), int(span_raw["end"])),
            lexical_variant=str(data["lexical_variant"]),
            source=src,
            snippet=data.get("snippet"),
            note_datetime=data.get("note_datetime"),
            section_concept_id=data.get("section_concept_id"),
            term_exists=bool(data.get("term_exists", True)),
            term_temporal=data.get("term_temporal"),
            term_modifiers=data.get("term_modifiers"),
            concept_id=concept_id,
            concept_confidence=data.get("concept_confidence"),
            value=Value(
                as_number=val_raw.get("as_number"),
                as_string=val_raw.get("as_string"),
                as_concept_id=val_raw.get("as_concept_id"),
                unit_concept_id=val_raw.get("unit_concept_id"),
                unit_source_value=val_raw.get("unit_source_value"),
                operator_concept_id=val_raw.get("operator_concept_id"),
            ),
            start_date=data.get("start_date"),
            evidence_source=data.get("evidence_source"),
        )

    @property
    def is_note_derived(self) -> bool:
        """Only note-derived facts may enter the CDM.

        An unset evidence_source is treated as note-derived: every current
        producer is an NLP pipeline over note text, and the field exists to catch
        the specific case of rubric evidence citing a structured OMOP row.
        """
        return self.evidence_source is None or self.evidence_source == "note"

    @property
    def is_normalized(self) -> bool:
        """A concept_id of None or 0 means the normalizer produced no mapping.

        0 is OMOP's "No matching concept" sentinel, so both are treated as
        unmapped: NOTE_NLP still gets the row, no domain row is written.
        """
        return bool(self.concept_id)

    @property
    def event_date(self) -> str:
        """Date to stamp on the domain row, from the record alone."""
        return self.event_date_or(None)

    def event_date_or(self, fallback: str | None) -> str:
        """Date for the domain row, falling back to the note's own date.

        Rubric-style output (a per-patient review) carries no date at all, so the
        NOTE row supplies it. Preferring the record's own date keeps span-based
        extractions unchanged.
        """
        if self.start_date:
            return self.start_date[:10]
        if self.note_datetime:
            return self.note_datetime[:10]
        if fallback:
            return str(fallback)[:10]
        raise ContractError(
            f"{self.record_id}: no start_date or note_datetime, and the resolved "
            f"NOTE has no note_date either — nothing to date the domain row with"
        )


def _reference(value: object, record_id: object, field_name: str) -> int | str:
    """Keep an id's declared type: int means CDM key, str means source value."""
    if isinstance(value, bool):
        raise ContractError(f"{record_id}: {field_name} must be an int or string")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ContractError(
        f"{record_id}: {field_name} must be an int (CDM key) or a non-empty string "
        f"(source value), got {value!r}"
    )


def load_records(path: Path) -> list[ExtractionRecord]:
    """Load ExtractionRecords from a JSON file or a directory of them.

    Accepts either a bare JSON list or {"records": [...]}.
    """
    return list(iter_records(path))


def iter_records(path: Path) -> Iterator[ExtractionRecord]:
    for file in _json_files(path):
        raw = json.loads(file.read_text())
        items = raw.get("records", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ContractError(f"{file}: expected a list of records or {{'records': [...]}}")
        for item in items:
            yield ExtractionRecord.from_dict(item)


def _json_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        if not files:
            raise ContractError(f"no .json files found in {path}")
        return files
    if not path.exists():
        raise ContractError(f"input path does not exist: {path}")
    return [path]
