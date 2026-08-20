"""End-to-end tests over a throwaway CDM + synthetic vocabulary."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omop_nlp_writer import cli  # noqa: E402
from omop_nlp_writer.adapters import build_records  # noqa: E402
from omop_nlp_writer.cdm import connect, init_schema  # noqa: E402
from omop_nlp_writer.domains import NLP_ID_BASE, NLP_TYPE_CONCEPT_ID  # noqa: E402
from omop_nlp_writer.record import ContractError, ExtractionRecord  # noqa: E402
from omop_nlp_writer.vocab import VocabLookup, build_from_csv  # noqa: E402
from omop_nlp_writer.writer import CdmNlpWriter, Disposition, Reason  # noqa: E402

FIXTURES = ROOT / "fixtures"


def base_record(**overrides) -> dict:
    data = {
        "record_id": "t:1001:130-134:42869861",
        "note_id": 1001,
        "person_id": 1,
        "span": {"start": 130, "end": 134},
        "lexical_variant": "MMSE",
        "snippet": "... MMSE 22/30 ...",
        "note_datetime": "2025-03-14T09:20:00",
        "term_exists": True,
        "concept_id": 42869861,
        "concept_confidence": 0.97,
        "value": {"as_number": 22.0},
        "source": {"pipeline": "test", "version": "1", "normalizer": "n@1"},
    }
    data.update(overrides)
    return data


class WriterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.cdm_path = tmp / "cdm.db"
        self.vocab_path = tmp / "vocab.db"
        build_from_csv(FIXTURES / "vocab_mini.csv", self.vocab_path)

        # Use the real init-cdm path so the tests exercise the same note/person
        # key allocation and note_source_value crosswalk that production uses.
        rc = cli.main(
            [
                "init-cdm",
                "--cdm", str(self.cdm_path),
                "--notes", str(FIXTURES / "notes" / "notes.json"),
            ]
        )
        assert rc == 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def writer(self, **kwargs) -> CdmNlpWriter:
        return CdmNlpWriter(self.cdm_path, self.vocab_path, **kwargs)

    def plan_one(self, record_dict: dict, **kwargs):
        with self.writer(**kwargs) as w:
            report = w.plan([ExtractionRecord.from_dict(record_dict)])
        return report.planned[0]


class TestContract(unittest.TestCase):
    def test_missing_required_field_is_rejected(self) -> None:
        bad = base_record()
        del bad["note_id"]
        with self.assertRaises(ContractError) as ctx:
            ExtractionRecord.from_dict(bad)
        self.assertIn("note_id", str(ctx.exception))

    def test_domain_id_in_input_is_ignored_not_trusted(self) -> None:
        """The vocabulary is the only source of truth for domain routing."""
        record = ExtractionRecord.from_dict(base_record(domain_id="Condition"))
        self.assertFalse(hasattr(record, "domain_id"))

    def test_concept_id_zero_counts_as_unmapped(self) -> None:
        self.assertFalse(ExtractionRecord.from_dict(base_record(concept_id=0)).is_normalized)
        self.assertFalse(ExtractionRecord.from_dict(base_record(concept_id=None)).is_normalized)

    def test_offset_is_a_string_per_cdm_54(self) -> None:
        record = ExtractionRecord.from_dict(base_record())
        self.assertEqual(record.span.omop_offset, "130-134")
        self.assertIsInstance(record.span.omop_offset, str)

    def test_event_date_requires_a_date_source(self) -> None:
        record = ExtractionRecord.from_dict(base_record(note_datetime=None))
        with self.assertRaises(ContractError):
            _ = record.event_date


class TestRouting(WriterTestBase):
    def test_email_example_routes_mmse_to_observation(self) -> None:
        """MMSE = 22 / concept 42869861 -> OBSERVATION.value_as_number = 22."""
        planned = self.plan_one(base_record())
        self.assertIs(planned.disposition, Disposition.WRITTEN)
        self.assertEqual(planned.domain_id, "Observation")
        self.assertEqual(planned.target.table, "observation")
        self.assertEqual(planned.domain_row["observation_concept_id"], 42869861)
        self.assertEqual(planned.domain_row["value_as_number"], 22.0)

    def test_domain_row_carries_nlp_provenance(self) -> None:
        planned = self.plan_one(base_record())
        self.assertEqual(
            planned.domain_row["observation_type_concept_id"], NLP_TYPE_CONCEPT_ID
        )
        self.assertGreater(planned.domain_row["observation_id"], NLP_ID_BASE)

    def test_condition_concept_routes_to_condition_occurrence(self) -> None:
        planned = self.plan_one(base_record(concept_id=4182210, value={}))
        self.assertEqual(planned.target.table, "condition_occurrence")

    def test_negated_mention_gets_note_nlp_but_no_domain_row(self) -> None:
        planned = self.plan_one(base_record(term_exists=False))
        self.assertIs(planned.disposition, Disposition.NOTE_NLP_ONLY)
        self.assertIs(planned.reason, Reason.NEGATED)
        self.assertEqual(planned.note_nlp_row["term_exists"], "N")
        # The concept is still recorded as evidence.
        self.assertEqual(planned.note_nlp_row["note_nlp_concept_id"], 42869861)

    def test_unmapped_mention_gets_note_nlp_with_concept_zero(self) -> None:
        planned = self.plan_one(base_record(concept_id=None))
        self.assertIs(planned.disposition, Disposition.NOTE_NLP_ONLY)
        self.assertIs(planned.reason, Reason.UNMAPPED)
        self.assertEqual(planned.note_nlp_row["note_nlp_concept_id"], 0)

    def test_visit_domain_is_not_routed(self) -> None:
        planned = self.plan_one(base_record(concept_id=9201, value={}))
        self.assertIs(planned.disposition, Disposition.NOTE_NLP_ONLY)
        self.assertIs(planned.reason, Reason.DOMAIN_NOT_ROUTED)

    def test_concept_absent_from_vocabulary_is_not_guessed(self) -> None:
        planned = self.plan_one(base_record(concept_id=999_999_999))
        self.assertIs(planned.reason, Reason.CONCEPT_NOT_IN_VOCAB)

    def test_confidence_floor_gates_the_domain_row(self) -> None:
        planned = self.plan_one(base_record(concept_confidence=0.31), confidence_threshold=0.5)
        self.assertIs(planned.reason, Reason.LOW_CONFIDENCE)
        # ...and is off by default.
        self.assertIs(self.plan_one(base_record(concept_confidence=0.31)).reason, Reason.OK)


class TestForeignKeys(WriterTestBase):
    def test_unknown_note_id_is_skipped_not_inserted(self) -> None:
        """NOTE_NLP.note_id is a FK — the ID-crosswalk failure mode."""
        planned = self.plan_one(base_record(note_id=999))
        self.assertIs(planned.disposition, Disposition.SKIPPED)
        self.assertIs(planned.reason, Reason.NOTE_NOT_FOUND)

    def test_person_note_mismatch_is_skipped(self) -> None:
        planned = self.plan_one(base_record(note_id=1001, person_id=2))
        self.assertIs(planned.reason, Reason.PERSON_MISMATCH)


class TestNoteIdCrosswalk(WriterTestBase):
    """The platform emits string note ids; CDM NOTE.note_id is an integer."""

    STRING_ID = "2025-05-02__memory_clinic"

    def test_string_note_id_resolves_via_note_source_value(self) -> None:
        planned = self.plan_one(
            base_record(note_id=self.STRING_ID, span={"start": 112, "end": 116})
        )
        self.assertIs(planned.disposition, Disposition.WRITTEN)
        self.assertIsInstance(planned.resolved_note_id, int)
        self.assertEqual(planned.note_nlp_row["note_id"], planned.resolved_note_id)
        # The integer key, not the string, lands in NOTE_NLP.
        self.assertNotEqual(planned.note_nlp_row["note_id"], self.STRING_ID)

    def test_numeric_string_resolves_by_source_value_never_by_coercion(self) -> None:
        """A numeric string is a source value, not a CDM key.

        The string-id note was allocated CDM key 1 with
        note_source_value="2025-05-02__memory_clinic".  So int 1 finds it by key,
        while the string "1" must NOT — nothing has that source value.  This is
        the distinction that would collapse if we coerced digit strings to ints.
        """
        by_key = self.plan_one(base_record(note_id=1, span={"start": 112, "end": 116}))
        self.assertEqual(by_key.resolved_note_id, 1)

        as_source_value = self.plan_one(base_record(note_id="1"))
        self.assertIs(as_source_value.reason, Reason.NOTE_NOT_FOUND)

    def test_unknown_string_id_reports_the_crosswalk_gap(self) -> None:
        planned = self.plan_one(base_record(note_id="2099-01-01__nonexistent"))
        self.assertIs(planned.reason, Reason.NOTE_NOT_FOUND)
        self.assertIn("note_source_value", planned.detail)

    def test_string_person_id_resolves_via_person_source_value(self) -> None:
        planned = self.plan_one(base_record(person_id="SYNTH-001"))
        self.assertIs(planned.disposition, Disposition.WRITTEN)
        self.assertEqual(planned.resolved_person_id, 1)
        self.assertEqual(planned.domain_row["person_id"], 1)

    def test_unknown_string_person_is_skipped(self) -> None:
        planned = self.plan_one(base_record(person_id="SYNTH-999"))
        self.assertIs(planned.reason, Reason.PERSON_NOT_FOUND)

    def test_ledger_keeps_the_upstream_id(self) -> None:
        record = base_record(note_id=self.STRING_ID, span={"start": 112, "end": 116})
        with self.writer() as w:
            w.execute(w.plan([ExtractionRecord.from_dict(record)]))
            row = w.conn.execute(
                "SELECT note_id, source_note_id FROM nlp_record_ledger"
            ).fetchone()
            self.assertEqual(row["source_note_id"], self.STRING_ID)
            self.assertIsInstance(row["note_id"], int)


class TestStandardConceptResolution(WriterTestBase):
    """Domain tables must carry STANDARD concepts.

    Concept sets expand over CONCEPT_ANCESTOR, which holds only standard
    concepts, so a non-standard concept in a domain table produces a row that
    cohort SQL can never match.
    """

    NON_STANDARD = 4022331   # 'Connection of vena cava ... NOS', SNOMED, non-standard
    ITS_STANDARD = 4022329   # what it 'Maps to'

    def setUp(self) -> None:
        super().setUp()
        # Extend the synthetic vocab with a non-standard concept and its mapping.
        conn = sqlite3.connect(self.vocab_path)
        conn.execute(
            "INSERT OR REPLACE INTO concept VALUES (?,?,?,?,?,?,?,?)",
            (self.NON_STANDARD, "Connection of vena cava NOS", "Procedure",
             "SNOMED", "Procedure", None, "X1", None),
        )
        conn.execute(
            "INSERT OR REPLACE INTO concept VALUES (?,?,?,?,?,?,?,?)",
            (self.ITS_STANDARD, "Connection of vena cava", "Procedure",
             "SNOMED", "Procedure", "S", "X2", None),
        )
        conn.commit()
        conn.close()

        # Deliberately NOT a sibling of vocab.db: VocabLookup auto-discovers a
        # sibling maps_to.db, and one test needs the "no mapping available" path.
        rel_dir = Path(self.tmp.name) / "rel"
        rel_dir.mkdir()
        self.maps_to_path = rel_dir / "maps_to.db"
        rel = sqlite3.connect(self.maps_to_path)
        rel.execute(
            """CREATE TABLE concept_relationship (
                   concept_id_1 INTEGER, concept_id_2 INTEGER,
                   relationship_id TEXT, invalid_reason TEXT)"""
        )
        rel.execute(
            "INSERT INTO concept_relationship VALUES (?,?,'Maps to',NULL)",
            (self.NON_STANDARD, self.ITS_STANDARD),
        )
        rel.commit()
        rel.close()

    def test_non_standard_resolves_through_maps_to(self) -> None:
        planned = self.plan_one(
            base_record(concept_id=self.NON_STANDARD, value={}),
            relationship_path=self.maps_to_path,
        )
        self.assertIs(planned.disposition, Disposition.WRITTEN)
        # The row carries the STANDARD concept...
        self.assertEqual(
            planned.domain_row["procedure_concept_id"], self.ITS_STANDARD
        )
        # ...and preserves the original for audit.
        self.assertEqual(
            planned.domain_row["procedure_source_concept_id"], self.NON_STANDARD
        )
        # Domain comes from the standard concept.
        self.assertEqual(planned.domain_id, "Procedure")

    def test_standard_concept_sets_no_source_concept_id(self) -> None:
        planned = self.plan_one(base_record(), relationship_path=self.maps_to_path)
        self.assertNotIn("observation_source_concept_id", planned.domain_row)

    def test_non_standard_without_mapping_is_evidence_only(self) -> None:
        """No maps_to.db available -> refuse rather than write an unqueryable row."""
        planned = self.plan_one(base_record(concept_id=self.NON_STANDARD, value={}))
        self.assertIs(planned.disposition, Disposition.NOTE_NLP_ONLY)
        self.assertIs(planned.reason, Reason.NO_STANDARD_MAPPING)
        self.assertIn("invisible to cohort SQL", planned.detail)

    def test_subsumes_only_relationship_db_is_treated_as_absent(self) -> None:
        """CP's concept_relationship.db has only 'Subsumes' — must not look usable."""
        subsumes_only = Path(self.tmp.name) / "concept_relationship.db"
        rel = sqlite3.connect(subsumes_only)
        rel.execute(
            """CREATE TABLE concept_relationship (
                   concept_id_1 INTEGER, concept_id_2 INTEGER,
                   relationship_id TEXT, invalid_reason TEXT)"""
        )
        rel.execute("INSERT INTO concept_relationship VALUES (1,2,'Subsumes',NULL)")
        rel.commit()
        rel.close()
        lookup = VocabLookup(self.vocab_path, subsumes_only)
        self.assertFalse(lookup.has_relationships)
        self.assertIn("no 'Maps to' rows", lookup.relationship_warning)
        lookup.close()


