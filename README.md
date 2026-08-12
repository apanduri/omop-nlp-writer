# omop_nlp_writer

Insert NLP-extracted clinical facts into an OMOP CDM 5.4 — **NOTE_NLP plus the
appropriate domain table** — keeping every inserted record distinguishable from
structured EHR data.

```
chart-review NER output ─┐
                         ├─► ExtractionRecord ─► NOTE_NLP  ─┐
normalizer output ───────┘    (CONTRACT.md)    └► domain table (OBSERVATION, …)
                                                    │
                                                    ▼
                              computable_phenotype_library generates cohort SQL
                              that reads these rows
```

Stdlib-only Python, SQLite target — `make demo` runs the whole loop with no
infrastructure and no dependencies.

## Quickstart

```bash
make demo      # fixtures -> vocab -> CDM -> records -> dry-run -> load -> verify
make test      # 62 tests
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

The reference example (MMSE = 22), end to end:

```
OK   note 1001 @130-134    'MMSE'
      NOTE_NLP    : note_nlp_id=9000000001 term_exists=Y concept=42869861 (Mini-Mental State Examination [MMSE] Observation)
      domain      : Observation -> OBSERVATION
        observation_id                   = 9000000001
        person_id                        = 1
        observation_concept_id           = 42869861
        observation_date                 = 2025-03-14
        observation_type_concept_id      = 32858  <-- NLP provenance
        observation_source_value         = MMSE
        value_as_number                  = 22.0
```

## Layout

| Path | What |
|---|---|
| [CONTRACT.md](CONTRACT.md) | **The input contract.** The one thing both pipelines must meet |
| [INTEGRATION-OPTIONS.md](INTEGRATION-OPTIONS.md) | How the 3 repos could be wired together — for the meeting |
| `omop_nlp_writer/record.py` | `ExtractionRecord` + validation |
| `omop_nlp_writer/vocab.py` | `concept_id` → `domain_id`; also reads custom concepts read-only |
| `omop_nlp_writer/domains.py` | Domain → CDM table routing, provenance constants |
| `omop_nlp_writer/cdm.py` | CDM 5.4 subset DDL + the NLP ledger |
| `omop_nlp_writer/writer.py` | `plan()` / `execute()` — the core |
| `omop_nlp_writer/adapters/` | One file per upstream format (`acts.py`, `chart_review.py`) |
| `omop_nlp_writer/normalizer_client.py` | Calls concept-normalizer; optional and lazily imported |
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
| `note_not_found` | `NOTE_NLP.note_id` is a FK; the note must be loaded first |
| `person_mismatch` | Record's person disagrees with the note's person |
| `already_loaded` | Idempotency, via the ledger |
| `evidence_source_is_not_a_note` | Cites a structured CDM row, not a note — skipped entirely |

**Note ids are crosswalked, not assumed.** The upstream platform emits string note
ids (`"2024-12-04__pathology_report"`); `NOTE.note_id` is an integer. An `int`
`note_id` is used as a CDM key; a `string` is resolved via
`NOTE.note_source_value`, which is the CDM's own slot for the source system's
identifier — so no side table is needed. Same for `person_source_value`. Types are
honoured as given, so `"1001"` is a source value and `1001` is a key. The ledger
records both, so a load can be traced back to the producer's id space.

**Facts read out of the CDM are never written back.** Rubric-style evidence can
carry `"source": "omop"` with a table and row id, citing a structured row the
extractor *read* rather than something found in a note. Those are refused
outright — not even a NOTE_NLP row — because re-inserting them would duplicate
existing EHR data under an NLP provenance flag.

**Re-runs are no-ops.** `nlp_record_ledger` maps each `record_id` to the rows it
produced. That gives idempotency *and* makes `unload` possible — an NLP load is
reversible without guessing which rows came from where. The ledger is our table,
not part of the CDM.

**Zero-length events.** A note mention implies no duration, so
`condition_end_date == condition_start_date`. Inventing a duration would be worse.

## Vocabulary

The real OMOP vocabulary lives in `vocab/` (gitignored — 1.1 GB):

```
vocab/concept.db               6,414,175 concepts   (full Athena bundle)
vocab/concept_relationship.db  1,692,952 relationships
```

Run against it with `--vocab vocab/concept.db`. It is schema-identical to
computable_phenotype_library's `concept.db` — the same file, in fact, copied from
the CP server. `fixtures/vocab_mini.csv` remains for fast offline tests.

**Provenance concept, verified 2026-08-03** against that vocabulary:
`32858 = NLP` (Type Concept, standard). Worth recording what it is *not* —
`32468`, an earlier guess here, is "Inferred from claim" (Procedure Type), and
`32817 = EHR` is the structured-data type NLP rows must stay distinct from.

## Before this touches anything real

1. **Point at a real CDM.** SQLite here for zero-setup; Eunomia or Synthea+ETL
   next. Postgres is a connection change, not a schema change. `cdm.py` creates
   only the tables this writer touches — it is not a conformant full CDM.
2. **Settle the `planned` assertion policy.** `"start donepezil"` currently
   produces a DRUG_EXPOSURE row. A planned medication arguably is not an
   exposure. One dict in
   [adapters/chart_review.py](omop_nlp_writer/adapters/chart_review.py) flips it;
   the assertion is preserved in `term_modifiers` either way.
## End-to-end proof

`scripts/cohort_demo.py` closes the loop: an NLP-extracted fact is found by real
OHDSI-generated cohort SQL.

```
BSO-AD NER output          (Behavior_and_Lifestyle, Treadmill)
  -> concept-normalizer    custom concept 2030233672, Observation domain
  -> load                  OBSERVATION row, type_concept_id 32858 (NLP)
  -> CirceR                cohort SQL for "Exercise equipment + descendants"
  -> run                   person_id=2 (SYNTH-002) 2025-04-02 .. 2026-12-31
