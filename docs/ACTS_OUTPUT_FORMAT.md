# ACTS output format — for the OMOP CDM loader

Derived from the chart-review platform's own type definitions plus a structural
audit of **42 real `review_state.json` files / 921 field assessments**. Every
type, key name, enum value and presence/absence claim below is measured, not
inferred from the criteria docs.

**All examples here are synthetic.** The real outputs are PHI (verbatim clinical
quotes, service dates embedded in `note_id`, internal person identifiers), so
nothing patient-derived appears in this document. If you need genuinely unedited
files, say so — the repo ships two synthetic ACTS patients
(`patient_fake_acts_01`, `_02`) and I can run the pipeline over those and send
the raw output. That gives you byte-exact structure with no redaction anywhere.

Schema source of truth in code:
`packages/domain-review/src/review-state.ts`

---

## 1. Where output is written

One `review_state.json` **per (session × patient × task)**:

```
<CHART_REVIEW_REVIEWS_ROOT>/<session_id>/<patient_id>/<task_id>/review_state.json
```

Not per note. `<task_id>` is `acts`. `<session_id>` looks like `session_004`, and
the same patient reviewed in two sessions has two independent files — there is no
merge step.

Agent-run artifacts live separately, one directory per run:

```
<CHART_REVIEW_RUNS_ROOT>/<run_id>/
├── manifest.json                                  run config (the index you want)
├── status.json                                    per-patient outcome
├── per_patient/<patient_id>/agent_draft.json      legacy single-agent draft
├── per_patient/<patient_id>/agents/agent_1.json   per-agent draft
├── per_patient/<patient_id>/agents/agent_1_transcript.jsonl
└── _scratch_state_agent_1/<patient_id>/<task_id>/review_state.json
```

`run_id` is an ISO-ish timestamp, e.g. `2026-08-17T18-20-32-131Z`.
`agent_draft.json` and `agents/agent_N.json` use the **same schema** as
`review_state.json`.

`_scratch_state_agent_N/` is the agent's live working state during the run; when
a run crashes it can be the only place its answers survive. Prefer the exported
`agent_draft.json` when present.

The roots are configurable via `CHART_REVIEW_REVIEWS_ROOT` and
`CHART_REVIEW_RUNS_ROOT`, defaulting to `<platform_root>/var/reviews` and
`<platform_root>/var/runs`.

---

## 2. `review_state.json`

```jsonc
{
  "schema_version": "1",                    // string, not int. absent in older files
  "patient_id": "patient_fake_acts_01",     // STRING, always
  "task_id": "acts",
  "task_version": "2026-06-23",             // rubric version
  "review_status": "reviewer_validated",
  "version": 49,                            // int, monotonic per file
  "updated_at": "2026-07-21T14:40:03.232Z",
  "updated_by": "sneha",                     // reviewer id or "agent"
  "field_assessments": [ /* see §3 */ ],
  "cross_criterion_alerts": [
    {
      "id": "der:apoe2",
      "kind": "derivation_violation",
      "fields": ["apoe2"],
      "severity": "warning",
      "message": "apoe2 derivation could not be evaluated (missing or inconsistent inputs).",
      "computed_at": "2026-07-13T21:25:30.255Z"
    }
  ],
  "imported_from_run": "2026-07-13T21-20-45-160Z",   // present only if imported
  "imported_at": "2026-07-13T21:31:27.308Z",
  "imported_agents": ["agent_1"]
}
```

Key presence across the 42 audited files:

| key | present in | type |
|---|---|---|
| `patient_id`, `task_id`, `review_status`, `updated_at`, `updated_by` | 42/42 | string |
| `version` | 42/42 | int |
| `field_assessments` | 42/42 | array |
| `cross_criterion_alerts` | 36/42 | array |
| `schema_version`, `task_version` | 25/42 | string |
| `imported_from_run`, `imported_at` | 17/42 | string |
| `imported_agents` | 17/42 | array |

**Treat everything except the 42/42 set as optional.** Note `schema_version` is
declared *required* in the type but is absent from 17/42 real files, so it's
back-compat-optional in practice.

`review_status` ∈ `draft` · `in_progress` · `agent_complete` ·
`reviewer_validated` · `locked`

### Further optional keys declared in the type

