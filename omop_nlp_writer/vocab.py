"""OMOP vocabulary lookup — resolves concept_id -> domain_id.

Schema-compatible with the computable_phenotype_library concept.db (see that
repo's backend/build_omop_sqlite.py), so pointing this at the real vocabulary is
a single --vocab flag change:

    CREATE TABLE concept (
        concept_id       INTEGER PRIMARY KEY,
        concept_name     TEXT,
        domain_id        TEXT,
        vocabulary_id    TEXT,
        concept_class_id TEXT,
        standard_concept TEXT,
        concept_code     TEXT,
        invalid_reason   TEXT
    );

The bundled fixtures/vocab_mini.csv is a *synthetic* subset for development.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CONCEPT_DDL = """
CREATE TABLE IF NOT EXISTS concept (
    concept_id       INTEGER PRIMARY KEY,
    concept_name     TEXT,
    domain_id        TEXT,
    vocabulary_id    TEXT,
    concept_class_id TEXT,
    standard_concept TEXT,
    concept_code     TEXT,
    invalid_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_concept_vocab_code ON concept(vocabulary_id, concept_code);
CREATE INDEX IF NOT EXISTS idx_concept_domain ON concept(domain_id);
"""


@dataclass(slots=True, frozen=True)
class ConceptRecord:
    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    concept_class_id: str | None
    standard_concept: str | None
    concept_code: str
    invalid_reason: str | None


@dataclass(slots=True, frozen=True)
class Resolution:
    """The outcome of resolving a concept to something writable to a domain table.

    OMOP domain tables must carry *standard* concepts — concept sets expand over
    CONCEPT_ANCESTOR, which only contains standard concepts, so a non-standard
    concept in a domain table is invisible to cohort SQL even though the row
    exists.  Non-standard concepts are therefore followed through the "Maps to"
    relationship, and the original is preserved in *_source_concept_id.
    """

    source: ConceptRecord
    standard: ConceptRecord | None
    via_maps_to: bool

    @property
    def is_writable(self) -> bool:
        return self.standard is not None

    @property
    def domain_id(self) -> str | None:
        """The domain of the concept that will drive routing."""
        return self.standard.domain_id if self.standard else None


class VocabLookup:
    """Read-only concept lookups, cached in-process."""

    def __init__(self, db_path: Path, relationship_db_path: Path | None = None):
        if not db_path.exists():
            raise FileNotFoundError(
                f"Vocabulary DB not found at {db_path}. "
                f"Run `python3 -m omop_nlp_writer build-vocab` for the synthetic dev "
                f"vocab, or pass --vocab pointing at a real OMOP concept.db."
            )
        self.db_path = db_path
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._cache: dict[int, ConceptRecord | None] = {}

        # 'Maps to' relationships live in a separate file.  NOTE: CP's own
        # concept_relationship.db holds ONLY 'Subsumes' rows (that is all its
        # export needs), so it is useless for this purpose — maps_to.db is
        # preferred, and a relationship DB with no 'Maps to' rows is treated as
        # absent rather than silently returning "no mapping" for everything.
        if relationship_db_path is None:
            for candidate in ("maps_to.db", "concept_relationship.db"):
                sibling = db_path.parent / candidate
                if sibling.exists():
                    relationship_db_path = sibling
                    break
        self.relationship_db_path = relationship_db_path
        self._rel_conn: sqlite3.Connection | None = None
        self.relationship_warning: str | None = None
        if relationship_db_path is not None and relationship_db_path.exists():
            conn = sqlite3.connect(f"file:{relationship_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            has_maps_to = conn.execute(
                "SELECT 1 FROM concept_relationship WHERE relationship_id = 'Maps to' LIMIT 1"
            ).fetchone()
            if has_maps_to:
                self._rel_conn = conn
            else:
                conn.close()
                self.relationship_warning = (
                    f"{relationship_db_path.name} contains no 'Maps to' rows "
                    f"(CP's copy holds only 'Subsumes'), so non-standard concepts "
                    f"cannot be resolved. Build maps_to.db from CONCEPT_RELATIONSHIP.csv."
                )
        else:
            self.relationship_warning = (
                "no relationship DB found next to the vocabulary, so non-standard "
                "concepts cannot be resolved to standard ones"
            )
        self._maps_to_cache: dict[int, list[int]] = {}

    @property
    def has_relationships(self) -> bool:
        return self._rel_conn is not None

    # ------------------------------------------------------------- resolution

    def resolve(self, concept_id: int) -> Resolution | None:
        """Resolve a concept to the standard concept a domain row should carry.

        Returns None when the concept is not in the vocabulary at all.
        """
        source = self.find(concept_id)
        if source is None:
            return None
        if source.standard_concept == "S":
            return Resolution(source=source, standard=source, via_maps_to=False)

        for target_id in self.maps_to(concept_id):
            target = self.find(target_id)
            if target is not None and target.standard_concept == "S":
                return Resolution(source=source, standard=target, via_maps_to=True)
        return Resolution(source=source, standard=None, via_maps_to=False)

    def maps_to(self, concept_id: int) -> list[int]:
        """concept_ids this concept 'Maps to'. Empty if unknown or unmapped."""
        if self._rel_conn is None:
            return []
        if concept_id in self._maps_to_cache:
            return self._maps_to_cache[concept_id]
        rows = self._rel_conn.execute(
            """
            SELECT concept_id_2
              FROM concept_relationship
             WHERE concept_id_1 = ?
               AND relationship_id = 'Maps to'
               AND (invalid_reason IS NULL OR invalid_reason = '')
            """,
            (concept_id,),
        ).fetchall()
        targets = [int(r["concept_id_2"]) for r in rows if int(r["concept_id_2"]) != concept_id]
        self._maps_to_cache[concept_id] = targets
        return targets

    def find(self, concept_id: int) -> ConceptRecord | None:
        if concept_id in self._cache:
            return self._cache[concept_id]
        row = self._conn.execute(
            """
            SELECT concept_id, concept_name, domain_id, vocabulary_id,
                   concept_class_id, standard_concept, concept_code, invalid_reason
              FROM concept
             WHERE concept_id = ?
             LIMIT 1
            """,
            (concept_id,),
        ).fetchone()
        record = None if row is None else ConceptRecord(
            concept_id=int(row["concept_id"]),
            concept_name=(row["concept_name"] or "").strip(),
            domain_id=(row["domain_id"] or "").strip(),
            vocabulary_id=(row["vocabulary_id"] or "").strip(),
            concept_class_id=(row["concept_class_id"] or None),
            standard_concept=(row["standard_concept"] or None),
            concept_code=(row["concept_code"] or "").strip(),
            invalid_reason=(row["invalid_reason"] or None),
        )
        self._cache[concept_id] = record
        return record

    def close(self) -> None:
        self._conn.close()
        if self._rel_conn is not None:
            self._rel_conn.close()


def build_from_csv(csv_path: Path, db_path: Path) -> int:
    """Build a concept.db-shaped SQLite from a CSV. Used for the dev vocab."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(CONCEPT_DDL)
    columns = [
        "concept_id",
        "concept_name",
        "domain_id",
        "vocabulary_id",
        "concept_class_id",
        "standard_concept",
        "concept_code",
        "invalid_reason",
    ]
    rows = []
    with csv_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            raw_id = (row.get("concept_id") or "").strip()
            if not raw_id or not raw_id.lstrip("-").isdigit():
                continue  # blank line or a "# comment" row
            rows.append(tuple((row.get(col) or "").strip() or None for col in columns))
    conn.executemany(
        f"INSERT OR REPLACE INTO concept VALUES ({','.join('?' * len(columns))})", rows
    )
    conn.commit()
    conn.close()
    return len(rows)
