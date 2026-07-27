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
  "note_id":   1001,          // FK -> NOTE.note_id  (MUST already exist)
  "person_id": 1,             // FK -> PERSON.person_id
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

## Open questions for Yuhang / Xuguang

1. **`note_id` identity.** NOTE_NLP.note_id is a FK into NOTE. Do the notes the
   chart-review pipeline reads already live in a CDM NOTE table, or do they need
   loading + an ID crosswalk? This is the likeliest source of integration pain.
2. **Join key between the two pipelines.** This contract assumes
   `(note_id, span.start, span.end)` is what links a chart-review mention to its
   normalizer output. If the normalizer works off bare strings with no offsets,
   the join is ambiguous whenever a term appears twice in one note.
3. **Normalizer output shape.** One `concept_id`, or ranked candidates with
   scores? If ranked, we need an agreed confidence floor below which a mention
   is written to NOTE_NLP but *not* to a domain table.
4. **Values.** Does the NER stage emit the numeric value ("22") as a separate
   field, or only as part of the matched text? The writer needs it separated.
5. **Units.** `unit_concept_id` must itself be normalized. Who owns that?