None appeared in the ACTS corpus, but the interface allows them and a loader
should tolerate them rather than fail on unknown keys:

```ts
task_document_sha?: string;
summary?: ReviewSummary;              // brief_summary, key_conditions, …
keyword_suggestions?: KeywordSuggestions;
selected_evidence?: SelectedEvidence[];
locked_at?, locked_by?, lock_task_sha?: string;   // set by the LOCK phase
assigned_to?: string[];
encounters?: Encounter[];             // empty for patient-level tasks like ACTS
span_labels?                          // NER tasks only; absent for phenotype
```

`Encounter` is `{ encounter_id, kind: "encounter"|"episode", date?, label?,
note_ids? }`, and pairs with `FieldAssessment.encounter_id` for guidelines that
capture per-visit findings. **ACTS is patient-level** (`review_unit: patient`),
so `encounters` is absent and every assessment is patient-wide — one answer per
`field_id`. If you later load an encounter-scoped task, the same `field_id` can
appear multiple times, distinguished only by `encounter_id`.

### ⚠ `updated_by` is typed narrowly but isn't

The type declares `updated_by: AssessmentSource | "system"` — i.e.
`agent`/`reviewer`/`derived`/`system`. Real values are **reviewer ids**
(`"sneha"`, `"agent"`, `"agent_batch-<patient>-agent_1-<epoch>"`). Treat it as a
free-form string, not an enum.

---

## 3. `field_assessments[]`

```jsonc
{
  "field_id": "smoking_status",             // exact spelling, snake_case
  "answer": "current",                       // type VARIES by field — see §4
  "confidence": "high",                      // OPTIONAL — see warning below
  "evidence": [ /* see §5 */ ],              // always present, often []
  "rationale": "Documented as a current every day smoker.",
  "source": "reviewer",
  "status": "approved",
  "updated_at": "2026-07-21T14:39:10.919Z",
  "updated_by": "sneha",
  "captured_against_schema_hash": "0702d6bbe98b1a8e",

  // present only when a reviewer changed an agent answer:
  "original_agent_snapshot": {
    "answer": "current",
    "evidence": [ /* … */ ],
    "rationale": "…",
    "confidence": "high",
    "captured_at": "2026-07-21T14:37:38.556Z",
    "captured_from_version": 14
  },

  // other optional keys defined in the type but not observed in this corpus:
  "edit_reason": "misinterpreted",   // missed_evidence | misinterpreted | wrong_rule | criterion_ambiguous | other
  "edit_note": "…",
  "comment": "…",
  "encounter_id": "…"                // when a field is scoped to one encounter
}
```

Measured presence over 921 assessments:

| key | count | note |
|---|---|---|
| `field_id`, `answer`, `evidence`, `rationale`, `source`, `status`, `updated_at`, `updated_by`, `captured_against_schema_hash` | 921 | always |
| `original_agent_snapshot` | 93 | only where a reviewer edited an agent answer |
| **`confidence`** | **3** | **almost always absent — see below** |

### ⚠ `confidence` is usually absent

It appeared in **3 of 921** assessments. Only agent-authored assessments carry
it; once a reviewer touches a field, the reviewer's assessment has no
`confidence`. If you need the agent's confidence for a reviewed field, read
`original_agent_snapshot.confidence`.

Observed enum values:

```
source:     reviewer (900) · derived (18) · agent (3)
status:     approved (918) · agent_proposed (3)
confidence: absent (918) · "high" (3)
```

Full unions from `review-state.ts`:

```ts
type AssessmentSource = "agent" | "reviewer" | "derived";
type AssessmentStatus = "pending" | "agent_proposed" | "approved"
                      | "overridden" | "not_applicable";
```

`source: "derived"` marks the platform-computed fields (§7).

---

## 4. `answer` types — measured per field

Aggregate over 921 assessments: `null` 794 · `array` 72 · `string` 54 · `int` 1.

**Booleans are the strings `"1"` / `"0"`** — not JSON booleans, not ints.
**Enums and codes are strings.** **Numerics are JSON numbers.**

