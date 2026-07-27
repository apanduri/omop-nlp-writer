"""omop_nlp_writer — insert NLP-extracted clinical facts into an OMOP CDM.

Pipeline:
    chart-review NER output ─┐
                             ├─> ExtractionRecord ─> NOTE_NLP (+ domain table)
    normalizer output ───────┘

Deliberately stdlib-only.
"""

from .record import ContractError, ExtractionRecord, load_records
from .writer import CdmNlpWriter, Disposition, LoadReport, Reason

__all__ = [
    "CdmNlpWriter",
    "ContractError",
    "Disposition",
    "ExtractionRecord",
    "LoadReport",
    "Reason",
    "load_records",
]
