"""Register a source ontology into the OMOP vocabulary as custom concepts.

Hongyu approved custom concepts "provided that they are also added to the CONCEPT
table so the pipeline can process them", with the domain "determined by the LLM
or with Observation used as the default".

This does it without an LLM, because the source ontology already carries the
information an LLM would be guessing at:

  * CONCEPT           — one row per ontology concept, concept_id >= 2,000,000,000
                        (the OHDSI-reserved range for local concepts)
  * CONCEPT_ANCESTOR  — generated from the ontology's own parent/child tree, so
                        descendant-based cohort queries can find these concepts.
                        Without these rows a custom concept is present in the CDM
                        but invisible to CP's generated SQL, which expands concept
                        sets by descendant.
  * CONCEPT_RELATIONSHIP — 'Maps to' rows for concepts that DO have a standard
                        OMOP equivalent, so the writer resolves them to the
                        standard concept and keeps the custom one in
                        *_source_concept_id.

Domain assignment, in priority order:
  1. inherited from the nearest ancestor that has an OMOP 'Maps to' (the mapped
     concept's real domain)
  2. inherited from the nearest ancestor with an assigned domain
  3. DEFAULT_DOMAIN ("Observation") — correct for most SDoH

Every concept records which rule decided its domain, so a wrong one is findable
and correctable later rather than silently baked in.

Re-runnable by design: the source ontology grows (BSO-AD promotes
`novel_candidate` spans into concepts.json through its curation workbench), so
registration must be incremental, and identity must be stable across runs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# OHDSI reserves concept_id >= 2 billion for local/custom concepts, so these can
# never collide with a released Athena vocabulary.
CUSTOM_ID_BASE = 2_000_000_000
CUSTOM_ID_CEILING = 2_100_000_000  # keeps room for other local vocabularies

DEFAULT_DOMAIN = "Observation"

# Domains a clinical fact can be written to (mirrors domains.DOMAIN_TARGETS).
WRITABLE_DOMAINS = frozenset(
    {"Observation", "Measurement", "Condition", "Drug", "Procedure", "Device", "Specimen"}
)

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS custom_concept (
    concept_id        INTEGER PRIMARY KEY,
    concept_name      TEXT NOT NULL,
    domain_id         TEXT NOT NULL,
    vocabulary_id     TEXT NOT NULL,
    concept_class_id  TEXT,
    standard_concept  TEXT,
    concept_code      TEXT NOT NULL,
    invalid_reason    TEXT,
    -- provenance beyond the CDM columns
    source_key        TEXT NOT NULL UNIQUE,   -- "<entity_type>|<concept_name>"
    source_id         TEXT,                   -- the ontology's own id (may not be unique)
    parent_key        TEXT,
    depth             INTEGER,
    domain_source     TEXT NOT NULL,          -- which rule decided domain_id
    maps_to           INTEGER,                -- standard OMOP concept, if any
    maps_to_origin    TEXT,                   -- how that mapping was decided
    registered_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_custom_source_key ON custom_concept(source_key);
CREATE INDEX IF NOT EXISTS idx_custom_parent ON custom_concept(parent_key);

CREATE TABLE IF NOT EXISTS custom_concept_ancestor (
    ancestor_concept_id      INTEGER NOT NULL,
    descendant_concept_id    INTEGER NOT NULL,
    min_levels_of_separation INTEGER NOT NULL,
    max_levels_of_separation INTEGER NOT NULL,
    PRIMARY KEY (ancestor_concept_id, descendant_concept_id)
);

CREATE TABLE IF NOT EXISTS custom_concept_relationship (
    concept_id_1    INTEGER NOT NULL,
    concept_id_2    INTEGER NOT NULL,
    relationship_id TEXT NOT NULL,
    invalid_reason  TEXT,
    origin          TEXT,
    PRIMARY KEY (concept_id_1, concept_id_2, relationship_id)
);
CREATE INDEX IF NOT EXISTS idx_ccr_1
    ON custom_concept_relationship(concept_id_1, relationship_id);
"""


