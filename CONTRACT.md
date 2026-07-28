# ExtractionRecord — the integration contract

This is the **only** interface this writer accepts. Both upstream pipelines are
expected to meet this schema via a thin adapter (see `omop_nlp_writer/adapters/`);
neither pipeline needs to change its own native output format.

One `ExtractionRecord` = **one mention of one concept in one note**.

## Schema

```jsonc
{
  // Stable, deterministic id for this mention. Used as the idempotency key so
  // re-running the same extraction never duplicates CDM rows.
  // Convention: "<pipeline>:<note_id>:<start>-<end>:<concept_id>"
  "record_id": "cr:1001:412-416:42869861",

  // --- provenance (who produced this) --------------------------------------
  "source": {
    "pipeline": "chart-review-platform",   // -> NOTE_NLP.nlp_system
    "version":  "0.3.1",
    "model":    "claude-opus-4-5",
    "normalizer": "bso-normalizer@0.2.0"   // optional, appended to nlp_system
  },

  // --- where in the record ---------------------------------------------------
  // An INT is used as a CDM surrogate key directly.
  // A STRING is treated as the upstream identifier and resolved via
  //   NOTE.note_source_value / PERSON.person_source_value.
  // The chart-review platform emits string note ids, so both are accepted:
  //   "note_id": "2024-12-04__pathology_report"   -> resolved via source value
  //   "note_id": 1001                             -> used as the key
  // Types are honoured as given: "1001" is a source value, 1001 is a key.
  // Either way the note must already exist in NOTE.
  "note_id":   1001,
  "person_id": 1,

  // Where the fact came from. "note" (or omitted) is the only insertable value.
  // Rubric-answer evidence can carry {"source": "omop", "table": ..., "row_id": ...},
  // which cites a structured row the extractor READ. Re-inserting those would
  // duplicate existing EHR data as NLP-derived, so the writer refuses them
  // outright — not even a NOTE_NLP row, because it was never in a note.
  "evidence_source": "note",
  "note_datetime": "2025-03-14T09:20:00",
  "span":    { "start": 412, "end": 416 },   // char offsets into NOTE.note_text
  "lexical_variant": "MMSE",                 // the surface text as written
  "snippet": "...cognition: MMSE 22/30, down from 26...",
  "section_concept_id": null,                // optional

  // --- assertion / negation -------------------------------------------------
  "term_exists":    true,      // false => NOTE_NLP row only, NO domain row
  "term_temporal":  "current", // "current" | "history" | null
  "term_modifiers": "confidence=0.94",

  // --- normalization (from the normalizer pipeline) -------------------------
  "concept_id": 42869861,      // null or 0 => unmapped; NOTE_NLP row only
  "concept_confidence": 0.97,

  // --- the value, if the extraction carried one ----------------------------
  "value": {
    "as_number":     22.0,
    "as_string":     null,
    "as_concept_id": null,
    "unit_concept_id":  null,
    "unit_source_value": null,
    "operator_concept_id": null
  },

  "start_date": "2025-03-14"   // date for the domain row; defaults to note date
}
```

## Deliberately NOT in the schema

**`domain_id`.** The writer derives it from `concept_id` against the OMOP
vocabulary. Accepting it as an input would create a second source of truth that
would silently drift from the vocabulary the query-generation side uses.

## Settled

- **Note identity.** Confirmed to need a crosswalk: the platform's note ids are
  strings (`"2024-12-04__pathology_report"`) and `NOTE.note_id` is an integer.
  Handled — `note_source_value` is the crosswalk, and `init-cdm` populates it.
- **Join key.** Moot. The NER SDK is vendored inside the chart-review platform, so
  each `MentionRecord` already carries `note_id`, `person_id`, `start`/`end`,
  `text` and `concept_name` together. No cross-pipeline join is needed.
- **Structured-source evidence.** Rubric evidence carrying `"source": "omop"`
  cites a row read out of the CDM. Refused outright, so existing EHR data is
  never re-inserted as NLP-derived.

## Still open

1. **BSO-AD → OMOP mapping.** The vendored NER SDK normalizes to the BSO-AD
   ontology — `ontology/concepts.json` holds ~550 custom ids and labels with no
   OMOP, SNOMED, LOINC or RxNorm codes. So `concept_id` cannot currently be
   populated at all. Needs either an OMOP code per BSO-AD concept, or a curated
   crosswalk file. Blocking for anything beyond synthetic runs.
2. **Numeric values.** BSO-AD explicitly excludes lab values and assessments, and
   rubric answers are boolean/categorical. Nothing observed so far emits the
   `(concept, numeric value)` pair the MMSE = 22 example needs.
3. **Units.** `unit_concept_id` must itself be normalized. Currently kept as
   `unit_source_value` only, unmapped and visible rather than guessed.
4. **Assertion policy.** Whether `planned` mentions ("start donepezil") should
   become clinical facts. Currently yes; one dict flips it.
