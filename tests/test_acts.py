"""ACTS rubric output -> ExtractionRecords.

Rubric answers differ from NER spans in two ways that carry real risk: the concept
comes from the field rather than the text, and the answer's TYPE decides which OMOP
value column it belongs in.  Generic handling of the second would put a date in a
numeric column, or turn a negative finding into a diagnosis.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omop_nlp_writer.adapters import acts, build_acts_records  # noqa: E402
from omop_nlp_writer.adapters.acts import ActsFormatError  # noqa: E402


def doc(*answers: dict) -> dict:
    return {
        "pipeline": "chart-review-platform",
        "task_id": "acts",
        "note_id": 1001,
        "person_id": 1,
        "note_datetime": "2025-03-14T09:20:00",
        "answers": list(answers),
    }


def answer(field_id: str, value: object, **kw) -> dict:
    ev = kw.pop("evidence", [{"source": "note", "span_offsets": [10, 20],
                              "verbatim_quote": "quoted text"}])
    return {"field_id": field_id, "answer": value, "evidence": ev, **kw}


class ActsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read(self, *answers: dict) -> list[dict]:
        p = Path(self.tmp.name) / "acts.json"
        p.write_text(json.dumps(doc(*answers)))
        return list(acts.read_answers(p))


class TestValueTyping(ActsTestBase):
    def test_score_goes_to_value_as_number(self) -> None:
        (r,) = self.read(answer("mmse_score", 22))
        self.assertEqual(r["value"]["as_number"], 22.0)
        self.assertTrue(r["term_exists"])

    def test_numeric_enums_are_numbers_not_categories(self) -> None:
        """CDR global is 0/0.5/1/2/3 and GDS stage 1-7 — scores, not labels."""
        for field, value in (("cdr_global", 0.5), ("gds_stage", 4)):
            (r,) = self.read(answer(field, value))
            self.assertEqual(r["value"]["as_number"], float(value), field)

    def test_categorical_answer_is_carried_as_a_string(self) -> None:
        """The string is value_source_value; build_acts_records adds the concept."""
        (r,) = self.read(answer("smoking_status", "former"))
        self.assertEqual(r["value"]["as_string"], "former")
        self.assertIsNone(r["value"].get("as_number"))
        self.assertEqual(r["raw_answer"], "former")

    def test_date_answer_is_not_coerced_into_a_number(self) -> None:
        (r,) = self.read(answer("lmp_date", "around 1998"))
        self.assertEqual(r["value"]["as_string"], "around 1998")
        self.assertIsNone(r["value"].get("as_number"))
        self.assertIn("modelling decision", r["term_modifiers"].lower())

    def test_non_numeric_answer_in_a_numeric_field_is_kept_visible(self) -> None:
        """A data problem must not be silently coerced or dropped."""
        (r,) = self.read(answer("mmse_score", "not recorded"))
        self.assertEqual(r["value"]["as_string"], "not recorded")
        self.assertIn("expected a number", r["term_modifiers"])

    def test_list_answer_is_flagged_as_needing_per_element_concepts(self) -> None:
        (r,) = self.read(answer("allergen", ["penicillin", "latex"]))
        self.assertIn("penicillin", r["value"]["as_string"])
        self.assertIn("own concept", r["term_modifiers"])


class TestNegativeFindings(ActsTestBase):
    """The case where generic handling would fabricate a diagnosis."""

    def test_boolean_zero_becomes_a_negative_finding(self) -> None:
        (r,) = self.read(answer("impaired_cognition", 0))
        self.assertFalse(r["term_exists"], "0 must not become a positive fact")
        self.assertIn("negative finding", r["term_modifiers"])

    def test_boolean_one_is_a_positive_finding(self) -> None:
        (r,) = self.read(answer("impaired_cognition", 1))
        self.assertTrue(r["term_exists"])

    def test_string_booleans_are_understood(self) -> None:
        for value, expected in (("no", False), ("No", False), ("yes", True),
                                ("false", False), ("1", True)):
            (r,) = self.read(answer("postmenopause", value))
            self.assertIs(r["term_exists"], expected, value)

    def test_unrecognised_boolean_is_reported_not_guessed_silently(self) -> None:
        (r,) = self.read(answer("impaired_cognition", "probably"))
        self.assertIn("unrecognised boolean", r["term_modifiers"])


class TestSkipping(ActsTestBase):
    def test_computed_fields_are_not_inserted(self) -> None:
        """mmse_severity carries nothing mmse_score does not."""
        rows = self.read(answer("mmse_score", 22), answer("mmse_severity", "mild"),
                         answer("apoe4", 1))
        self.assertEqual(len(rows), 1)

    def test_undocumented_answers_are_skipped(self) -> None:
        self.assertEqual(self.read(answer("mmse_score", None)), [])
        self.assertEqual(self.read(answer("mmse_score", "")), [])

    def test_answers_evidenced_only_from_structured_data_are_dropped(self) -> None:
        """Citing an OMOP row means the fact is already in the CDM."""
        rows = self.read(answer("pack_year", 30, evidence=[
            {"source": "omop", "table": "observation", "row_id": 5}
        ]))
        self.assertEqual(rows, [])

    def test_note_evidence_survives_alongside_omop_evidence(self) -> None:
        rows = self.read(answer("pack_year", 30, evidence=[
            {"source": "omop", "table": "observation", "row_id": 5},
            {"source": "note", "span_offsets": [5, 9], "verbatim_quote": "30 pack"},
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"]["as_number"], 30.0)


class TestEvidenceHandling(ActsTestBase):
    def test_one_record_per_evidence_span(self) -> None:
        """NOTE_NLP is one row per span, so two citations are two records."""
        rows = self.read(answer("mmse_score", 22, evidence=[
            {"source": "note", "span_offsets": [10, 20], "verbatim_quote": "a"},
            {"source": "note", "span_offsets": [50, 60], "verbatim_quote": "b"},
        ]))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["record_id"], rows[1]["record_id"])

    def test_record_id_includes_the_field_so_two_fields_never_collide(self) -> None:
        """Two fields can cite the same span; they are different facts."""
        rows = self.read(answer("mmse_score", 22), answer("moca_score", 25))
        self.assertNotEqual(rows[0]["record_id"], rows[1]["record_id"])

    def test_missing_span_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(ActsFormatError) as ctx:
            self.read(answer("mmse_score", 22, evidence=[{"source": "note"}]))
        self.assertIn("span_offsets", str(ctx.exception))

    def test_quote_becomes_the_lexical_variant(self) -> None:
        (r,) = self.read(answer("mmse_score", 22))
        self.assertEqual(r["lexical_variant"], "quoted text")

    def test_confidence_is_translated_to_a_number(self) -> None:
        (r,) = self.read(answer("mmse_score", 22, confidence="low"))
        self.assertEqual(r["concept_confidence"], 0.4)


class TestFormatGuard(ActsTestBase):
    def test_ner_output_is_rejected_with_a_useful_message(self) -> None:
        p = Path(self.tmp.name) / "ner.json"
        p.write_text(json.dumps({"note_id": 1, "entities": []}))
        with self.assertRaises(ActsFormatError) as ctx:
            list(acts.read_answers(p))
        self.assertIn("chart_review.py", str(ctx.exception))


class FakeNormalizer:
    """Stands in for concept-normalizer without needing the real vocabulary."""

    def __init__(self, mapping: dict, reviewed_no: set[str] | None = None):
        self.mapping = mapping
        self.reviewed_no = reviewed_no or set()
        self.calls: list[str] = []

    def resolve(self, term: str):
        from omop_nlp_writer.normalizer_client import Resolved

        self.calls.append(term)
        if term in self.reviewed_no:
            return Resolved(None, "not_in_target", "checked, nothing suitable")
        if term in self.mapping:
            return Resolved(self.mapping[term], "mapped", "reviewed alias")
        return Resolved(None, "unmapped", "no exact name match")

    def resolve_value(self, field_id: str, answer: object):
        from omop_nlp_writer.normalizer_client import Resolved

        return Resolved(None, "unmapped", "no value concept")

    def resolve_unit(self, field_id: str):
        from omop_nlp_writer.normalizer_client import Resolved

        return Resolved(None, "not_in_target", "unitless")


class TestNormalizerIntegration(ActsTestBase):
    def write(self, *answers: dict) -> Path:
        d = Path(self.tmp.name) / "acts"
        d.mkdir(exist_ok=True)
        (d / "note.json").write_text(json.dumps(doc(*answers)))
        return d

    def test_field_id_is_what_gets_normalized(self) -> None:
        """Not the quoted text — the field already identifies the concept."""
        norm = FakeNormalizer({"mmse_score": 4169175})
        records, _ = build_acts_records(
            self.write(answer("mmse_score", 22)), normalizer=norm
        )
        self.assertEqual(norm.calls, ["mmse_score"])
        self.assertEqual(records[0].concept_id, 4169175)

    def test_each_field_is_resolved_once_however_many_answers(self) -> None:
        norm = FakeNormalizer({"mmse_score": 4169175})
        self.write(answer("mmse_score", 22, evidence=[
            {"source": "note", "span_offsets": [1, 2], "verbatim_quote": "a"},
            {"source": "note", "span_offsets": [3, 4], "verbatim_quote": "b"},
        ]))
        build_acts_records(Path(self.tmp.name) / "acts", normalizer=norm)
        self.assertEqual(len(set(norm.calls)), 1)

    def test_reviewed_nonmapping_is_reported_as_a_decision(self) -> None:
        norm = FakeNormalizer({}, reviewed_no={"mattis_drs"})
        records, warnings = build_acts_records(
            self.write(answer("mattis_drs", 118)), normalizer=norm
        )
        self.assertIsNone(records[0].concept_id)
        self.assertTrue(any("by decision" in w for w in warnings))

    def test_unresolved_field_is_reported_differently_from_a_decision(self) -> None:
        norm = FakeNormalizer({})
        _, warnings = build_acts_records(
            self.write(answer("mmse_score", 22)), normalizer=norm
        )
        self.assertTrue(any("not resolved" in w for w in warnings))
        self.assertFalse(any("by decision" in w for w in warnings))

    def test_one_warning_per_field_not_per_answer(self) -> None:
        """A 29-field rubric over many notes would otherwise bury the signal."""
        norm = FakeNormalizer({})
        _, warnings = build_acts_records(
            self.write(answer("mmse_score", 22, evidence=[
                {"source": "note", "span_offsets": [1, 2], "verbatim_quote": "a"},
                {"source": "note", "span_offsets": [3, 4], "verbatim_quote": "b"},
            ])),
            normalizer=norm,
        )
        self.assertEqual(len(warnings), 1)

    def test_records_still_produced_without_a_normalizer(self) -> None:
        """Degrades to evidence-only rather than dropping the extraction."""
        records, warnings = build_acts_records(
            self.write(answer("mmse_score", 22)), normalizer=None
        )
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].concept_id)
        self.assertTrue(any("no normalizer" in w for w in warnings))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestValueAndUnitConcepts(ActsTestBase):
    """OMOP represents values and units as concepts too, not just as text.

    Without value_as_concept_id a categorical answer is stored but unqueryable —
    a cohort query for former smokers cannot filter on the string "former".
    """

    class Norm(FakeNormalizer):
        def __init__(self, values=None, units=None, **kw):
            super().__init__(**kw)
            self.values = values or {}
            self.units = units or {}
            self.value_calls: list[str] = []

        def resolve_value(self, field_id, answer):
            from omop_nlp_writer.normalizer_client import Resolved

            key = f"{field_id}.{str(answer).strip().lower()}"
            self.value_calls.append(key)
            if key in self.values:
                return Resolved(self.values[key], "mapped", "reviewed alias")
            return Resolved(None, "unmapped", "no value concept")

        def resolve_unit(self, field_id):
            from omop_nlp_writer.normalizer_client import Resolved

            if field_id in self.units:
                return Resolved(self.units[field_id], "mapped", "reviewed alias")
            return Resolved(None, "not_in_target", "unitless")

    def write(self, *answers: dict) -> Path:
        d = Path(self.tmp.name) / "acts"
        d.mkdir(exist_ok=True)
        (d / "note.json").write_text(json.dumps(doc(*answers)))
        return d

    def test_categorical_value_gets_value_as_concept_id(self) -> None:
        norm = self.Norm(mapping={"smoking_status": 43054909},
                         values={"smoking_status.former": 45883458})
        records, _ = build_acts_records(
            self.write(answer("smoking_status", "former")), normalizer=norm
        )
        self.assertEqual(records[0].value.as_concept_id, 45883458)

    def test_the_string_is_kept_as_well_for_value_source_value(self) -> None:
        """The concept drives queries; the original text stays auditable."""
        norm = self.Norm(mapping={"smoking_status": 43054909},
                         values={"smoking_status.former": 45883458})
        records, _ = build_acts_records(
            self.write(answer("smoking_status", "former")), normalizer=norm
        )
        self.assertEqual(records[0].value.as_string, "former")

    def test_value_key_includes_the_field(self) -> None:
        """'former' under smoking_status is not 'former' under anything else."""
        norm = self.Norm(mapping={}, values={})
        build_acts_records(self.write(answer("smoking_status", "former")), normalizer=norm)
        self.assertEqual(norm.value_calls, ["smoking_status.former"])

    def test_unmappable_value_is_reported_as_unqueryable(self) -> None:
        norm = self.Norm(mapping={"apoe_genotype": 37397776}, values={})
        _, warnings = build_acts_records(
            self.write(answer("apoe_genotype", "e3/e4")), normalizer=norm
        )
        self.assertIsNotNone(warnings)
        self.assertTrue(any("cannot filter on the value" in w for w in warnings))

    def test_numeric_field_with_a_unit_gets_unit_concept_id(self) -> None:
        norm = self.Norm(mapping={"education_years": 42528763}, units={"education_years": 9448})
        records, _ = build_acts_records(
            self.write(answer("education_years", 16)), normalizer=norm
        )
        self.assertEqual(records[0].value.unit_concept_id, 9448)
        self.assertEqual(records[0].value.as_number, 16.0)

    def test_unitless_score_gets_no_unit(self) -> None:
        """An MMSE of 22 is not 22 of anything."""
        norm = self.Norm(mapping={"mmse_score": 4169175}, units={})
        records, _ = build_acts_records(
            self.write(answer("mmse_score", 22)), normalizer=norm
        )
        self.assertIsNone(records[0].value.unit_concept_id)

    def test_no_unit_lookup_for_a_non_numeric_answer(self) -> None:
        norm = self.Norm(mapping={"smoking_status": 43054909}, units={"smoking_status": 9448})
        records, _ = build_acts_records(
            self.write(answer("smoking_status", "former")), normalizer=norm
        )
        self.assertIsNone(records[0].value.unit_concept_id)