class TestOmopSourcedEvidenceIsRefused(WriterTestBase):
    """Rubric evidence can cite a structured row the extractor READ.

    Writing that back would re-insert existing EHR data as NLP-derived — the
    exact contamination the provenance flag exists to prevent.
    """

    def test_omop_sourced_record_writes_nothing_at_all(self) -> None:
        planned = self.plan_one(base_record(evidence_source="omop"))
        self.assertIs(planned.disposition, Disposition.SKIPPED)
        self.assertIs(planned.reason, Reason.NOT_NOTE_DERIVED)
        # Not even a NOTE_NLP row: it was never in a note.
        self.assertIsNone(planned.note_nlp_row)

    def test_note_sourced_and_unset_are_both_accepted(self) -> None:
        self.assertIs(
            self.plan_one(base_record(evidence_source="note")).disposition,
            Disposition.WRITTEN,
        )
        self.assertIs(
            self.plan_one(base_record()).disposition, Disposition.WRITTEN
        )

    def test_omop_sourced_record_survives_execute_without_inserting(self) -> None:
        with self.writer() as w:
            w.execute(w.plan([ExtractionRecord.from_dict(base_record(evidence_source="omop"))]))
            for table in ("note_nlp", "observation", "nlp_record_ledger"):
                self.assertEqual(
                    w.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"],
                    0,
                    f"{table} should be untouched",
                )


