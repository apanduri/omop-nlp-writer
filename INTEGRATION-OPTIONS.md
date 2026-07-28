# Wiring the three repos together

> **DECIDED 2026-07-28 by Hongyu: Option C — separate, independently running
> services.** Shared storage (D) and a single combined tool (B) are ruled out.
> The analysis below is kept for the record; see "Consequences of Option C" at
> the end for what actually changes.
>
> Hongyu also independently confirmed the load-bearing constraint: the query
> pipeline does not query NOTE_NLP, so normalized extractions **must** also be
> written to OBSERVATION / MEASUREMENT / CONDITION_OCCURRENCE. That is exactly
> why this writer produces two rows per mention.

Four ways the pieces could talk. Option A is how the code works today, and it
remains the local-development mode regardless of the deployed topology.

The three pieces:

| # | Repo | Language | Role |
|---|---|---|---|
| 1 | `chart-review-platform` (Yuhang) | TS/React/Express + Python sidecar | notes → entities + spans + values |
| 2 | normalization pipeline (Xuguang) | *link needed* | entity text → `concept_id` |
| 3 | `omop_nlp_writer` (this repo) | Python (stdlib) | records → NOTE_NLP + domain tables |
| 4 | `computable_phenotype_library` | Python + R (CirceR) | cohort SQL that reads the result |

---

## Option A — file handoff, one CLI orchestrates (current)

Each stage writes JSON; this repo's adapters read it.

```
chart-review run ──► *.json ──► [adapter] ──► ExtractionRecord[] ──► CDM
                                    ▲
Xuguang's normalizer ──► *.json ────┘
```

**For:** each repo is independently runnable and testable. Batch by nature —
nothing here is real-time. Saved JSON is replayable, so a bad mapping can be
re-diagnosed six weeks later without re-running an LLM. Checked-in fixtures
become regression tests for free. Zero infrastructure; no shared secrets.
Cross-language for free.

**Against:** someone has to move files between machines. No live status. Manual
sequencing for a large batch.

**Best when:** now, and probably for the next few months.

---

## Option B — one repo, one process

Vendor all three into a monorepo and run a single command end to end.

**For:** one install, one version, one command. No format drift, because there's
one type definition. Easiest for a new group member to run.

**Against:** forces three languages into one build (Yuhang's is TS + a Python
sidecar; ours is Python; the query side needs R). Three owners now share a
release cycle and a git history — a change to the review UI can break a CDM load.
Hardest option to back out of.

**Best when:** the interfaces have stopped changing and the group wants one
artifact to hand to collaborators. Not yet.

---

## Option C — separate services over HTTP

Each pipeline exposes an endpoint; a thin orchestrator calls them in sequence.

**For:** independent deploys and scaling. Real-time single-note extraction
becomes possible (useful if a clinician-facing tool ever wants it). Language
boundaries stay clean.

**Against:** all three must be running, reachable, and version-compatible before
anything can be tested — the dominant cost during active development. Adds auth,
retries, timeouts, and idempotency-across-the-wire. Debugging a bad mapping means
correlating logs across three services. For batch work, this buys nothing.

**Best when:** there's a production, always-on consumer. Not for a research group
iterating on rubrics.

---

## Option D — shared MongoDB as the bus

Each stage reads and writes documents in one Mongo instance:
`notes → extractions → normalized → loaded`, each stage stamping status.

**For:** queryable intermediate state ("show me every mention that failed
normalization"). Natural fit for the annotator/cross-validation work — Mongo is
also where inter-annotator agreement data would live. Decouples timing: stages
poll rather than being invoked. `computable_phenotype_library` already runs
MongoDB, so there's operational familiarity and no new dependency.

**Against:** the schema becomes an implicit shared contract that three people can
change without anyone noticing — the exact drift Option A's single JSON schema
prevents. Needs a real state machine (claimed/failed/retried) or two runners will
double-process. All three repos gain a Mongo dependency and a connection string,
which is awkward for anyone wanting to run one piece standalone. Debugging shifts
from "read the file" to "query the collection."

**Best when:** the annotation/adjudication workflow needs shared mutable state —
which it plausibly will. Worth flagging: if we go here, keep the JSON contract as
the *document* schema, so Mongo becomes a transport for Option A rather than a
replacement for it.

---

## Consequences of Option C (the decision)

What this repo needs, given "separate, independently running services":

1. **An HTTP ingest endpoint** wrapping the existing writer:
   `POST /extractions` taking a batch of `ExtractionRecord`s (the CONTRACT.md
   body, unchanged — it becomes the request payload instead of a file),
   `?dry_run=true` returning the same plan report without inserting.
   `plan()`/`execute()` are already separate, so this is a thin layer.
2. **Idempotency across the wire.** Retries and double-sends are a fact of
   service-to-service calls. `nlp_record_ledger` already handles this: a replayed
   batch returns `already_loaded` per record rather than duplicating rows. This
   is the main reason the ledger was worth building up front.
3. **A caller.** Someone has to sequence NER → normalize → load. Either the
   chart-review platform calls the normalizer then calls us, or a thin
   orchestrator does. **Worth settling explicitly** — with three independent
   services and no named orchestrator, this step tends to end up owned by nobody.
4. **Batch semantics.** Partial failure in a batch: all-or-nothing (current
   behaviour — one transaction, rollback on error), or per-record best-effort
   with a per-record status list? Per-record status is friendlier for a caller
   retrying only what failed. Needs deciding before the endpoint is published.
5. **The CLI stays.** It is how the service is developed, tested, and demoed
   without the other two services running. Option A does not go away; it becomes
   the local mode.

Deliberately *not* adopted: no message broker, no shared database between
services, no shared Python package imported by the others. Each service owns its
own storage and speaks only over the contract.

## Original recommendation (superseded)

**Stay on A now. Plan for D as the second step, not C or B.**

The reasoning: the expensive uncertainty right now isn't transport, it's whether
the three formats actually line up (`note_id` identity, the span join key, who
owns unit normalization). Option A surfaces those mismatches in the cheapest
possible way — a file that doesn't parse. Adopting B or C before those are settled
means debugging format problems through a build system or a network.

Once the formats are stable and the annotator workflow needs shared state, D adds
real value, and the migration is small if `ExtractionRecord` is already the
document shape. That's why it's worth keeping the contract stable now even though
Option A doesn't strictly require it.

**What to ask for in the meeting:**

1. Xuguang's repo link, and whether the normalizer is an importable package, a
   CLI, or a hosted service. If in-process, the A→D path stays simple; if hosted,
   we need a local mapping cache regardless of which option we pick.
2. 5–10 real chart-review output files on synthetic notes — actual files, not a
   spec. The adapter gets written against those.
3. Agreement that `ExtractionRecord` ([CONTRACT.md](CONTRACT.md)) is the seam,
   whatever transport we end up choosing.
