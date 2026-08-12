#!/usr/bin/env python3
"""Prove the loop closes: NLP fact -> CDM -> OHDSI cohort SQL finds the patient.

Two steps, because CirceR lives on the CP server rather than this machine:

  1. `prepare` (here)  — materialise CONCEPT / CONCEPT_ANCESTOR / COHORT into the
     demo CDM and emit a Circe cohort expression JSON.
  2. `run` (here)      — execute the SQL that CirceR generated from that JSON.

The cohort is deliberately defined on a PARENT concept with descendants included:

    Observation of "Exercise equipment" (custom) OR ANY DESCENDANT

Nothing in the CDM carries that parent concept — the patient only has a
`Treadmill` observation.  So the cohort can only return anyone if the
CONCEPT_ANCESTOR rows generated from BSO-AD's own hierarchy are correct.  It is a
test of the thing most likely to fail silently.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))



# Mirrors concept-normalizer's stable_concept_id. Duplicated in this demo script
# so the writer's repo does not depend on the normalizer package just to compute a
# concept id it already has in the registry.
def stable_concept_id(source_key: str, vocabulary_id: str) -> int:
    import hashlib

    digest = hashlib.sha256(f"{vocabulary_id}|{source_key}".encode()).digest()
    return 2_000_000_000 + (int.from_bytes(digest[:8], "big") % 100_000_000)


VOCAB_DDL = """
CREATE TABLE IF NOT EXISTS concept (
    concept_id       INTEGER PRIMARY KEY,
    concept_name     TEXT,
    domain_id        TEXT,
    vocabulary_id    TEXT,
    concept_class_id TEXT,
    standard_concept TEXT,
    concept_code     TEXT,
    invalid_reason   TEXT,
    valid_start_date TEXT,
    valid_end_date   TEXT
);
CREATE TABLE IF NOT EXISTS concept_ancestor (
    ancestor_concept_id      INTEGER NOT NULL,
    descendant_concept_id    INTEGER NOT NULL,
    min_levels_of_separation INTEGER,
    max_levels_of_separation INTEGER,
    PRIMARY KEY (ancestor_concept_id, descendant_concept_id)
);
CREATE TABLE IF NOT EXISTS concept_relationship (
    concept_id_1    INTEGER NOT NULL,
    concept_id_2    INTEGER NOT NULL,
    relationship_id TEXT NOT NULL,
    invalid_reason  TEXT
);
CREATE TABLE IF NOT EXISTS concept_synonym (
    concept_id   INTEGER NOT NULL,
    concept_synonym_name TEXT,
    language_concept_id  INTEGER
);
CREATE TABLE IF NOT EXISTS cohort (
    cohort_definition_id INTEGER NOT NULL,
    subject_id           INTEGER NOT NULL,
    cohort_start_date    TEXT NOT NULL,
    cohort_end_date      TEXT NOT NULL
);
"""

VOCAB_ID = "BSO-AD"
PARENT_KEY = "Element_Relevant_to_Behavior_and_Lifestyle|Exercise_Equipment"


def cmd_prepare(args: argparse.Namespace) -> int:
    cdm = sqlite3.connect(args.cdm)
    cdm.row_factory = sqlite3.Row
    cdm.executescript(VOCAB_DDL)

    reg = sqlite3.connect(f"file:{args.registry}?mode=ro", uri=True)
    reg.row_factory = sqlite3.Row

    # --- copy the custom vocabulary in -------------------------------------
    n_concept = 0
    for r in reg.execute(
        """SELECT concept_id, concept_name, domain_id, vocabulary_id,
                  concept_class_id, standard_concept, concept_code, invalid_reason
             FROM custom_concept"""
    ):
        cdm.execute(
            """INSERT OR REPLACE INTO concept
               (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
                standard_concept, concept_code, invalid_reason,
                valid_start_date, valid_end_date)
               VALUES (?,?,?,?,?,?,?,?, '1970-01-01', '2099-12-31')""",
            tuple(r),
        )
        n_concept += 1

    n_anc = 0
    for r in reg.execute("SELECT * FROM custom_concept_ancestor"):
        cdm.execute(
            "INSERT OR REPLACE INTO concept_ancestor VALUES (?,?,?,?)", tuple(r)
        )
        n_anc += 1

    n_rel = 0
    for r in reg.execute(
        """SELECT concept_id_1, concept_id_2, relationship_id, invalid_reason
             FROM custom_concept_relationship"""
    ):
        cdm.execute(
            "INSERT OR REPLACE INTO concept_relationship VALUES (?,?,?,?)", tuple(r)
        )
        n_rel += 1

    # --- plus the standard concepts actually referenced by CDM rows ---------
    referenced = set()
    for table, col in (
        ("observation", "observation_concept_id"),
        ("condition_occurrence", "condition_concept_id"),
        ("measurement", "measurement_concept_id"),
        ("drug_exposure", "drug_concept_id"),
    ):
        for r in cdm.execute(f"SELECT DISTINCT {col} AS c FROM {table}"):
            if r["c"]:
                referenced.add(int(r["c"]))
    missing = {
        c for c in referenced
        if not cdm.execute("SELECT 1 FROM concept WHERE concept_id=?", (c,)).fetchone()
    }
    if missing:
        src = sqlite3.connect(f"file:{args.vocab}?mode=ro", uri=True)
        for cid in missing:
            row = src.execute(
                """SELECT concept_id, concept_name, domain_id, vocabulary_id,
                          concept_class_id, standard_concept, concept_code, invalid_reason
                     FROM concept WHERE concept_id = ?""",
                (cid,),
            ).fetchone()
            if row:
                cdm.execute(
                    """INSERT OR REPLACE INTO concept VALUES
                       (?,?,?,?,?,?,?,?, '1970-01-01','2099-12-31')""",
                    tuple(row),
                )
                # A standard concept is its own ancestor; without the self-row a
                # descendant-expanded concept set misses the concept itself.
                cdm.execute(
                    "INSERT OR REPLACE INTO concept_ancestor VALUES (?,?,0,0)",
                    (cid, cid),
                )
        src.close()
    cdm.commit()

    parent_id = stable_concept_id(PARENT_KEY, VOCAB_ID)
    descendants = cdm.execute(
        """SELECT c.concept_id, c.concept_name, a.min_levels_of_separation AS lvl
             FROM concept_ancestor a JOIN concept c ON c.concept_id = a.descendant_concept_id
            WHERE a.ancestor_concept_id = ? ORDER BY lvl, c.concept_name""",
        (parent_id,),
    ).fetchall()

    print(f"[prepare] vocabulary into {args.cdm.name}: {n_concept} concepts, "
          f"{n_anc} ancestor rows, {n_rel} relationships")
    print(f"[prepare] + {len(missing)} standard concepts referenced by CDM rows")
    print(f"\n[prepare] cohort will be defined on:")
    print(f"          {parent_id}  Exercise equipment  (custom, BSO-AD)")
    print(f"[prepare] its {len(descendants)} descendants in the vocabulary:")
    for d in descendants:
        print(f"            level {d['lvl']}  {d['concept_id']}  {d['concept_name']}")

    # --- the cohort expression --------------------------------------------
    expression = {
        "ConceptSets": [
            {
                "id": 0,
                "name": "Exercise equipment (BSO-AD, with descendants)",
                "expression": {
                    "items": [
                        {
                            "concept": {
                                "CONCEPT_ID": parent_id,
                                "CONCEPT_NAME": "Exercise equipment",
                                "STANDARD_CONCEPT": "S",
                                "CONCEPT_CODE": PARENT_KEY,
                                "DOMAIN_ID": "Observation",
                                "VOCABULARY_ID": VOCAB_ID,
                                "CONCEPT_CLASS_ID": "Undefined",
                                "INVALID_REASON": "V",
                            },
                            # The whole point: rely on descendant expansion.
                            "includeDescendants": True,
                            "includeMapped": False,
                            "isExcluded": False,
                        }
                    ]
                },
            }
        ],
        "PrimaryCriteria": {
            "CriteriaList": [{"Observation": {"CodesetId": 0}}],
            "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
            "PrimaryCriteriaLimit": {"Type": "All"},
        },
        "QualifiedLimit": {"Type": "All"},
        "ExpressionLimit": {"Type": "All"},
        "InclusionRules": [],
        "CensoringCriteria": [],
        "CollapseSettings": {"CollapseType": "ERA", "EraPad": 0},
        "cdmVersionRange": ">=5.0.0",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(expression, indent=2) + "\n")
    print(f"\n[prepare] cohort expression -> {args.out}")
    cdm.close()
    reg.close()
    return 0


# SqlRender's SQLite dialect renders every date operation as
#   CAST(STRFTIME('%s', DATETIME(col, 'unixepoch', ...)) AS REAL)
# i.e. it assumes date columns hold NUMERIC Unix epoch seconds, which is how
# DatabaseConnector loads a SQLite CDM.  This writer stores ISO text (readable,
# and what Postgres wants), so comparisons silently match nothing — the query
# runs clean and returns zero rows.  For the demo we convert a COPY of the CDM to
# epoch dates rather than patching the generated SQL, so what runs is genuinely
# what OHDSI tooling would run.  A Postgres CDM never hits this.
DATE_COLUMNS = {
    "observation": ("observation_date",),
    "observation_period": (
        "observation_period_start_date",
        "observation_period_end_date",
    ),
    "condition_occurrence": ("condition_start_date", "condition_end_date"),
    "measurement": ("measurement_date",),
    "drug_exposure": ("drug_exposure_start_date", "drug_exposure_end_date"),
    "procedure_occurrence": ("procedure_date",),
    "device_exposure": ("device_exposure_start_date", "device_exposure_end_date"),
    "specimen": ("specimen_date",),
    "note": ("note_date",),
}


def _to_epoch_copy(src: Path, dst: Path) -> Path:
    """Copy the CDM with date columns as epoch seconds (SqlRender/SQLite convention)."""
    import shutil

    if dst.exists():
        dst.unlink()
    shutil.copy(src, dst)
    conn = sqlite3.connect(dst)
    for table, columns in DATE_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        for col in columns:
            conn.execute(
                f"UPDATE {table} SET {col} = CAST(STRFTIME('%s', {col}) AS INTEGER) "
                f"WHERE {col} IS NOT NULL AND {col} LIKE '____-__-__%'"
            )
    conn.commit()
    conn.close()
    return dst


def cmd_run(args: argparse.Namespace) -> int:
    sql = args.sql.read_text()
    epoch_cdm = _to_epoch_copy(args.cdm, args.cdm.with_name("cohort_demo_epoch.db"))
    print(f"[run] date columns converted to epoch seconds in {epoch_cdm.name}")
    cdm = sqlite3.connect(epoch_cdm)
    cdm.row_factory = sqlite3.Row
    cdm.execute("DELETE FROM cohort")
    cdm.commit()

    statements = [s.strip() for s in sql.split(";") if s.strip()]
    print(f"[run] executing {len(statements)} statements from {args.sql.name}")
    for i, stmt in enumerate(statements, 1):
        try:
            cdm.execute(stmt)
        except sqlite3.Error as exc:
            print(f"[run] statement {i} failed: {exc}")
            print(f"       {stmt[:300]}")
            if not args.keep_going:
                return 1
    cdm.commit()

    rows = cdm.execute(
        """SELECT co.subject_id,
                  DATE(co.cohort_start_date, 'unixepoch') AS start_date,
                  DATE(co.cohort_end_date, 'unixepoch')   AS end_date,
                  p.person_source_value
             FROM cohort co LEFT JOIN person p ON p.person_id = co.subject_id
            ORDER BY co.subject_id"""
    ).fetchall()
    print(f"\n{'=' * 66}")
    print(f"COHORT RESULT: {len(rows)} patient(s)")
    print(f"{'=' * 66}")
    for r in rows:
        print(f"  person_id={r['subject_id']}  ({r['person_source_value']})  "
              f"{r['start_date']} .. {r['end_date']}")
    if rows:
        print("\nThe NLP-derived observation was found by OHDSI-generated cohort SQL.")
    else:
        print("\nNo patients — the loop did NOT close.")
    cdm.close()
    return 0 if rows else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--cdm", type=Path, default=ROOT / "build" / "cdm.db")
    p.add_argument("--registry", type=Path, default=ROOT / "vocab" / "custom_vocab.db")
    p.add_argument("--vocab", type=Path, default=ROOT / "vocab" / "concept.db")
    p.add_argument("--out", type=Path, default=ROOT / "build" / "cohort_expression.json")
    p.set_defaults(func=cmd_prepare)

    r = sub.add_parser("run")
    r.add_argument("--cdm", type=Path, default=ROOT / "build" / "cdm.db")
    r.add_argument("--sql", type=Path, default=ROOT / "build" / "cohort.sql")
    r.add_argument("--keep-going", action="store_true")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