class TestExecuteAndIdempotency(WriterTestBase):
    def test_dry_run_writes_nothing(self) -> None:
        with self.writer() as w:
            w.plan([ExtractionRecord.from_dict(base_record())])
            count = w.conn.execute("SELECT COUNT(*) c FROM note_nlp").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_commit_then_rerun_is_a_no_op(self) -> None:
        record = ExtractionRecord.from_dict(base_record())
        with self.writer() as w:
            w.execute(w.plan([record]))
        with self.writer() as w:
            report = w.plan([ExtractionRecord.from_dict(base_record())])
            self.assertIs(report.planned[0].reason, Reason.ALREADY_LOADED)
            w.execute(report)
            self.assertEqual(
                w.conn.execute("SELECT COUNT(*) c FROM note_nlp").fetchone()["c"], 1
            )
            self.assertEqual(
                w.conn.execute("SELECT COUNT(*) c FROM observation").fetchone()["c"], 1
            )

    def test_unload_reverses_the_load(self) -> None:
        with self.writer() as w:
            w.execute(w.plan([ExtractionRecord.from_dict(base_record())]))
        with self.writer() as w:
            deleted = w.unload()
            self.assertEqual(deleted, {"note_nlp": 1, "observation": 1})
            for table in ("note_nlp", "observation", "nlp_record_ledger"):
                self.assertEqual(
                    w.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"], 0
                )
            # The notes themselves must survive an unload.
            self.assertEqual(w.conn.execute("SELECT COUNT(*) c FROM note").fetchone()["c"], 4)