| field_id | answer type when answered | notes |
|---|---|---|
| `impaired_cognition` | `string` — `"1"` / `"0"` | **the 0/no case you asked about: `"0"`, a string** |
| `postmenopause` | `string` — `"1"` / `"0"` | same |
| `apoe_genotype` | `string` — `"e3/e4"`, `"e4_carrier"`, `"none"` | |
| `apoe2` / `apoe3` / `apoe4` | `string` — `"1"` / `"0"` / `"NA"` | derived; note the third value `"NA"` |
| `smoking_status` | `string` — `"current"`/`"former"`/`"never"`/`"unknown"` | |
| `cdr_global` | `string` — `"0"`,`"0.5"`,`"1"`,`"2"`,`"3"` | **string, including `"0.5"`** |
| `gds_stage` | `string` — `"1"`…`"7"` | |
| `cdr_severity`, `mmse_severity`, `moca_severity` | `string` | derived bands |
| `mmse_score`, `moca_score`, `mattis_drs`, `tics_score`, `hachinski_score`, `npi_total`, `cornell_csdd`, `gds_depression_score`, `education_years`, `smoking_duration` | `int` | never answered in this corpus — all `null`. Type per the criteria schema is integer |
| `pack_per_day`, `pack_year` | `number` | `pack_per_day` observed as `int`; `pack_per_day` may be fractional (0.5) so parse as float |
| `lmp_date` | `string` | free text: `"05/10/2026"`, `"two weeks ago"`, `"January 2025"` — **not a parseable date** |
| `quit_time` | `string` | 4-digit year as a string, e.g. `"2015"` |
| `allergen`, `vaccine_name` | `array` of objects | §6 |
| `vaccine_category` | `array` | derived from `vaccine_name[].Category` |

Two traps for a loader:

- **`cdr_global` is a string containing `"0.5"`.** Casting to int loses it.
- **`lmp_date` is deliberately free text.** The criterion accepts relative
  expressions; don't coerce to a date column without a nullable text fallback.

---

## 5. `evidence[]`

```jsonc
{
  "source": "note",                                  // "note" | "omop"
  "note_id": "2021-04-12__sleep_med_md_op_progress_note.txt",
  "span_offsets": [2045, 2056],                       // [start, end) char offsets
  "verbatim_quote": "Tobacco use",
  "doc_type": "sleep med md op progress note",
  "author_role": "attending physician"
}
```

All six keys appeared in all 66 observed evidence objects. (The `Evidence`
interface isn't declared in `review-state.ts` — it's imported from
`@chart-review/platform-types` — so the observed keys above are the practical
contract.)

**`note_id` is a STRING — a filename**, of the form
`YYYY-MM-DD__<doc_type_snake_case>.txt`. Not an integer, and not an OMOP
`note_id`. It is the file's basename within `<patients_root>/<patient_id>/notes/`.
It encodes the service date, which is why real values are PHI.

**`span_offsets` are character offsets into that note file**, `[start, end)`.
They are byte-for-byte offsets into the raw `.txt`, so they only resolve against
the same file revision.

Citations per assessment, measured:

```
0 citations: 856 assessments     (unanswered fields, and reviewer answers with no cite)
1 citation:  64
2 citations: 1                    ← multi-citation does occur
```

So model `evidence` as 0..N, and don't assume a single citation.

---

## 6. Entity-list answers (`allergen`, `vaccine_name`)

`answer` is a JSON array of objects; `[]` means "affirmatively none documented"
(e.g. NKDA), which is **not** the same as `null`.

```jsonc
"answer": [
  {
    "Allergen": "Celexa",                    // the SUBSTANCE only
    "Supporting_Evidence": "Allergies Celexa (Swelling Pharyngeal, …)",
    "Category": "medication",                 // medication|food|environment|biologic
    "Type": "allergy",                        // allergy|intolerance
    "Reaction": "Swelling Pharyngeal, Difficulty Breathing, Dryness",
    "Severity": "severe",                     // mild|moderate|severe
    "Clinical_Status": "active",              // active|inactive|resolved
    "Verification_Status": "confirmed"        // confirmed|unconfirmed|refuted|entered-in-error
  }
]
```

```jsonc
"answer": [
  {
    "Vaccine_Name": "Shingrix",
    "Supporting_Evidence": "Shingrix administered 03/2024",
    "Category": "Non-Live Vaccine",   // Live Vaccine|Non-Live Vaccine|BCG|
                                       // Active Amyloid or Tau Immunization|
                                       // Not a vaccine|Ambiguous
    "Administration_Date": "03/2024",  // string, may be absent
    "Disease": "herpes zoster"
  }
]
```

