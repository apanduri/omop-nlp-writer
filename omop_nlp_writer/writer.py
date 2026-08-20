"""The writer: ExtractionRecord -> NOTE_NLP row (+ domain row when warranted).

Two-phase by design: `plan()` decides everything and touches nothing, `execute()`
performs the inserts.  That split is what makes --dry-run trustworthy — it is the
same decision path as a real load, minus the INSERTs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .cdm import connect, init_schema
from .domains import (
    DOMAIN_TARGETS,
    NLP_ID_BASE,
    NLP_TYPE_CONCEPT_ID,
    UNROUTED_DOMAINS,
    DomainTarget,
    route,
)
from .record import ExtractionRecord
from .vocab import Resolution, VocabLookup


class Disposition(str, Enum):
    """What happened to a record, and why."""

    WRITTEN = "written"                    # NOTE_NLP + domain row
    NOTE_NLP_ONLY = "note_nlp_only"        # kept as evidence, no domain row
    FACT_ONLY = "fact_only"                # domain row, no evidence row (uncited)
    SKIPPED = "skipped"                    # nothing written


class Reason(str, Enum):
    OK = "ok"
    ALREADY_LOADED = "already_loaded"
    NOTE_NOT_FOUND = "note_not_found"
    PERSON_MISMATCH = "person_mismatch"
    NOT_NOTE_DERIVED = "evidence_source_is_not_a_note"
    PERSON_NOT_FOUND = "person_not_found"
    NEGATED = "term_exists=false"
    UNMAPPED = "no_concept_id"
    CONCEPT_NOT_IN_VOCAB = "concept_not_in_vocabulary"
    NO_STANDARD_MAPPING = "non_standard_concept_with_no_maps_to"
    LOW_CONFIDENCE = "below_confidence_threshold"
    DOMAIN_NOT_ROUTED = "domain_not_routed"
    NO_DOMAIN_ON_CONCEPT = "concept_has_no_domain_id"


@dataclass(slots=True)
class PlannedWrite:
    record: ExtractionRecord
    disposition: Disposition
    reason: Reason
    detail: str | None = None
    domain_id: str | None = None
    concept_name: str | None = None
    resolved_note_id: int | None = None
    resolved_person_id: int | None = None
    note_nlp_id: int | None = None
    note_nlp_row: dict[str, Any] | None = None
    target: DomainTarget | None = None
    domain_row_id: int | None = None
    domain_row: dict[str, Any] | None = None


@dataclass(slots=True)
class LoadReport:
    planned: list[PlannedWrite] = field(default_factory=list)
    dry_run: bool = True

    @property
    def written(self) -> list[PlannedWrite]:
        return [p for p in self.planned if p.disposition is Disposition.WRITTEN]

    @property
    def note_nlp_only(self) -> list[PlannedWrite]:
        return [p for p in self.planned if p.disposition is Disposition.NOTE_NLP_ONLY]

    @property
    def fact_only(self) -> list[PlannedWrite]:
        return [p for p in self.planned if p.disposition is Disposition.FACT_ONLY]

    @property
    def skipped(self) -> list[PlannedWrite]:
        return [p for p in self.planned if p.disposition is Disposition.SKIPPED]

    @property
    def note_nlp_rows(self) -> int:
        return len(self.written) + len(self.note_nlp_only)

    @property
    def domain_rows(self) -> int:
        return len(self.written) + len(self.fact_only)


class CdmNlpWriter:
    def __init__(
        self,
        cdm_path: Path,
        vocab_path: Path,
        *,
        confidence_threshold: float = 0.0,
        create: bool = False,
        relationship_path: Path | None = None,
    ):
        self.conn = connect(cdm_path, create=create)
        if create:
            init_schema(self.conn)
        self.vocab = VocabLookup(vocab_path, relationship_path)
        self.confidence_threshold = confidence_threshold
        self._next_ids: dict[str, int] = {}

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        self.vocab.close()
        self.conn.close()

    def __enter__(self) -> CdmNlpWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- plan

    def plan(self, records: Iterable[ExtractionRecord]) -> LoadReport:
        report = LoadReport()
        for record in records:
            report.planned.append(self._plan_one(record))
        return report

    def _plan_one(self, record: ExtractionRecord) -> PlannedWrite:
        if self._already_loaded(record.record_id):
            return PlannedWrite(record, Disposition.SKIPPED, Reason.ALREADY_LOADED)

        # Guard first: a fact the extractor read out of the CDM must never be
        # written back in as NLP-derived.  That would double-count existing EHR
        # data and defeat the provenance separation entirely.
        if not record.is_note_derived:
            return PlannedWrite(
                record,
                Disposition.SKIPPED,
                Reason.NOT_NOTE_DERIVED,
                detail=(
                    f"evidence_source={record.evidence_source!r} — this cites a row the "
                    f"extractor read from the CDM, not a fact found in a note; "
                    f"re-inserting it would duplicate structured EHR data"
                ),
            )

        # An uncited answer has no note. Its clinical fact is still real, so only
        # the evidence row is lost rather than the finding itself.
        note = None
        resolved_note_id = None
        if record.has_evidence:
            note = self._lookup_note(record.note_id)
            if note is None:
                return PlannedWrite(
                    record,
                    Disposition.SKIPPED,
                    Reason.NOTE_NOT_FOUND,
                    detail=self._note_not_found_detail(record.note_id),
                )
            resolved_note_id = int(note["note_id"])

        resolved_person_id = self._resolve_person(record.person_id)
        if resolved_person_id is None:
            return PlannedWrite(
                record,
                Disposition.SKIPPED,
                Reason.PERSON_NOT_FOUND,
                detail=(
                    f"person_source_value {record.person_id!r} is not in PERSON — "
                    f"the person crosswalk is missing this patient"
                ),
            )
        if note is not None and int(note["person_id"]) != resolved_person_id:
            return PlannedWrite(
                record,
                Disposition.SKIPPED,
                Reason.PERSON_MISMATCH,
                detail=(
                    f"record says person {record.person_id} (-> {resolved_person_id}), "
                    f"NOTE {resolved_note_id} belongs to person {note['person_id']}"
                ),
            )

        # A note-derived fact is dated by its note. Rubric output carries no date
        # of its own — it is patient-level — so the NOTE row is the source of
        # truth rather than something to infer from a filename.
        fallback_date = None
        if note is not None:
            try:
                fallback_date = note["note_date"]
            except (IndexError, KeyError):
                pass

        note_nlp_id = None
        note_nlp_row = None
        if resolved_note_id is not None:
            note_nlp_id = self._allocate("note_nlp", "note_nlp_id")
            note_nlp_row = self._build_note_nlp_row(record, note_nlp_id, resolved_note_id)

        # --- decide whether a domain row is also warranted --------------------
        # resolve() follows 'Maps to' when the concept is non-standard, because a
        # non-standard concept in a domain table is invisible to cohort SQL.
        resolution = self.vocab.resolve(record.concept_id) if record.is_normalized else None
        standard = resolution.standard if resolution else None
        domain_id = standard.domain_id if standard else None
        concept_name = None
        if resolution:
            concept_name = resolution.source.concept_name
            if resolution.via_maps_to and standard:
                concept_name += f" -> {standard.concept_name}"

        def evidence_only(reason: Reason, detail: str | None = None) -> PlannedWrite:
            if note_nlp_row is None:
                # No note and no domain row: nothing can be written at all.
                return PlannedWrite(
                    record, Disposition.SKIPPED, reason,
                    detail=(detail or "") + " (and no citation, so not even evidence)",
                    resolved_person_id=resolved_person_id,
                )
            return PlannedWrite(
                record,
                Disposition.NOTE_NLP_ONLY,
                reason,
                detail=detail,
                domain_id=domain_id,
                concept_name=concept_name,
                resolved_note_id=resolved_note_id,
                resolved_person_id=resolved_person_id,
                note_nlp_id=note_nlp_id,
                note_nlp_row=note_nlp_row,
            )

        if not record.term_exists:
            return evidence_only(
                Reason.NEGATED,
                "negated/absent assertion — recorded as evidence but not as a clinical fact",
            )
        if not record.is_normalized:
            return evidence_only(Reason.UNMAPPED, "normalizer produced no concept_id")
        if resolution is None:
            return evidence_only(
                Reason.CONCEPT_NOT_IN_VOCAB,
                f"concept_id {record.concept_id} not found in {self.vocab.db_path.name}",
            )
        if standard is None:
            detail = (
                f"concept {resolution.source.concept_id} "
                f"({resolution.source.concept_name!r}, {resolution.source.vocabulary_id}) "
                f"is non-standard and has no 'Maps to' a standard concept — a domain "
                f"row carrying it would be invisible to cohort SQL"
            )
            if not self.vocab.has_relationships:
                detail += f". Note: {self.vocab.relationship_warning}"
            return evidence_only(Reason.NO_STANDARD_MAPPING, detail)
        if (
            self.confidence_threshold > 0
            and record.concept_confidence is not None
            and record.concept_confidence < self.confidence_threshold
        ):
            return evidence_only(
                Reason.LOW_CONFIDENCE,
                f"confidence {record.concept_confidence} < threshold {self.confidence_threshold}",
            )
        if not domain_id:
            return evidence_only(Reason.NO_DOMAIN_ON_CONCEPT)

        target = route(domain_id)
        if target is None:
            return evidence_only(
                Reason.DOMAIN_NOT_ROUTED,
                UNROUTED_DOMAINS.get(
                    domain_id, f"domain '{domain_id}' has no configured CDM target table"
                ),
            )

        domain_row_id = self._allocate(target.table, target.pk)
        return PlannedWrite(
            record,
            Disposition.WRITTEN if note_nlp_row is not None else Disposition.FACT_ONLY,
            Reason.OK,
            domain_id=domain_id,
            concept_name=concept_name,
            resolved_note_id=resolved_note_id,
            resolved_person_id=resolved_person_id,
            note_nlp_id=note_nlp_id,
            note_nlp_row=note_nlp_row,
            target=target,
            domain_row_id=domain_row_id,
            domain_row=self._build_domain_row(
                record, target, domain_row_id, resolved_person_id, resolution,
                fallback_date,
            ),
        )

    # ------------------------------------------------------- id resolution

    def _lookup_note(self, note_ref: int | str):
        """int -> NOTE.note_id; str -> NOTE.note_source_value.

        note_source_value is the CDM's own slot for the source system's note
        identifier, which makes it the right crosswalk for string ids like
        "2024-12-04__pathology_report" — no side table needed.
        """
        if isinstance(note_ref, int):
            return self.conn.execute(
                "SELECT note_id, person_id, note_date, note_datetime FROM note "
                "WHERE note_id = ?", (note_ref,)
            ).fetchone()
        return self.conn.execute(
            "SELECT note_id, person_id, note_date, note_datetime FROM note "
            "WHERE note_source_value = ?", (note_ref,)
        ).fetchone()

    def _resolve_person(self, person_ref: int | str) -> int | None:
        if isinstance(person_ref, int):
            return person_ref
        row = self.conn.execute(
            "SELECT person_id FROM person WHERE person_source_value = ?", (person_ref,)
        ).fetchone()
        return int(row["person_id"]) if row else None

    def _note_not_found_detail(self, note_ref: int | str) -> str:
        if isinstance(note_ref, int):
            return (
                f"note_id {note_ref} is not in NOTE — load the note first "
                f"(NOTE_NLP.note_id is a foreign key)"
            )
        return (
            f"no NOTE has note_source_value {note_ref!r} — the note must be loaded "
            f"into the CDM before its extractions can be, and its source id recorded "
            f"in note_source_value"
        )

    # ---------------------------------------------------------------- builders

    def _build_note_nlp_row(
        self, record: ExtractionRecord, note_nlp_id: int, resolved_note_id: int
    ) -> dict[str, Any]:
        nlp_dt = _now_iso()
        return {
            "note_nlp_id": note_nlp_id,
            "note_id": resolved_note_id,
            "section_concept_id": record.section_concept_id,
            "snippet": record.snippet,
            "offset": record.span.omop_offset if record.span else None,
            "lexical_variant": record.lexical_variant,
            # NOTE_NLP keeps the mapping even when we decline to create a domain
            # row, so a rejected mapping is still auditable.
            "note_nlp_concept_id": record.concept_id or 0,
            "note_nlp_source_concept_id": None,
            "nlp_system": record.source.nlp_system,
            "nlp_date": nlp_dt[:10],
            "nlp_datetime": nlp_dt,
            # CDM 5.4 types term_exists as varchar(1).
            "term_exists": "Y" if record.term_exists else "N",
            "term_temporal": record.term_temporal,
            "term_modifiers": record.term_modifiers,
        }

    def _build_domain_row(
        self,
        record: ExtractionRecord,
        target: DomainTarget,
        row_id: int,
        resolved_person_id: int,
        resolution: Resolution,
        fallback_date: str | None = None,
    ) -> dict[str, Any]:
        event_date = record.event_date_or(fallback_date)
        assert resolution.standard is not None
        row: dict[str, Any] = {
            target.pk: row_id,
            "person_id": resolved_person_id,
            # The STANDARD concept drives the query; see Resolution.
            target.concept_column: resolution.standard.concept_id,
            target.start_date_column: event_date,
            # The provenance flag: this is what keeps NLP-derived rows
            # distinguishable from structured EHR data.
            target.type_concept_column: NLP_TYPE_CONCEPT_ID,
            target.source_value_column: record.lexical_variant,
        }
        # When 'Maps to' was followed, the original non-standard concept is kept
        # in *_source_concept_id so the mapping stays auditable and reversible.
        if resolution.via_maps_to and target.source_concept_column:
            row[target.source_concept_column] = resolution.source.concept_id
        if target.start_datetime_column:
            row[target.start_datetime_column] = record.note_datetime or f"{event_date}T00:00:00"
        if target.end_date_column:
            # No duration is inferable from a note mention; a zero-length event
            # is the honest representation.
            row[target.end_date_column] = event_date

        v = record.value
        if target.supports_value:
            if "value_as_number" in target.value_columns and v.as_number is not None:
                row["value_as_number"] = float(v.as_number)
            if "value_as_string" in target.value_columns and v.as_string is not None:
                row["value_as_string"] = v.as_string
            if "value_as_concept_id" in target.value_columns and v.as_concept_id is not None:
                row["value_as_concept_id"] = v.as_concept_id
            if "unit_concept_id" in target.value_columns and v.unit_concept_id is not None:
                row["unit_concept_id"] = v.unit_concept_id
            if "operator_concept_id" in target.value_columns and v.operator_concept_id is not None:
                row["operator_concept_id"] = v.operator_concept_id
            if v.unit_source_value is not None:
                row["unit_source_value"] = v.unit_source_value
            if v.as_number is not None:
                row["value_source_value"] = str(v.as_number)
            elif v.as_string is not None:
                row["value_source_value"] = v.as_string
        return row

    # ----------------------------------------------------------------- execute

    def execute(self, report: LoadReport) -> LoadReport:
        """Insert everything the plan calls for, in one transaction."""
        loaded_at = _now_iso()
        try:
            for planned in report.planned:
                if planned.disposition is Disposition.SKIPPED:
                    continue
                if planned.note_nlp_row is not None:
                    self._insert("note_nlp", planned.note_nlp_row)
                if planned.disposition in (Disposition.WRITTEN, Disposition.FACT_ONLY):
                    assert planned.target is not None and planned.domain_row is not None
                    self._insert(planned.target.table, planned.domain_row)
                self._insert(
                    "nlp_record_ledger",
                    {
                        "record_id": planned.record.record_id,
                        "note_id": planned.resolved_note_id or 0,
                        "source_note_id": str(planned.record.note_id),
                        "span_offset": (
                            planned.record.span.omop_offset
                            if planned.record.span else None
                        ),
                        "lexical_variant": planned.record.lexical_variant,
                        "concept_id": planned.record.concept_id,
                        "domain_id": planned.domain_id,
                        "note_nlp_id": planned.note_nlp_id,
                        "domain_table": planned.target.table if planned.target else None,
                        "domain_row_id": planned.domain_row_id,
                        "nlp_system": planned.record.source.nlp_system,
                        "loaded_at": loaded_at,
                    },
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        report.dry_run = False
        return report

    # ------------------------------------------------------------------ delete

    def unload(self, *, nlp_system: str | None = None) -> dict[str, int]:
        """Reverse a load using the ledger. The reason the ledger exists."""
        where, params = ("WHERE nlp_system = ?", (nlp_system,)) if nlp_system else ("", ())
        rows = self.conn.execute(
            f"SELECT record_id, note_nlp_id, domain_table, domain_row_id "
            f"FROM nlp_record_ledger {where}",
            params,
        ).fetchall()
        deleted: dict[str, int] = {}
        for row in rows:
            if row["note_nlp_id"] is not None:
                self.conn.execute(
                    "DELETE FROM note_nlp WHERE note_nlp_id = ?", (row["note_nlp_id"],)
                )
                deleted["note_nlp"] = deleted.get("note_nlp", 0) + 1
            table, row_id = row["domain_table"], row["domain_row_id"]
            if table and row_id is not None:
                pk = DOMAIN_TARGETS_BY_TABLE[table].pk
                self.conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (row_id,))
                deleted[table] = deleted.get(table, 0) + 1
            self.conn.execute(
                "DELETE FROM nlp_record_ledger WHERE record_id = ?", (row["record_id"],)
            )
        self.conn.commit()
        self._next_ids.clear()
        return deleted

    # ------------------------------------------------------------------ helpers

    def _already_loaded(self, record_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM nlp_record_ledger WHERE record_id = ?", (record_id,)
        ).fetchone()
        return row is not None

    def _allocate(self, table: str, pk: str) -> int:
        """Surrogate keys for NLP rows start above NLP_ID_BASE.

        Keeps them clear of EHR-derived keys and makes an NLP row identifiable
        from its id alone.  Cached in-process so a plan can allocate many ids
        before any INSERT happens.
        """
        if table not in self._next_ids:
            row = self.conn.execute(f"SELECT MAX({pk}) AS m FROM {table}").fetchone()
            current = row["m"] if row and row["m"] is not None else 0
            self._next_ids[table] = max(int(current), NLP_ID_BASE)
        self._next_ids[table] += 1
        return self._next_ids[table]

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        quoted = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )


DOMAIN_TARGETS_BY_TABLE = {t.table: t for t in DOMAIN_TARGETS.values()}


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today() -> str:
    return date.today().isoformat()
