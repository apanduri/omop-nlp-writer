"""Domain routing — OMOP domain_id -> CDM 5.4 event table.

This mirrors the routing in computable_phenotype_library's
backend/app/ohdsi/algorithm_to_ohdsi.py (_CRITERIA_TYPE_TO_DOMAIN_ID) so the
table an NLP fact lands in is the same table the generated cohort SQL reads
from.  If that mapping changes there, it must change here.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Provenance: how NLP-derived rows stay distinguishable from structured EHR data
# ---------------------------------------------------------------------------

# OMOP "Type Concept" vocabulary id for NLP-derived records.  Every domain row
# this writer creates carries it in the table's *_type_concept_id column, which
# is the CDM-sanctioned way to record provenance.
#
# VERIFY against the real vocabulary before any non-synthetic use: run
#   SELECT concept_id, concept_name FROM concept
#    WHERE vocabulary_id = 'Type Concept' AND concept_name LIKE '%NLP%';
# and correct this constant.  The synthetic dev vocab defines it so the writer
# is exercisable today.
NLP_TYPE_CONCEPT_ID = 32468
NLP_TYPE_CONCEPT_NAME = "Natural Language Processing"

# Second, belt-and-braces provenance signal: surrogate keys for NLP-derived rows
# are allocated above this base, so an NLP row is identifiable from its id alone
# even if a downstream tool ignores *_type_concept_id.
NLP_ID_BASE = 9_000_000_000


@dataclass(slots=True, frozen=True)
class DomainTarget:
    """Where and how a normalized concept is written into the CDM."""

    table: str
    pk: str
    concept_column: str
    source_value_column: str
    type_concept_column: str
    start_date_column: str
    start_datetime_column: str | None = None
    end_date_column: str | None = None
    supports_value: bool = False
    value_columns: tuple[str, ...] = ()


DOMAIN_TARGETS: dict[str, DomainTarget] = {
    "Observation": DomainTarget(
        table="observation",
        pk="observation_id",
        concept_column="observation_concept_id",
        source_value_column="observation_source_value",
        type_concept_column="observation_type_concept_id",
        start_date_column="observation_date",
        start_datetime_column="observation_datetime",
        supports_value=True,
        value_columns=("value_as_number", "value_as_string", "value_as_concept_id", "unit_concept_id"),
    ),
    "Measurement": DomainTarget(
        table="measurement",
        pk="measurement_id",
        concept_column="measurement_concept_id",
        source_value_column="measurement_source_value",
        type_concept_column="measurement_type_concept_id",
        start_date_column="measurement_date",
        start_datetime_column="measurement_datetime",
        supports_value=True,
        value_columns=(
            "value_as_number",
            "value_as_concept_id",
            "unit_concept_id",
            "operator_concept_id",
        ),
    ),
    "Condition": DomainTarget(
        table="condition_occurrence",
        pk="condition_occurrence_id",
        concept_column="condition_concept_id",
        source_value_column="condition_source_value",
        type_concept_column="condition_type_concept_id",
        start_date_column="condition_start_date",
        start_datetime_column="condition_start_datetime",
        end_date_column="condition_end_date",
    ),
    "Drug": DomainTarget(
        table="drug_exposure",
        pk="drug_exposure_id",
        concept_column="drug_concept_id",
        source_value_column="drug_source_value",
        type_concept_column="drug_type_concept_id",
        start_date_column="drug_exposure_start_date",
        start_datetime_column="drug_exposure_start_datetime",
        end_date_column="drug_exposure_end_date",
    ),
    "Procedure": DomainTarget(
        table="procedure_occurrence",
        pk="procedure_occurrence_id",
        concept_column="procedure_concept_id",
        source_value_column="procedure_source_value",
        type_concept_column="procedure_type_concept_id",
        start_date_column="procedure_date",
        start_datetime_column="procedure_datetime",
    ),
    "Device": DomainTarget(
        table="device_exposure",
        pk="device_exposure_id",
        concept_column="device_concept_id",
        source_value_column="device_source_value",
        type_concept_column="device_type_concept_id",
        start_date_column="device_exposure_start_date",
        start_datetime_column="device_exposure_start_datetime",
        end_date_column="device_exposure_end_date",
    ),
    "Specimen": DomainTarget(
        table="specimen",
        pk="specimen_id",
        concept_column="specimen_concept_id",
        source_value_column="specimen_source_value",
        type_concept_column="specimen_type_concept_id",
        start_date_column="specimen_date",
        start_datetime_column="specimen_datetime",
    ),
}

# Domains a normalized concept can legitimately carry but that this writer will
# not create rows for, with the reason.  Keeps the "unsupported" path explicit
# rather than silently dropping facts.
UNROUTED_DOMAINS: dict[str, str] = {
    "Visit": "visits are structural, not NLP-derived; would corrupt visit accounting",
    "Death": "DEATH is one-row-per-person; needs a dedicated reconciliation policy",
    "Meas Value": "a value concept, not an event — belongs in value_as_concept_id",
    "Unit": "a unit concept, not an event — belongs in unit_concept_id",
    "Metadata": "not patient data",
    "Type Concept": "not patient data",
    "Provider": "not patient data",
    "Note": "the note itself is already in NOTE",
    "Spec Anatomic Site": "a qualifier concept, not an event",
}


def route(domain_id: str) -> DomainTarget | None:
    return DOMAIN_TARGETS.get(domain_id)
