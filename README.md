# omop_nlp_writer

Insert NLP-extracted clinical facts into an OMOP CDM 5.4 — **NOTE_NLP plus the
appropriate domain table** — keeping every inserted record distinguishable from
structured EHR data.

```
chart-review NER output ─┐
  (Yuhang's pipeline)    ├─► ExtractionRecord ─► NOTE_NLP  ─┐
normalizer output ───────┘    (CONTRACT.md)    └► domain table (OBSERVATION, …)
  (Xuguang's pipeline)                              │
                                                    ▼
                              computable_phenotype_library generates cohort SQL
                              that reads these rows
```

Stdlib-only Python, SQLite target — `make demo` runs the whole loop with no
infrastructure and no dependencies.

## Quickstart

```bash
make demo      # fixtures -> vocab -> CDM -> records -> dry-run -> load -> verify
make test      # 23 tests
```

Or step by step:

```bash
python3 scripts/make_fixtures.py             # dummy output for BOTH pipelines
python3 -m omop_nlp_writer build-vocab       # synthetic dev vocabulary
python3 -m omop_nlp_writer init-cdm          # CDM 5.4 subset + synthetic PERSON/NOTE
python3 -m omop_nlp_writer records --out build/extractions.json
python3 -m omop_nlp_writer load              # DRY RUN — prints every row it would write
python3 -m omop_nlp_writer load --commit --min-confidence 0.5
python3 -m omop_nlp_writer verify
python3 -m omop_nlp_writer unload            # reverse it
```

The worked example from Hongyu's email, end to end:

```
OK   note 1001 @130-134    'MMSE'
      NOTE_NLP    : note_nlp_id=9000000001 term_exists=Y concept=42869861 (Mini-Mental State Examination [MMSE] Observation)
      domain      : Observation -> OBSERVATION
        observation_id                   = 9000000001
        person_id                        = 1
        observation_concept_id           = 42869861
        observation_date                 = 2025-03-14
        observation_type_concept_id      = 32468  <-- NLP provenance
        observation_source_value         = MMSE
        value_as_number                  = 22.0
```

## Layout

| Path | What |
|---|---|
| [CONTRACT.md](CONTRACT.md) | **The input contract.** The one thing both pipelines must meet |
| [INTEGRATION-OPTIONS.md](INTEGRATION-OPTIONS.md) | How the 3 repos could be wired together — for the meeting |
| `omop_nlp_writer/record.py` | `ExtractionRecord` + validation |
| `omop_nlp_writer/vocab.py` | `concept_id` → `domain_id`, schema-identical to CP's `concept.db` |
| `omop_nlp_writer/domains.py` | Domain → CDM table routing, provenance constants |
| `omop_nlp_writer/cdm.py` | CDM 5.4 subset DDL + the NLP ledger |
| `omop_nlp_writer/writer.py` | `plan()` / `execute()` — the core |
| `omop_nlp_writer/adapters/` | One file per upstream pipeline; the only format-specific code |
| `fixtures/` | Dummy output for both pipelines + synthetic notes |
| `scripts/make_fixtures.py` | Regenerates all fixtures; offsets computed from note text |

## Design decisions worth knowing

**`plan()` and `execute()` are separate.** `--dry-run` is the default and follows
the identical decision path as a real load, minus the INSERTs. You can show the
whole mapping to the group without touching a database.

**`domain_id` is never accepted as input.** It's derived from `concept_id` via
the vocabulary. Two sources of truth for domain assignment would silently drift
from the routing the query-generation side uses.

**Provenance, two ways.** Every domain row carries `*_type_concept_id =
NLP_TYPE_CONCEPT_ID`, the CDM-sanctioned mechanism. Additionally, NLP surrogate
keys are allocated above 9,000,000,000, so an NLP row is identifiable from its id
alone even if a downstream tool ignores the type concept.

**Nothing is silently dropped.** A mention that doesn't earn a domain row still
lands in NOTE_NLP as evidence, with the reason reported:

| Reason | Behaviour |
|---|---|
| `term_exists=false` | Negated/absent — evidence only, never a clinical fact |
| `no_concept_id` | Normalizer found nothing — NOTE_NLP with `note_nlp_concept_id = 0` |
| `concept_not_in_vocabulary` | Refuses to guess a domain |
| `below_confidence_threshold` | `--min-confidence` gate |
| `domain_not_routed` | e.g. Visit — writing it would corrupt visit accounting |
| `note_not_found` | `NOTE_NLP.note_id` is a FK; the ID-crosswalk failure mode |
| `person_mismatch` | Record's person disagrees with the note's person |
| `already_loaded` | Idempotency, via the ledger |

**Re-runs are no-ops.** `nlp_record_ledger` maps each `record_id` to the rows it
produced. That gives idempotency *and* makes `unload` possible — an NLP load is
reversible without guessing which rows came from where. The ledger is our table,
not part of the CDM.

**Zero-length events.** A note mention implies no duration, so
`condition_end_date == condition_start_date`. Inventing a duration would be worse.

## Before this touches anything real

1. **Verify `NLP_TYPE_CONCEPT_ID`** ([domains.py](omop_nlp_writer/domains.py)).
   `32468` is used as "Natural Language Processing" and is defined in the
   synthetic vocab so the writer runs today — confirm it against a real
   vocabulary before relying on it:
   ```sql
   SELECT concept_id, concept_name FROM concept
    WHERE vocabulary_id = 'Type Concept' AND concept_name LIKE '%NLP%';
   ```
2. **Replace the synthetic vocabulary.** `fixtures/vocab_mini.csv` is fabricated;
   only `42869861` is authoritative (it came from the email). It's
   schema-identical to computable_phenotype_library's `concept.db`, so
   `--vocab /path/to/concept.db` is the whole switch.
3. **Point at a real CDM.** SQLite here for zero-setup. Eunomia or Synthea+ETL
   next; Postgres is a connection change, not a schema change. `cdm.py` creates
   only the tables this writer touches — it is not a conformant full CDM.
4. **Settle the `planned` assertion policy.** `"start donepezil"` currently
   produces a DRUG_EXPOSURE row. A planned medication arguably is not an
   exposure. One dict in
   [adapters/chart_review.py](omop_nlp_writer/adapters/chart_review.py) flips it;
   the assertion is preserved in `term_modifiers` either way.

## Synthetic data only

Every fixture here is fabricated, per the instruction to develop against
synthetic data only. Notes carry a `SYNTHETIC NOTE - NOT REAL PATIENT DATA`
header. Do not point `init-cdm` at real notes.

## Open questions for Yuhang and Xuguang

Full list in [CONTRACT.md](CONTRACT.md). The two that will cost the most time:

- **`note_id` identity.** `NOTE_NLP.note_id` is a FK into `NOTE`. Do the notes
  the chart-review pipeline reads already exist in a CDM `NOTE` table, or do they
  need loading plus an ID crosswalk?
- **The join key.** This assumes `(note_id, span.start, span.end)` links an NER
  mention to its normalization. If the normalizer takes bare strings with no
  offsets, the join is ambiguous for any term appearing twice in one note.