Note the keys inside entity records are **PascalCase with underscores**, unlike
the snake_case `field_id`s. Required keys are `Allergen` /`Vaccine_Name` plus
`Supporting_Evidence`; the rest are optional attributes.

---

## 7. Computed fields — present, not omitted

**Yes, they are emitted.** Seven fields are computed by the platform rather than
answered by the agent, and they appear in the output with `source: "derived"`:

| derived field | computed from |
|---|---|
| `apoe2`, `apoe3`, `apoe4` | `apoe_genotype` |
| `moca_severity` | `moca_score` (≥26 normal, ≥18 mild, ≥10 moderate, else severe) |
| `mmse_severity` | `mmse_score` (≥24 / ≥19 / ≥10 / else) |
| `cdr_severity` | `cdr_global` (0 normal, 0.5 very_mild, 1 mild, 2 moderate, 3 severe) |
| `vaccine_category` | `entity_attr(vaccine_name, Category)` |

Measured presence: `apoe2/3/4`, `cdr_severity`, `mmse_severity`, `moca_severity`
in 36/42 files; `vaccine_category` in 26/42.

For a loader you probably want to **skip these and recompute**, since they're
functions of other fields and the derivation expressions live in the criteria
frontmatter (`derivation:` key in
`.claude/skills/chart-review-acts/references/criteria/*.md`).

---

## 8. Undocumented fields: omitted *or* null — both happen

This is the subtlest part, and it's two different cases:

**a) Applicable but not documented → PRESENT with `answer: null`.**
794 of 921 assessments have `answer: null`. E.g. a chart with no MoCA still emits
an `mmse_score` / `moca_score` assessment with `answer: null`, `evidence: []`.

**b) Not applicable → OMITTED entirely.** The conditional smoking fields are
gated by `is_applicable_when` on `smoking_status`, and simply don't appear:

```
pack_per_day      present in  7 / 42 files
quit_time         present in  7 / 42
pack_year         present in  8 / 42
smoking_duration  present in  9 / 42
vaccine_category  present in 26 / 42
(all other fields  present in 36 / 42)
```

Assessment counts per file: `{0: 6, 24: 7, 25: 21, 28: 4, 29: 4}` — so a file may
carry 0, 24, 25, 28 or 29 assessments out of 29 possible field ids.

### ⚠ The one that will corrupt your data

**Do not treat absent-or-null as `0`.** The rubric is explicit that `0` is a real,
severe value:

> If the chart documents **NO** number for this scale, leave the answer **null**
> — do **NOT** write `0`; 0 is a real, severe score.

So for `impaired_cognition`, `"0"` means *affirmatively not impaired* (cognition
documented normal), while `null` means *not addressed*. A MoCA of `0` means
profound impairment; `null` means no MoCA was performed. Collapsing those into
one column silently invents findings.

Same distinction for entity lists: `[]` = affirmatively none (NKDA), `null` = not
assessed.

---

## 9. Run-level files

`manifest.json`:

```
agent_specs, cost_cap_usd, guideline_sha, kind, label, max_concurrency,
max_turns_per_patient, model, patient_ids, provider, rubric_version,
run_id, session_id, started_at, started_by, task_id
```

`session_id` links a run back to the session whose `review_state.json` files it
produced — that's your join key. `label` is a human name like `pilot-iter_011`.

### ⚠ `manifest.model` is unreliable

It records `modelFor("default")` — a hardcoded config default
(`packages/model-config/src/index.ts`), **not the model actually used**. Every run
in our corpus claims `anthropic/claude-haiku-4.5` while actually having run
`Qwen/Qwen3-32B` on local vLLM. Don't use it for provenance.

`status.json`:

```
run_id, state, started_at, updated_at, completed_at,
total_cost_usd, n_patients, n_complete, n_error, n_running,
per_patient: { "<patient_id>": { state, started_at, completed_at,
                                 duration_ms, cost_usd, field_count, error } }
```

`per_patient` keys are patient id **strings**. `state` ∈ `complete` · `error` ·
`running`; the run-level `state` includes `complete_with_errors`.

### ⚠ `field_count` undercounts