# ---------------------------------------------------------------------------
# source ontology
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceConcept:
    """One concept from the source ontology, keyed the way it is actually safe to.

    BSO-AD's own `id` is NOT unique (40 collisions across subtrees), so identity
    is (entity_type, concept_name) — verified unique across all 660 concepts, and
    both fields are present in the NER output, so the adapter can join on them
    without any change to the normalizer.
    """

    entity_type: str
    concept_name: str
    source_id: str | None = None
    parent_name: str | None = None
    depth: int | None = None

    @property
    def key(self) -> str:
        return f"{self.entity_type}|{self.concept_name}"

    @property
    def parent_key(self) -> str | None:
        if not self.parent_name or self.parent_name == self.concept_name:
            return None
        return f"{self.entity_type}|{self.parent_name}"

    @property
    def display_name(self) -> str:
        """Ontology labels use underscores; CONCEPT.concept_name should read."""
        return self.concept_name.replace("_", " ").strip()


def load_bso_ad(path: Path) -> list[SourceConcept]:
    """Flatten BSO-AD concepts.json into SourceConcepts."""
    doc = json.loads(path.read_text())
    out: list[SourceConcept] = []

    def walk(node: object, entity_type: str) -> None:
        if isinstance(node, dict):
            if "id" in node and "label" in node:
                out.append(
                    SourceConcept(
                        entity_type=entity_type,
                        concept_name=str(node["label"]),
                        source_id=str(node["id"]),
                        parent_name=(
                            str(node["parent_label"]) if node.get("parent_label") else None
                        ),
                        depth=node.get("depth"),
                    )
                )
            for value in node.values():
                walk(value, entity_type)
        elif isinstance(node, list):
            for item in node:
                walk(item, entity_type)

    for key, value in doc.items():
        if key == "_meta":
            continue
        walk(value, key)

    # Guard the identity assumption rather than trusting it.
    seen: dict[str, SourceConcept] = {}
    for c in out:
        if c.key in seen:
            raise ValueError(
                f"source ontology key collision: {c.key!r} appears twice — "
                f"(entity_type, concept_name) is no longer a safe identity"
            )
        seen[c.key] = c
    return out


# ---------------------------------------------------------------------------
# stable ids
# ---------------------------------------------------------------------------


def stable_concept_id(source_key: str, vocabulary_id: str) -> int:
    """Deterministic concept_id from the source key.

    Hashing rather than counting: registration must be re-runnable as the
    ontology grows, and a counter would renumber existing concepts whenever a new
    one is inserted mid-list — silently repointing rows already in the CDM.
    """
    digest = hashlib.sha256(f"{vocabulary_id}|{source_key}".encode()).digest()
    span = CUSTOM_ID_CEILING - CUSTOM_ID_BASE
    return CUSTOM_ID_BASE + (int.from_bytes(digest[:8], "big") % span)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RegistrationReport:
    vocabulary_id: str
    inserted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    ancestor_rows: int = 0
    maps_to_rows: int = 0
    domain_counts: dict[str, int] = field(default_factory=dict)
    domain_source_counts: dict[str, int] = field(default_factory=dict)
    id_collisions: list[str] = field(default_factory=list)
    dry_run: bool = True


