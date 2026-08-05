"""Registering a source ontology as OMOP custom concepts."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omop_nlp_writer.register import (  # noqa: E402
    CUSTOM_ID_BASE,
    CUSTOM_ID_CEILING,
    DEFAULT_DOMAIN,
    SourceConcept,
    VocabularyRegistrar,
    load_bso_ad,
    stable_concept_id,
)
from omop_nlp_writer.vocab import VocabLookup, build_from_csv  # noqa: E402

FIXTURES = ROOT / "fixtures"

# A miniature ontology with the shapes that matter: a root, a mid category, two
# leaves, and a second subtree reusing a label (which BSO-AD does).
ONTOLOGY = {
    "_meta": {"version": "test"},
    "Lifestyle": {
        "concepts": [
            {"id": "00001", "label": "Exercise", "depth": 0},
            {"id": "00002", "label": "Exercise_equipment",
             "parent_label": "Exercise", "depth": 1},
            {"id": "00003", "label": "Treadmill",
             "parent_label": "Exercise_equipment", "depth": 2},
            {"id": "00004", "label": "Rowing_machine",
             "parent_label": "Exercise_equipment", "depth": 2},
        ]
    },
    "Dementia": {
        "concepts": [
            {"id": "00001", "label": "Dementia", "depth": 0},
            {"id": "00002", "label": "Treadmill",
             "parent_label": "Dementia", "depth": 1},
        ]
    },
}


def write_ontology(path: Path, doc: dict | None = None) -> Path:
    path.write_text(json.dumps(doc if doc is not None else ONTOLOGY))
    return path


class TestLoadOntology(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_ontology(Path(self.tmp.name) / "concepts.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_flattens_all_subtrees(self) -> None:
        concepts = load_bso_ad(self.path)
        self.assertEqual(len(concepts), 6)
        self.assertEqual({c.entity_type for c in concepts}, {"Lifestyle", "Dementia"})

    def test_identity_is_entity_type_plus_name_not_the_ontology_id(self) -> None:
        """BSO-AD's own ids collide across subtrees — 40 of them in the real file."""
        concepts = load_bso_ad(self.path)
        by_id = [c for c in concepts if c.source_id == "00001"]
        self.assertEqual(len(by_id), 2, "ontology ids are not unique")
        self.assertEqual(len({c.key for c in concepts}), 6, "keys must be unique")

    def test_same_label_in_two_subtrees_stays_distinct(self) -> None:
        concepts = {c.key: c for c in load_bso_ad(self.path)}
        self.assertIn("Lifestyle|Treadmill", concepts)
        self.assertIn("Dementia|Treadmill", concepts)
        self.assertNotEqual(
            stable_concept_id("Lifestyle|Treadmill", "T"),
            stable_concept_id("Dementia|Treadmill", "T"),
        )

    def test_duplicate_key_is_rejected_not_silently_merged(self) -> None:
        doc = {"A": {"concepts": [
            {"id": "1", "label": "Same", "depth": 0},
            {"id": "2", "label": "Same", "depth": 0},
        ]}}
        path = write_ontology(Path(self.tmp.name) / "dupe.json", doc)
        with self.assertRaises(ValueError) as ctx:
            load_bso_ad(path)
        self.assertIn("collision", str(ctx.exception))

    def test_underscores_become_readable_names(self) -> None:
        concepts = {c.key: c for c in load_bso_ad(self.path)}
        self.assertEqual(concepts["Lifestyle|Rowing_machine"].display_name, "Rowing machine")


class TestStableIds(unittest.TestCase):
    def test_ids_land_in_the_ohdsi_custom_range(self) -> None:
        for key in ("A|B", "Lifestyle|Treadmill", "x" * 200):
            cid = stable_concept_id(key, "BSO-AD")
            self.assertGreaterEqual(cid, CUSTOM_ID_BASE)
            self.assertLess(cid, CUSTOM_ID_CEILING)

    def test_id_is_stable_across_runs(self) -> None:
        """Ids must not shift when the ontology grows — rows already in the CDM
        point at them."""
        a = stable_concept_id("Lifestyle|Treadmill", "BSO-AD")
        b = stable_concept_id("Lifestyle|Treadmill", "BSO-AD")
        self.assertEqual(a, b)

    def test_vocabulary_id_changes_the_id(self) -> None:
        self.assertNotEqual(
            stable_concept_id("A|B", "BSO-AD"), stable_concept_id("A|B", "OTHER")
        )


class RegistrarTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.ontology = write_ontology(tmp / "concepts.json")
        self.registry = tmp / "custom_vocab.db"
        self.concepts = load_bso_ad(self.ontology)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def registrar(self) -> VocabularyRegistrar:
        return VocabularyRegistrar(self.registry, vocabulary_id="BSO-AD")


class TestRegistration(RegistrarTestBase):
    def test_dry_run_writes_nothing(self) -> None:
        with self.registrar() as reg:
            report = reg.register(self.concepts)
            self.assertTrue(report.dry_run)
            self.assertEqual(len(report.inserted), 6)
            n = reg.conn.execute("SELECT COUNT(*) c FROM custom_concept").fetchone()["c"]
            self.assertEqual(n, 0)

    def test_commit_inserts_every_concept(self) -> None:
        with self.registrar() as reg:
            reg.register(self.concepts, commit=True)
            n = reg.conn.execute("SELECT COUNT(*) c FROM custom_concept").fetchone()["c"]
            self.assertEqual(n, 6)

    def test_rerun_is_incremental_not_duplicating(self) -> None:
        """The ontology grows: novel_candidates get promoted into concepts.json."""
        with self.registrar() as reg:
            reg.register(self.concepts, commit=True)
        grown = self.concepts + [
            SourceConcept(entity_type="Lifestyle", concept_name="Stair_climber",
                          source_id="00005", parent_name="Exercise_equipment", depth=2)
        ]
        with self.registrar() as reg:
            report = reg.register(grown, commit=True)
            self.assertEqual(len(report.inserted), 1)
            self.assertEqual(len(report.unchanged), 6)
            n = reg.conn.execute("SELECT COUNT(*) c FROM custom_concept").fetchone()["c"]
            self.assertEqual(n, 7)

    def test_custom_concepts_are_not_marked_standard(self) -> None:
        with self.registrar() as reg:
            reg.register(self.concepts, commit=True)
            rows = reg.conn.execute(
                "SELECT standard_concept FROM custom_concept"
            ).fetchall()
            self.assertTrue(all(r["standard_concept"] is None for r in rows))


class TestAncestors(RegistrarTestBase):
    def ancestors(self) -> list[sqlite3.Row]:
        with self.registrar() as reg:
            reg.register(self.concepts, commit=True)
            return reg.conn.execute(
                "SELECT * FROM custom_concept_ancestor"
            ).fetchall()

    def test_every_concept_is_its_own_ancestor(self) -> None:
        """Without the self-row, 'include descendants' misses the concept itself."""
        rows = self.ancestors()
        selves = [r for r in rows
                  if r["ancestor_concept_id"] == r["descendant_concept_id"]]
        self.assertEqual(len(selves), 6)
        self.assertTrue(all(r["min_levels_of_separation"] == 0 for r in selves))

    def test_grandparent_reaches_grandchild(self) -> None:
        """Exercise -> Exercise_equipment -> Treadmill must be 2 levels."""
        root = stable_concept_id("Lifestyle|Exercise", "BSO-AD")
        leaf = stable_concept_id("Lifestyle|Treadmill", "BSO-AD")
        rows = {(r["ancestor_concept_id"], r["descendant_concept_id"]):
                (r["min_levels_of_separation"], r["max_levels_of_separation"])
                for r in self.ancestors()}
        self.assertIn((root, leaf), rows)
        self.assertEqual(rows[(root, leaf)], (2, 2))

    def test_subtrees_do_not_cross_contaminate(self) -> None:
        dementia_root = stable_concept_id("Dementia|Dementia", "BSO-AD")
        lifestyle_leaf = stable_concept_id("Lifestyle|Treadmill", "BSO-AD")
        pairs = {(r["ancestor_concept_id"], r["descendant_concept_id"])
                 for r in self.ancestors()}
        self.assertNotIn((dementia_root, lifestyle_leaf), pairs)