class TestAdapterJoin(unittest.TestCase):
    def test_two_pipelines_join_on_note_and_span(self) -> None:
        records, warnings = build_records(
            FIXTURES / "chart_review_output", FIXTURES / "normalizer_output"
        )
        self.assertEqual(len(records), 12)
        by_variant = {(r.note_id, r.lexical_variant): r for r in records}
        mmse = by_variant[(1001, "MMSE")]
        self.assertEqual(mmse.concept_id, 42869861)
        self.assertEqual(mmse.value.as_number, 22.0)
        self.assertEqual(mmse.source.normalizer, "bso-normalizer@0.2.0")
        # Unmapped mention survives the join with concept_id None.
        self.assertIsNone(by_variant[(1002, "Frailty")].concept_id)
        self.assertTrue(any("Frailty" in w for w in warnings))

    def test_ner_only_still_produces_records(self) -> None:
        records, _ = build_records(FIXTURES / "chart_review_output", None)
        self.assertEqual(len(records), 12)
        self.assertTrue(all(r.concept_id is None for r in records))

    def test_negation_comes_through_from_the_assertion_field(self) -> None:
        records, _ = build_records(
            FIXTURES / "chart_review_output", FIXTURES / "normalizer_output"
        )
        delirium = next(r for r in records if r.lexical_variant == "delirium")
        self.assertFalse(delirium.term_exists)
        self.assertIn("assertion=absent", delirium.term_modifiers)

    def test_offsets_match_the_actual_note_text(self) -> None:
        """Guards against fixture drift between notes and extractions."""
        notes = {
            n["note_id"]: n["note_text"]
            for n in json.loads((FIXTURES / "notes" / "notes.json").read_text())["notes"]
        }
        records, _ = build_records(
            FIXTURES / "chart_review_output", FIXTURES / "normalizer_output"
        )
        for r in records:
            text = notes[r.note_id]
            self.assertEqual(
                text[r.span.start : r.span.end],
                r.lexical_variant,
                f"offset drift for {r.record_id}",
            )


class TestCli(WriterTestBase):
    def test_full_load_via_cli(self) -> None:
        rc = cli.main(
            [
                "load",
                "--cdm", str(self.cdm_path),
                "--vocab", str(self.vocab_path),
                "--chart-review", str(FIXTURES / "chart_review_output"),
                "--normalizer", str(FIXTURES / "normalizer_output"),
                "--min-confidence", "0.5",
                "--commit",
            ]
        )
        self.assertEqual(rc, 0)
        conn = connect(self.cdm_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM note_nlp").fetchone()["c"], 11)
        nlp_rows = conn.execute(
            "SELECT COUNT(*) c FROM observation WHERE observation_type_concept_id = ?",
            (NLP_TYPE_CONCEPT_ID,),
        ).fetchone()["c"]
        # 3x MMSE + 1x fall. "Fall" (436583) is an Observation in OMOP, not a
        # Condition — a good reminder that the vocabulary decides the domain.
        self.assertEqual(nlp_rows, 4)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