class VocabularyRegistrar:
    def __init__(self, registry_path: Path, vocabulary_id: str = "BSO-AD"):
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path = registry_path
        self.vocabulary_id = vocabulary_id
        self.conn = sqlite3.connect(registry_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(REGISTRY_DDL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> VocabularyRegistrar:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ plan

    def register(
        self,
        concepts: Iterable[SourceConcept],
        *,
        mappings: dict[str, tuple[int, str, str]] | None = None,
        commit: bool = False,
        now: str = "",
    ) -> RegistrationReport:
        """Register concepts. `mappings` maps source_key -> (standard concept_id,
        that concept's domain_id, origin label)."""
        concepts = list(concepts)
        mappings = mappings or {}
        report = RegistrationReport(vocabulary_id=self.vocabulary_id)

        by_key = {c.key: c for c in concepts}
        ids: dict[str, int] = {}
        for c in concepts:
            cid = stable_concept_id(c.key, self.vocabulary_id)
            if cid in ids.values():
                report.id_collisions.append(c.key)
            ids[c.key] = cid

        domains = self._assign_domains(by_key, mappings)

        existing = {
            r["source_key"]: r
            for r in self.conn.execute(
                "SELECT source_key, concept_id, domain_id FROM custom_concept"
            )
        }

        rows = []
        for c in concepts:
            mapped = mappings.get(c.key)
            domain_id, domain_source = domains[c.key]
            report.domain_counts[domain_id] = report.domain_counts.get(domain_id, 0) + 1
            report.domain_source_counts[domain_source] = (
                report.domain_source_counts.get(domain_source, 0) + 1
            )
            if c.key in existing:
                report.unchanged.append(c.key)
                continue
            report.inserted.append(c.key)
            rows.append(
                (
                    ids[c.key],
                    c.display_name,
                    domain_id,
                    self.vocabulary_id,
                    "Undefined",
                    # Custom concepts are NOT standard: standard_concept stays
                    # NULL so nothing treats them as canonical, and 'Maps to'
                    # carries anything with a real OMOP equivalent.
                    None,
                    c.key,
                    None,
                    c.key,
                    c.source_id,
                    c.parent_key,
                    c.depth,
                    domain_source,
                    mapped[0] if mapped else None,
                    mapped[2] if mapped else None,
                    now,
                )
            )

        ancestors = self._build_ancestors(by_key, ids)
        report.ancestor_rows = len(ancestors)
        maps_to = [
            (ids[key], target, "Maps to", None, origin)
            for key, (target, _dom, origin) in mappings.items()
            if key in ids
        ]
        report.maps_to_rows = len(maps_to)

        if commit:
            self.conn.executemany(
                """INSERT OR REPLACE INTO custom_concept VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO custom_concept_ancestor VALUES (?,?,?,?)",
                ancestors,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO custom_concept_relationship VALUES (?,?,?,?,?)",
                maps_to,
            )
            self.conn.commit()
            report.dry_run = False
        return report

    # -------------------------------------------------------------- domains

    def _assign_domains(
        self,
        by_key: dict[str, SourceConcept],
        mappings: dict[str, tuple[int, str, str]],
    ) -> dict[str, tuple[str, str]]:
        """Domain per concept, plus which rule decided it."""
        resolved: dict[str, tuple[str, str]] = {}

        def resolve(key: str, seen: frozenset[str] = frozenset()) -> tuple[str, str]:
            if key in resolved:
                return resolved[key]
            if key in seen:  # cycle in the source ontology
                return (DEFAULT_DOMAIN, "default_cycle_guard")
            concept = by_key.get(key)
            if concept is None:
                return (DEFAULT_DOMAIN, "default_missing_parent")

            mapped = mappings.get(key)
            if mapped and mapped[1] in WRITABLE_DOMAINS:
                out = (mapped[1], "omop_mapping")
            elif concept.parent_key:
                parent_domain, parent_source = resolve(key=concept.parent_key,
                                                       seen=seen | {key})
                out = (
                    parent_domain,
                    "inherited" if parent_source != "default" else "default",
                )
            else:
                out = (DEFAULT_DOMAIN, "default")
            resolved[key] = out
            return out

        for key in by_key:
            resolve(key)
        return resolved

    # ------------------------------------------------------------ ancestors

    def _build_ancestors(
        self, by_key: dict[str, SourceConcept], ids: dict[str, int]
    ) -> list[tuple[int, int, int, int]]:
        """CONCEPT_ANCESTOR rows from the ontology's parent/child tree.

        OMOP requires a self-row (0 levels) for every concept — without it, a
        concept is not its own descendant and "include descendants" queries miss
        the concept itself, not just its children.
        """
        rows: dict[tuple[int, int], tuple[int, int]] = {}
        for key in by_key:
            cid = ids[key]
            rows[(cid, cid)] = (0, 0)

        for key, concept in by_key.items():
            descendant = ids[key]
            levels = 0
            cursor = concept.parent_key
            seen = {key}
            while cursor and cursor in by_key and cursor not in seen:
                seen.add(cursor)
                levels += 1
                ancestor = ids[cursor]
                pair = (ancestor, descendant)
                if pair in rows:
                    lo, hi = rows[pair]
                    rows[pair] = (min(lo, levels), max(hi, levels))
                else:
                    rows[pair] = (levels, levels)
                cursor = by_key[cursor].parent_key

        return [(a, d, lo, hi) for (a, d), (lo, hi) in rows.items()]

    # --------------------------------------------------------------- lookup

    def concept_id_for(self, entity_type: str, concept_name: str) -> int | None:
        """The join the adapter needs: (entity_type, concept_name) -> concept_id."""
        row = self.conn.execute(
            "SELECT concept_id FROM custom_concept WHERE source_key = ?",
            (f"{entity_type}|{concept_name}",),
        ).fetchone()
        return int(row["concept_id"]) if row else None
