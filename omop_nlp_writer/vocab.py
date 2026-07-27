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


class VocabLookup:
    """Read-only concept lookups, cached in-process."""

    def __init__(self, db_path: Path):
        if not db_path.exists():
            raise FileNotFoundError(
                f"Vocabulary DB not found at {db_path}. "
                f"Run scripts/build_vocab.py to build the synthetic dev vocab, "
                f"or pass --vocab pointing at a real OMOP concept.db."
            )
        self.db_path = db_path
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._cache: dict[int, ConceptRecord | None] = {}

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
