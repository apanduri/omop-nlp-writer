"""CLI: python -m omop_nlp_writer <command>

    build-vocab   build the synthetic dev vocabulary (or verify a real one)
    init-cdm      create the CDM 5.4 subset and load synthetic PERSON/NOTE rows
    records       join the two pipelines' outputs into ExtractionRecords
    load          write NOTE_NLP + domain rows      (--dry-run by default)
    verify        report what is in the CDM, split by provenance
    unload        reverse a load using the ledger
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .adapters import build_records, records_to_json
from .cdm import connect, init_schema
from .domains import DOMAIN_TARGETS, NLP_ID_BASE, NLP_TYPE_CONCEPT_ID
from .record import ContractError, load_records
from .vocab import VocabLookup, build_from_csv
from .writer import CdmNlpWriter, Disposition, LoadReport, Reason

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CDM = ROOT / "build" / "cdm.db"
DEFAULT_VOCAB = ROOT / "build" / "vocab.db"
DEFAULT_MAPS_TO = ROOT / "vocab" / "maps_to.db"
DEFAULT_VOCAB_CSV = ROOT / "fixtures" / "vocab_mini.csv"
DEFAULT_NOTES = ROOT / "fixtures" / "notes" / "notes.json"
DEFAULT_CHART_REVIEW = ROOT / "fixtures" / "chart_review_output"
DEFAULT_NORMALIZER = ROOT / "fixtures" / "normalizer_output"

EHR_TYPE_CONCEPT_ID = 32817  # "EHR" — used only to seed comparison rows


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_build_vocab(args: argparse.Namespace) -> int:
    n = build_from_csv(args.csv, args.vocab)
    print(f"[vocab] built {args.vocab} with {n} concepts from {args.csv}")
    lookup = VocabLookup(args.vocab)
    nlp_type = lookup.find(NLP_TYPE_CONCEPT_ID)
    if nlp_type is None:
        print(
            f"[vocab] WARNING: NLP provenance concept {NLP_TYPE_CONCEPT_ID} is not in this "
            f"vocabulary. Domain rows would carry an unresolvable "
            f"*_type_concept_id. Verify domains.NLP_TYPE_CONCEPT_ID against the real "
            f"vocabulary:\n"
            f"    SELECT concept_id, concept_name FROM concept\n"
            f"     WHERE vocabulary_id = 'Type Concept' AND concept_name LIKE '%NLP%';"
        )
    else:
        print(f"[vocab] NLP provenance concept {NLP_TYPE_CONCEPT_ID} = {nlp_type.concept_name!r}")
    lookup.close()
    return 0


def cmd_init_cdm(args: argparse.Namespace) -> int:
    conn = connect(args.cdm, create=True)
    init_schema(conn)
    print(f"[cdm] schema ready at {args.cdm}")

    doc = json.loads(args.notes.read_text())
    persons, notes = doc.get("persons", []), doc.get("notes", [])

    # Upstream ids may be strings ("2024-12-04__pathology_report", "SYNTH-001").
    # CDM surrogate keys are integers, so we allocate one per source id and record
    # the original in *_source_value — that column IS the crosswalk.
    person_keys: dict[object, int] = {}
    for i, p in enumerate(persons, start=1):
        raw = p["person_id"]
        key = raw if isinstance(raw, int) else i
        person_keys[raw] = key
        conn.execute(
            """INSERT OR REPLACE INTO person
               (person_id, gender_concept_id, year_of_birth, race_concept_id,
                ethnicity_concept_id, person_source_value)
               VALUES (?, ?, ?, 0, 0, ?)""",
            (
                key,
                p["gender_concept_id"],
                p["year_of_birth"],
                p.get("person_source_value") or str(raw),
            ),
        )
        # An observation period is required for OHDSI cohort SQL to find anyone.
        conn.execute(
            """INSERT OR REPLACE INTO observation_period
               (observation_period_id, person_id, observation_period_start_date,
                observation_period_end_date, period_type_concept_id)
               VALUES (?, ?, ?, ?, ?)""",
            (p["person_id"], p["person_id"], "2015-01-01", "2026-12-31", EHR_TYPE_CONCEPT_ID),
        )

    next_note_key = 1
    crosswalked = 0
    for n in notes:
        raw = n["note_id"]
        if isinstance(raw, int):
            note_key = raw
        else:
            note_key = next_note_key
            next_note_key += 1
            crosswalked += 1
        conn.execute(
            """INSERT OR REPLACE INTO note
               (note_id, person_id, note_date, note_datetime, note_type_concept_id,
                note_class_concept_id, note_title, note_text, encoding_concept_id,
                language_concept_id, note_source_value)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0, 0, ?)""",
            (
                note_key,
                person_keys.get(n["person_id"], n["person_id"]),
                n["note_date"],
                n.get("note_datetime"),
                EHR_TYPE_CONCEPT_ID,
                n.get("note_title"),
                n.get("note_text"),
                # The crosswalk: extractions arriving with the upstream string id
                # resolve through this column.
                str(raw),
            ),
        )
    conn.commit()
    conn.close()
    print(f"[cdm] loaded {len(persons)} persons, {len(notes)} notes from {args.notes}")
    if crosswalked:
        print(f"[cdm] {crosswalked} note(s) had non-integer source ids — allocated CDM")
        print("      keys and recorded the originals in NOTE.note_source_value")
    return 0


def cmd_records(args: argparse.Namespace) -> int:
    records, warnings = build_records(args.chart_review, args.normalizer)
    payload = {"records": records_to_json(records)}
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"[records] {len(records)} ExtractionRecords -> {args.out}")
    else:
        print(text, end="")
    for w in warnings:
        print(f"[records] warn: {w}", file=sys.stderr)
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    if args.extractions:
        try:
            records = load_records(args.extractions)
        except ContractError as exc:
            print(f"[load] contract error: {exc}", file=sys.stderr)
            return 2
        warnings: list[str] = []
    else:
        records, warnings = build_records(args.chart_review, args.normalizer)

    for w in warnings:
        print(f"[load] warn: {w}")

    with CdmNlpWriter(
        args.cdm,
        args.vocab,
        confidence_threshold=args.min_confidence,
        relationship_path=args.maps_to,
    ) as writer:
        if writer.vocab.relationship_warning:
            print(f"[load] warn: {writer.vocab.relationship_warning}")
        report = writer.plan(records)
        _print_report(report, dry_run=not args.commit)
        if args.commit:
            writer.execute(report)
            print(f"\n[load] committed to {args.cdm}")
        else:
            print("\n[load] DRY RUN — nothing written. Re-run with --commit to insert.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    conn = connect(args.cdm)
    print(f"CDM: {args.cdm}\n")

    n_notes = conn.execute("SELECT COUNT(*) c FROM note").fetchone()["c"]
    n_nlp = conn.execute("SELECT COUNT(*) c FROM note_nlp").fetchone()["c"]
    print(f"NOTE rows      : {n_notes}")
    print(f"NOTE_NLP rows  : {n_nlp}")

    print("\nDomain rows by provenance (the 'distinguishable from EHR' requirement):")
    print(f"  {'table':<24} {'NLP-derived':>12} {'other/EHR':>11}")
    for target in DOMAIN_TARGETS.values():
        try:
            nlp = conn.execute(
                f"SELECT COUNT(*) c FROM {target.table} "
                f"WHERE {target.type_concept_column} = ?",
                (NLP_TYPE_CONCEPT_ID,),
            ).fetchone()["c"]
            other = conn.execute(
                f"SELECT COUNT(*) c FROM {target.table} "
                f"WHERE {target.type_concept_column} != ?",
                (NLP_TYPE_CONCEPT_ID,),
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            continue
        if nlp or other:
            print(f"  {target.table:<24} {nlp:>12} {other:>11}")

    print("\nLedger (record_id -> rows produced):")
    rows = conn.execute(
        """SELECT lexical_variant, concept_id, domain_id, note_nlp_id,
                  domain_table, domain_row_id
             FROM nlp_record_ledger ORDER BY note_id, span_offset"""
    ).fetchall()
    if not rows:
        print("  (empty — nothing loaded yet)")
    for r in rows:
        domain = (
            f"{r['domain_table']}#{r['domain_row_id']}" if r["domain_table"] else "— (evidence only)"
        )
        print(
            f"  {r['lexical_variant']:<18} concept={r['concept_id'] or 0:<10} "
            f"{(r['domain_id'] or '-'):<12} note_nlp#{r['note_nlp_id']} -> {domain}"
        )

    print(f"\nAll NLP surrogate keys are > {NLP_ID_BASE:,} by construction.")
    conn.close()
    return 0


def cmd_unload(args: argparse.Namespace) -> int:
    with CdmNlpWriter(args.cdm, args.vocab, relationship_path=args.maps_to) as writer:
        deleted = writer.unload(nlp_system=args.nlp_system)
    if not deleted:
        print("[unload] nothing to remove")
    for table, n in sorted(deleted.items()):
        print(f"[unload] deleted {n} rows from {table}")
    return 0


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _print_report(report: LoadReport, *, dry_run: bool) -> None:
    verb = "would write" if dry_run else "wrote"
    print(f"\n{'=' * 78}")
    print(f"{len(report.planned)} extraction records")
    print(f"{'=' * 78}")

    for planned in report.planned:
        r = planned.record
        icon = {
            Disposition.WRITTEN: "OK  ",
            Disposition.NOTE_NLP_ONLY: "NLP ",
            Disposition.SKIPPED: "SKIP",
        }[planned.disposition]
        note_ref = str(r.note_id)
        if planned.resolved_note_id is not None and not isinstance(r.note_id, int):
            note_ref += f" -> NOTE#{planned.resolved_note_id}"
        head = f"{icon} note {note_ref} @{r.span.omop_offset:<10} {r.lexical_variant!r}"
        print(f"\n{head}")
        if planned.disposition is Disposition.SKIPPED:
            print(f"      skipped: {planned.reason.value}"
                  + (f" — {planned.detail}" if planned.detail else ""))
            continue

        concept = f"{r.concept_id or 0}"
        if planned.concept_name:
            concept += f" ({planned.concept_name})"
        print(f"      NOTE_NLP    : note_nlp_id={planned.note_nlp_id} "
              f"term_exists={'Y' if r.term_exists else 'N'} concept={concept}")

        if planned.disposition is Disposition.NOTE_NLP_ONLY:
            print(f"      no domain row: {planned.reason.value}"
                  + (f" — {planned.detail}" if planned.detail else ""))
            continue

        assert planned.target is not None and planned.domain_row is not None
        print(f"      domain      : {planned.domain_id} -> {planned.target.table.upper()}")
        for col, val in planned.domain_row.items():
            if val is None:
                continue
            marker = "  <-- NLP provenance" if col == planned.target.type_concept_column else ""
            print(f"        {col:<32} = {val}{marker}")

    print(f"\n{'-' * 78}")
    print(f"{verb}: {report.note_nlp_rows} NOTE_NLP rows, {len(report.written)} domain rows")
    print(f"  domain rows written      : {len(report.written)}")
    print(f"  NOTE_NLP only (evidence) : {len(report.note_nlp_only)}")
    print(f"  skipped entirely         : {len(report.skipped)}")
    by_reason: dict[str, int] = {}
    for p in report.planned:
        if p.reason is not Reason.OK:
            by_reason[p.reason.value] = by_reason.get(p.reason.value, 0) + 1
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    - {reason:<32} {n}")
    print("-" * 78)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m omop_nlp_writer", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--cdm", type=Path, default=DEFAULT_CDM, help="CDM SQLite path")
        sp.add_argument("--vocab", type=Path, default=DEFAULT_VOCAB, help="concept.db path")
        sp.add_argument("--maps-to", type=Path, default=None,
                        help="relationship DB with 'Maps to' rows; defaults to a "
                             "maps_to.db sibling of --vocab")

    bv = sub.add_parser("build-vocab", help="build the synthetic dev vocabulary")
    bv.add_argument("--csv", type=Path, default=DEFAULT_VOCAB_CSV)
    bv.add_argument("--vocab", type=Path, default=DEFAULT_VOCAB)
    bv.set_defaults(func=cmd_build_vocab)

    ic = sub.add_parser("init-cdm", help="create CDM tables and load synthetic notes")
    ic.add_argument("--cdm", type=Path, default=DEFAULT_CDM)
    ic.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ic.set_defaults(func=cmd_init_cdm)

    rc = sub.add_parser("records", help="join both pipelines into ExtractionRecords")
    rc.add_argument("--chart-review", type=Path, default=DEFAULT_CHART_REVIEW)
    rc.add_argument("--normalizer", type=Path, default=DEFAULT_NORMALIZER)
    rc.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    rc.set_defaults(func=cmd_records)

    ld = sub.add_parser("load", help="write NOTE_NLP + domain rows")
    add_common(ld)
    ld.add_argument("--extractions", type=Path,
                    help="ExtractionRecord JSON (file or dir); skips the adapters")
    ld.add_argument("--chart-review", type=Path, default=DEFAULT_CHART_REVIEW)
    ld.add_argument("--normalizer", type=Path, default=DEFAULT_NORMALIZER)
    ld.add_argument("--min-confidence", type=float, default=0.0,
                    help="normalizer score floor for writing a domain row (0 = off)")
    ld.add_argument("--commit", action="store_true",
                    help="actually insert; omit for a dry run")
    ld.set_defaults(func=cmd_load)

    vf = sub.add_parser("verify", help="report CDM contents by provenance")
    vf.add_argument("--cdm", type=Path, default=DEFAULT_CDM)
    vf.set_defaults(func=cmd_verify)

    ul = sub.add_parser("unload", help="reverse a load using the ledger")
    add_common(ul)
    ul.add_argument("--nlp-system", help="only remove rows from this nlp_system")
    ul.set_defaults(func=cmd_unload)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ContractError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
