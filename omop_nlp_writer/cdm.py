"""A minimal OMOP CDM 5.4 target, plus the NLP provenance ledger.

SQLite is used so the whole loop runs with no infrastructure.  Column names and
types follow https://ohdsi.github.io/CommonDataModel/cdm54.html — swapping in
Postgres later is a connection change, not a schema change.

Only the tables this writer touches are created; this is NOT a conformant full
CDM.  For real work, point at an actual CDM instance (Eunomia / Synthea+ETL).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# NOTE_NLP per CDM 5.4.  `offset` is varchar(50) in the spec (not an integer) —
# quoted here because OFFSET is a SQL keyword.
CDM_DDL = """
CREATE TABLE IF NOT EXISTS person (
    person_id                   INTEGER PRIMARY KEY,
    gender_concept_id           INTEGER NOT NULL,
    year_of_birth               INTEGER NOT NULL,
    month_of_birth              INTEGER,
    day_of_birth                INTEGER,
    birth_datetime              TEXT,
    race_concept_id             INTEGER NOT NULL DEFAULT 0,
    ethnicity_concept_id        INTEGER NOT NULL DEFAULT 0,
    location_id                 INTEGER,
    provider_id                 INTEGER,
    care_site_id                INTEGER,
    person_source_value         TEXT,
    gender_source_value         TEXT,
    gender_source_concept_id    INTEGER,
    race_source_value           TEXT,
    race_source_concept_id      INTEGER,
    ethnicity_source_value      TEXT,
    ethnicity_source_concept_id INTEGER
);