A patient can report `field_count: 0` while its draft contains a populated
entity-list answer (`allergen`). Don't use `field_count` to decide whether a
draft is empty — read the assessments.

---

## 10. Summary of your direct questions

| question | answer |
|---|---|
| field identifier spelling | snake_case, exactly as the criteria filenames: `mmse_score`, `impaired_cognition`, `apoe_genotype`, `smoking_status`, `lmp_date`, `pack_per_day`, `allergen`, `vaccine_name` |
| is `22` a number or `"22"`? | numeric scores are JSON **numbers**; enums/booleans/CDR are **strings** (`"0"`, `"0.5"`, `"current"`) |
| `note_id` type | **string** — a filename `YYYY-MM-DD__doc_type.txt`, not an integer, not an OMOP note_id |
| `person_id` type | **string** — `patient_id`, of the form `patient_<16 digits>` (or `patient_fake_acts_01` for synthetic); also the key type in `status.json.per_patient` |
| offsets | `span_offsets: [start, end)` char offsets into that note file |
| confidence | optional, `low`/`medium`/`high`; **absent on 918/921** — reviewer answers drop it. Use `original_agent_snapshot.confidence` |
| computed fields present? | **yes**, with `source: "derived"` — 7 of them |
| undocumented field | **present with `answer: null`** if applicable; **omitted** if `is_applicable_when` excludes it |
| multi-citation | yes, observed; model `evidence` as 0..N |
| one file per what? | per **(session × patient × task)**; run artifacts per run, with `manifest.json` as the index |
| schema in code | `packages/domain-review/src/review-state.ts` |
| output path | `<CHART_REVIEW_REVIEWS_ROOT>/<session>/<patient>/<task>/review_state.json` |

---

## 11. Citation coverage — measured

Same corpus: 42 files, 921 assessments, 127 of them with a non-null answer.

```
non-null answers        127
  with >=1 citation      33   (26.0%)
  with NO citation       94   (74.0%)
```

**But the aggregate is the wrong number to plan against.** Split by field kind:

| kind | answered | cited | coverage |
|---|---|---|---|
| **scalar** | 37 | 33 | **89.2%** |
| entity-list (`allergen`, `vaccine_name`) | 72 | 0 | 0% |
| derived (`apoe2/3/4`, `*_severity`, `vaccine_category`) | 18 | 0 | 0% |

So it isn't "half of answers lack provenance". Scalars cite **89%** of the time,
and the whole gap is two categories that are structurally different:

- **Derived fields (18)** — computed from other fields. They cannot cite, and
  you were going to recompute them anyway (§7).
- **Entity lists (72)** — these never populate `evidence[]`. Their provenance
  lives **inside the answer**: every record carries `Supporting_Evidence`, and
  **14 of 14 records have it (100%)**. The remaining answers are `[]`
  (affirmatively none documented / NKDA), which have nothing to cite.

Answered-but-uncited, by field:

```
allergen        36     entity-list — see above
vaccine_name    36     entity-list
apoe2/3/4       5 each derived
cdr_severity    3      derived
cdr_global      1  postmenopause 1  apoe_genotype 1  gds_stage 1
```

Only **4 scalar answers** across the whole corpus were answered without a
citation.

### By source

| source | answered | cited | coverage |
|---|---|---|---|
| reviewer | 109 | 33 | 30.3% |
| derived | 18 | 0 | 0% |
| agent | 0 | — | — |

No agent-sourced *answers* appear: once a reviewer approves an agent draft the
assessment becomes `source: "reviewer"` (the agent's version is preserved under
`original_agent_snapshot`). So this corpus can't measure the agent's own citation
rate — but two code paths make it effectively 100% for anything an agent answers:

- **Scalars** go through the faithfulness gate at the MCP boundary
  (`packages/faithfulness` → `verifyEvidence`): a quote not literally present in
  the note is **rejected**, with offsets auto-corrected when the quote is found
  but mis-located. An agent answer without verifiable evidence cannot be written.
- **Entity lists** go through `assertAnswerEntities(field, answer, requireEvidence = true)`
  in `packages/domain-review/src/review-state.ts:921`, which throws
  `entity_missing_evidence` on any record lacking `Supporting_Evidence`.

### ⚠ Caveat on the sample