```

The cohort is defined on the **parent** concept, which no CDM row carries. It can
only return anyone if the `CONCEPT_ANCESTOR` rows generated from BSO-AD's own
hierarchy are right — so it tests the thing most likely to fail silently.
Verified by negative control: deleting the single ancestor row linking `Treadmill`
to `Exercise equipment` drops the cohort to zero patients.

Two things learned running it:

**CirceR is not installed locally** — it lives in the CP server's backend
container. `prepare` emits a cohort expression, CirceR turns it into SQL there,
and `run` executes that SQL here. SQL generation is pure computation and touches
no patient data.

**SqlRender's SQLite dialect assumes NUMERIC epoch dates.** It renders every date
operation as `CAST(STRFTIME('%s', DATETIME(col, 'unixepoch', ...)) AS REAL)`,
which is how DatabaseConnector loads a SQLite CDM. This writer stores ISO text —
readable, and what Postgres wants — so date comparisons silently match nothing and
the cohort returns zero rows with no error. `run` therefore executes against an
epoch-converted copy rather than patching the generated SQL, so what runs is what
OHDSI tooling would run. A Postgres CDM never hits this.

## ACTS: rubric output

ACTS answers 29 typed questions per patient rather than tagging spans, so the
concept comes from the **field** and the value from the **answer**:

```bash
python3 -m omop_nlp_writer load --vocab vocab/concept.db --acts --commit
```

```
[load] normalizer: target=OMOP, AliasTable('acts', 13 mapped, 9 deliberate non-mappings)

OK   note 1001 @130-140    'MMSE 22/30'
      domain      : Measurement -> MEASUREMENT
        measurement_concept_id           = 4169175
        value_as_number                  = 22.0
        measurement_type_concept_id      = 32858  <-- NLP provenance
```

The answer's **type** decides which value column it lands in, and each type is
handled explicitly rather than generically, because the generic version fails in
ways nothing would report:

| Answer type | Goes to | Why it needs care |
|---|---|---|
| score (`mmse_score: 22`) | `value_as_number` | |
| numeric enum (`cdr_global: 0.5`) | `value_as_number` | it is a score, not a label |
| boolean (`impaired_cognition: 0`) | **no domain row** | `0` means NOT impaired — recording it as a fact would fabricate a diagnosis |
| category (`smoking_status: former`) | `value_as_string` | needs an answer-level concept for `value_as_concept_id`; carried visibly until then |
| date (`lmp_date: "around 1998"`) | `value_as_string` | OMOP has no date-valued column — modelling decision pending |
| list (`allergen: [...]`) | `value_as_string` | each element needs its own concept |

Also skipped: the 7 fields the rubric **computes** from another (`mmse_severity`
from `mmse_score`, `apoe4` from `apoe_genotype`) — inserting both would record the
same fact twice.

## Related projects

Normalization — turning extracted text into a concept, and registering an
ontology as OMOP custom concepts — lives in
[concept-normalizer](https://github.com/apanduri/concept-normalizer). This writer
only *reads* the registry it produces (`vocab/custom_vocab.db`), so the dependency
is one-way: nothing about the CDM belongs in the normalizer, and nothing about
matching or vocabulary authoring belongs here.

## Synthetic data only

Every fixture here is fabricated, per the instruction to develop against
synthetic data only. Notes carry a `SYNTHETIC NOTE - NOT REAL PATIENT DATA`
header. Do not point `init-cdm` at real notes.

## Assumptions the upstream formats have to confirm

Documented in [CONTRACT.md](CONTRACT.md). The two that would cost the most to get
wrong:

- **`note_id` identity.** `NOTE_NLP.note_id` is a FK into `NOTE`. Notes must
  already exist in the CDM `NOTE` table under the same ids the extraction output
  uses, or an ID crosswalk is required.
- **The join key.** This assumes `(note_id, span.start, span.end)` links an NER
  mention to its normalization. If the normalizer takes bare strings with no
  offsets, the join is ambiguous for any term appearing twice in one note.