class TestDomainAssignment(RegistrarTestBase):
    def domains(self, mappings=None) -> dict[str, tuple[str, str]]:
        with self.registrar() as reg:
            reg.register(self.concepts, mappings=mappings or {}, commit=True)
            return {
                r["source_key"]: (r["domain_id"], r["domain_source"])
                for r in reg.conn.execute(
                    "SELECT source_key, domain_id, domain_source FROM custom_concept"
                )
            }

    def test_default_is_observation(self) -> None:
        d = self.domains()
        self.assertEqual(d["Lifestyle|Treadmill"], (DEFAULT_DOMAIN, "default"))

    def test_domain_comes_from_an_omop_mapping_when_there_is_one(self) -> None:
        d = self.domains({"Dementia|Dementia": (4182210, "Condition", "test")})
        self.assertEqual(d["Dementia|Dementia"], ("Condition", "omop_mapping"))

    def test_children_inherit_the_mapped_parents_domain(self) -> None:
        """This is what avoids 660 individual LLM guesses."""
        d = self.domains({"Dementia|Dementia": (4182210, "Condition", "test")})
        self.assertEqual(d["Dementia|Treadmill"], ("Condition", "inherited"))

    def test_grandchild_inherits_through_two_levels(self) -> None:
        d = self.domains({"Lifestyle|Exercise": (4022331, "Measurement", "test")})
        self.assertEqual(d["Lifestyle|Exercise_equipment"], ("Measurement", "inherited"))
        self.assertEqual(d["Lifestyle|Treadmill"], ("Measurement", "inherited"))

    def test_decision_source_is_always_recorded(self) -> None:
        """A wrong domain must be findable later, not silently baked in."""
        for domain, source in self.domains().values():
            self.assertTrue(source)


class TestWriterSeesCustomConcepts(RegistrarTestBase):
    """The writer's vocabulary lookup must resolve registered custom concepts."""

    def setUp(self) -> None:
        super().setUp()
        self.vocab_path = Path(self.tmp.name) / "concept.db"
        build_from_csv(FIXTURES / "vocab_mini.csv", self.vocab_path)
        with self.registrar() as reg:
            reg.register(
                self.concepts,
                mappings={"Dementia|Dementia": (4182210, "Condition", "test")},
                commit=True,
            )

    def lookup(self) -> VocabLookup:
        # The registry is a sibling of the vocabulary, so it is auto-discovered.
        return VocabLookup(self.vocab_path)

    def test_registry_is_discovered(self) -> None:
        v = self.lookup()
        self.assertTrue(v.has_custom_registry)
        v.close()

    def test_source_pair_resolves_to_a_concept_id(self) -> None:
        v = self.lookup()
        cid = v.concept_id_for_source("Lifestyle", "Treadmill")
        self.assertEqual(cid, stable_concept_id("Lifestyle|Treadmill", "BSO-AD"))
        v.close()

    def test_unmapped_custom_concept_is_writable_as_itself(self) -> None:
        """A custom concept is the terminal representation, not a failed mapping."""
        v = self.lookup()
        cid = v.concept_id_for_source("Lifestyle", "Treadmill")
        r = v.resolve(cid)
        self.assertTrue(r.is_writable)
        self.assertTrue(r.is_custom)
        self.assertFalse(r.via_maps_to)
        self.assertEqual(r.standard.concept_id, cid)
        self.assertEqual(r.domain_id, "Observation")
        v.close()

    def test_mapped_custom_concept_resolves_to_the_standard_concept(self) -> None:
        v = self.lookup()
        cid = v.concept_id_for_source("Dementia", "Dementia")
        r = v.resolve(cid)
        self.assertTrue(r.via_maps_to)
        self.assertFalse(r.is_custom)
        self.assertEqual(r.standard.concept_id, 4182210)   # the standard concept
        self.assertEqual(r.source.concept_id, cid)         # custom kept as source
        v.close()

    def test_unknown_source_pair_returns_none(self) -> None:
        v = self.lookup()
        self.assertIsNone(v.concept_id_for_source("Lifestyle", "Never_Seen"))
        v.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