These 127 answers come from a **deliberately sparse** annotation pass — most
ACTS fields legitimately aren't documented in a chart, and this reviewer's
answers skew heavily to `null`. A richer run (ours produced ~200 answered fields
across 20 patients) may shift the mix. Treat 89% scalar coverage as indicative,
not a guarantee.

---

## 12. Note provenance when there is no citation

**Yes — recoverable, at three levels of precision.**

**1. Patient-level date, always available.** Every patient directory has a
`meta.json`, and `index_date` is populated for **30/30 patients**:

```
meta.json keys: patient_id, category, index_date, generated_by, phi
```

That's enough to write the clinical fact even with no note reference.

**2. The document set, always enumerable.** `<patients_root>/<patient_id>/notes/*.txt`,
filenames of the form `YYYY-MM-DD__doc_type.txt`. Notes per patient in this
cohort: 1–6 (`{1:10, 2:9, 3:4, 4:3, 5:1, 6:3}`). So even an uncited answer has a
known, dated candidate set — often a single note, where attribution is
unambiguous.

**3. Entity records carry their own snippet.** `Supporting_Evidence` is a
verbatim quote (14/14 records) but has **no `note_id` and no offsets**. Good for
a text match against the patient's notes; not a direct key.

Two things that do **not** help:

- `review_state.selected_evidence` — declared in the type, present in **0 of 42**
  files.
- **Agent transcripts** (`per_patient/<pid>/agents/agent_1_transcript.jsonl`) log
  tool calls and would in principle reveal which notes were opened. The one we
  sampled came from a failed run (2 lines, no tool calls), so **this is
  plausible but unverified** — don't build on it until confirmed against a
  successful run.

### Suggested loading strategy

| case | NOTE_NLP evidence row | clinical fact row |
|---|---|---|
| scalar with citation (89% of scalars) | `note_id` + offsets + quote | yes |
| scalar without citation | omit, or attribute to the sole note when the patient has exactly one | yes, dated by `index_date` |
| entity record | no `note_id`; `Supporting_Evidence` as the quote | yes, dated by `index_date` |
| `[]` entity answer | none | yes — an affirmative negative (NKDA) |
| `answer: null` | none | no — absence of assessment, not a finding |
| derived field | none | recompute rather than load |

That should let you load every clinical fact and lose only the NOTE_NLP rows for
uncited answers, rather than dropping the facts themselves.

---

## 13. Is citation enforceable in the review UI?

**Not today for reviewers; yes for agents; and the switch already exists.**

The VALIDATE gate (`server/review-routes.ts`, phenotype branch) checks four
predicates:

```ts
all_terminal                        // every leaf approved | overridden | not_applicable
every_leaf_touched_or_bulk_accepted // source === "reviewer" || status === "not_applicable"
alerts_dismissed                    // no error-severity cross-criterion alerts
faithfulness_pass = true            // hardcoded — "enforced at write time"
```

**No evidence requirement.** A reviewer can answer any field with no citation and
still mark the patient validated. `faithfulness_pass` is a literal `true`,
because faithfulness is enforced on the *agent's* write path, not the human's.

For entity fields the asymmetry is explicit in the code, with the rationale in a
comment:

```ts
// Supporting_Evidence is required of the AGENT (anti-fabrication); a human
// reviewer adjudicating may enter an entity without pasting a quote.
if (requireEvidence && (rec.Supporting_Evidence == null || …)) throw …
```

So `requireEvidence` is a real parameter (`review-state.ts:921`, default `true`).

**Could a required-citation mode exist?** Yes, cheaply — for scalars it's one
additional predicate in that gate (`every_answered_leaf_has_evidence`), and for
entity lists it's passing `requireEvidence = true` on the reviewer path. Neither
is an architectural change. Whether it *should* be on is a methodology call: it
would slow reviewers down and force a citation for answers a human derives from
reading the whole chart rather than one span.

If provenance completeness matters to your load, that's worth raising with the
platform owner as a config flag rather than working around downstream.

---

## 14. Offer

If hand-checking against synthetic fixtures isn't enough, I can run the pipeline
over the repo's two synthetic ACTS patients and send you the resulting
`review_state.json`, `agent_draft.json`, `manifest.json` and `status.json`
verbatim — no redaction, no PHI, exact bytes. That's a better basis for a loader
than anything reconstructed by hand. Just ask.