CREATE TABLE IF NOT EXISTS observation_period (
    observation_period_id         INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    observation_period_start_date TEXT NOT NULL,
    observation_period_end_date   TEXT NOT NULL,
    period_type_concept_id        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS note (
    note_id                   INTEGER PRIMARY KEY,
    person_id                 INTEGER NOT NULL,
    note_date                 TEXT NOT NULL,
    note_datetime             TEXT,
    note_type_concept_id      INTEGER NOT NULL,
    note_class_concept_id     INTEGER NOT NULL,
    note_title                TEXT,
    note_text                 TEXT,
    encoding_concept_id       INTEGER NOT NULL,
    language_concept_id       INTEGER NOT NULL,
    provider_id               INTEGER,
    visit_occurrence_id       INTEGER,
    visit_detail_id           INTEGER,
    note_source_value         TEXT,
    note_event_id             INTEGER,
    note_event_field_concept_id INTEGER
);

CREATE TABLE IF NOT EXISTS note_nlp (
    note_nlp_id                  INTEGER PRIMARY KEY,
    note_id                      INTEGER NOT NULL,
    section_concept_id           INTEGER,
    snippet                      TEXT,
    "offset"                     TEXT,
    lexical_variant              TEXT NOT NULL,
    note_nlp_concept_id          INTEGER,
    note_nlp_source_concept_id   INTEGER,
    nlp_system                   TEXT,
    nlp_date                     TEXT NOT NULL,
    nlp_datetime                 TEXT,
    term_exists                  TEXT,
    term_temporal                TEXT,
    term_modifiers               TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_nlp_note ON note_nlp(note_id);

CREATE TABLE IF NOT EXISTS observation (
    observation_id                INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    observation_concept_id        INTEGER NOT NULL,
    observation_date              TEXT NOT NULL,
    observation_datetime          TEXT,
    observation_type_concept_id   INTEGER NOT NULL,
    value_as_number               REAL,
    value_as_string               TEXT,
    value_as_concept_id           INTEGER,
    qualifier_concept_id          INTEGER,
    unit_concept_id               INTEGER,
    provider_id                   INTEGER,
    visit_occurrence_id           INTEGER,
    visit_detail_id               INTEGER,
    observation_source_value       TEXT,
    observation_source_concept_id  INTEGER,
    unit_source_value             TEXT,
    qualifier_source_value        TEXT,
    value_source_value            TEXT,
    observation_event_id          INTEGER,
    obs_event_field_concept_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_observation_person ON observation(person_id);

CREATE TABLE IF NOT EXISTS measurement (
    measurement_id                INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    measurement_concept_id        INTEGER NOT NULL,
    measurement_date              TEXT NOT NULL,
    measurement_datetime          TEXT,
    measurement_time              TEXT,
    measurement_type_concept_id   INTEGER NOT NULL,
    operator_concept_id           INTEGER,
    value_as_number               REAL,
    value_as_concept_id           INTEGER,
    unit_concept_id               INTEGER,
    range_low                     REAL,
    range_high                    REAL,
    provider_id                   INTEGER,
    visit_occurrence_id           INTEGER,
    visit_detail_id               INTEGER,
    measurement_source_value      TEXT,
    measurement_source_concept_id INTEGER,
    unit_source_value             TEXT,
    unit_source_concept_id        INTEGER,
    value_source_value            TEXT,
    measurement_event_id          INTEGER,
    meas_event_field_concept_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_measurement_person ON measurement(person_id);

CREATE TABLE IF NOT EXISTS condition_occurrence (
    condition_occurrence_id       INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    condition_concept_id          INTEGER NOT NULL,
    condition_start_date          TEXT NOT NULL,
    condition_start_datetime      TEXT,
    condition_end_date            TEXT,
    condition_end_datetime        TEXT,
    condition_type_concept_id     INTEGER NOT NULL,
    condition_status_concept_id   INTEGER,
    stop_reason                   TEXT,
    provider_id                   INTEGER,
    visit_occurrence_id           INTEGER,
    visit_detail_id               INTEGER,
    condition_source_value        TEXT,
    condition_source_concept_id   INTEGER,
    condition_status_source_value TEXT
);
CREATE INDEX IF NOT EXISTS idx_condition_person ON condition_occurrence(person_id);

CREATE TABLE IF NOT EXISTS drug_exposure (
    drug_exposure_id              INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    drug_concept_id               INTEGER NOT NULL,
    drug_exposure_start_date      TEXT NOT NULL,
    drug_exposure_start_datetime  TEXT,
    drug_exposure_end_date        TEXT NOT NULL,
    drug_exposure_end_datetime    TEXT,
    verbatim_end_date             TEXT,
    drug_type_concept_id          INTEGER NOT NULL,
    stop_reason                   TEXT,
    refills                       INTEGER,
    quantity                      REAL,
    days_supply                   INTEGER,
    sig                           TEXT,
    route_concept_id              INTEGER,
    lot_number                    TEXT,
    provider_id                   INTEGER,
    visit_occurrence_id           INTEGER,
    visit_detail_id               INTEGER,
    drug_source_value             TEXT,
    drug_source_concept_id        INTEGER,
    route_source_value            TEXT,
    dose_unit_source_value        TEXT
);
CREATE INDEX IF NOT EXISTS idx_drug_person ON drug_exposure(person_id);

CREATE TABLE IF NOT EXISTS procedure_occurrence (
    procedure_occurrence_id       INTEGER PRIMARY KEY,
    person_id                     INTEGER NOT NULL,
    procedure_concept_id          INTEGER NOT NULL,
    procedure_date                TEXT NOT NULL,
    procedure_datetime            TEXT,
    procedure_end_date            TEXT,
    procedure_end_datetime        TEXT,
    procedure_type_concept_id     INTEGER NOT NULL,
    modifier_concept_id           INTEGER,
    quantity                      INTEGER,
    provider_id                   INTEGER,
    visit_occurrence_id           INTEGER,
    visit_detail_id               INTEGER,
    procedure_source_value        TEXT,
    procedure_source_concept_id   INTEGER,
    modifier_source_value         TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedure_person ON procedure_occurrence(person_id);

CREATE TABLE IF NOT EXISTS device_exposure (
    device_exposure_id              INTEGER PRIMARY KEY,
    person_id                       INTEGER NOT NULL,
    device_concept_id               INTEGER NOT NULL,
    device_exposure_start_date      TEXT NOT NULL,
    device_exposure_start_datetime  TEXT,
    device_exposure_end_date        TEXT,
    device_exposure_end_datetime    TEXT,
    device_type_concept_id          INTEGER NOT NULL,
    unique_device_id                TEXT,
    production_id                   TEXT,
    quantity                        INTEGER,
    provider_id                     INTEGER,
    visit_occurrence_id             INTEGER,
    visit_detail_id                 INTEGER,
    device_source_value             TEXT,
    device_source_concept_id        INTEGER,
    unit_concept_id                 INTEGER,
    unit_source_value               TEXT,
    unit_source_concept_id          INTEGER
);

CREATE TABLE IF NOT EXISTS specimen (
    specimen_id                 INTEGER PRIMARY KEY,
    person_id                   INTEGER NOT NULL,
    specimen_concept_id         INTEGER NOT NULL,
    specimen_type_concept_id    INTEGER NOT NULL,
    specimen_date               TEXT NOT NULL,
    specimen_datetime           TEXT,
    quantity                    REAL,
    unit_concept_id             INTEGER,
    anatomic_site_concept_id    INTEGER,
    disease_status_concept_id   INTEGER,
    specimen_source_id          TEXT,
    specimen_source_value       TEXT,
    unit_source_value           TEXT,
    anatomic_site_source_value  TEXT,
    disease_status_source_value TEXT
);
"""

# Not part of the CDM.  Our own ledger: maps each ExtractionRecord to the rows it
# produced.  This is what makes re-runs idempotent and makes an NLP load
# reversible without guessing which rows came from where.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS nlp_record_ledger (
    record_id        TEXT PRIMARY KEY,
    note_id          INTEGER NOT NULL,
    span_offset      TEXT,
    lexical_variant  TEXT,
    concept_id       INTEGER,
    domain_id        TEXT,
    note_nlp_id      INTEGER,
    domain_table     TEXT,
    domain_row_id    INTEGER,
    nlp_system       TEXT,
    loaded_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_note ON nlp_record_ledger(note_id);
"""


def connect(db_path: Path, *, create: bool = False) -> sqlite3.Connection:
    if not create and not db_path.exists():
        raise FileNotFoundError(
            f"CDM database not found at {db_path}. Run scripts/init_cdm.py first."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CDM_DDL)
    conn.executescript(LEDGER_DDL)
    conn.commit()
