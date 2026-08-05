#!/usr/bin/env python3
"""How much of the BSO-AD ontology already exists in the OMOP vocabulary?

Hongyu's position is that "OMOP already contains suitable concepts in most
cases".  This measures it, so the group can decide how much mapping work is real
and how many custom concepts are actually needed.

Deliberately conservative: this reports what an *exact, case-insensitive name
match* finds.  It is NOT a normalizer and must not be used as one — a real
matcher needs context, synonyms and human review.  Read the output as a floor on
coverage, not an answer.

    python3 scripts/bso_ad_coverage.py \
        --ontology ~/Downloads/chart-review-platform/vendor/bso-ad-sdk/ontology/concepts.json \
        --vocab vocab/concept.db \
        --out build/bso_ad_coverage.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Domains a clinical fact can actually be written to (mirrors domains.py).
WRITABLE_DOMAINS = {
    "Observation", "Measurement", "Condition", "Drug",
    "Procedure", "Device", "Specimen",
}


def load_ontology(path: Path) -> list[dict]:
    """Flatten BSO-AD's nested subtrees into (id, label, subtree, depth)."""
    doc = json.loads(path.read_text())
    out: list[dict] = []

    def walk(node: object, subtree: str) -> None:
        if isinstance(node, dict):
            if "id" in node and "label" in node:
                out.append(
                    {
                        "bso_id": str(node["id"]),
                        "label": str(node["label"]),
                        "subtree": subtree,
                        "depth": node.get("depth"),
                        "parent_label": node.get("parent_label"),
                    }
                )
            for value in node.values():
                walk(value, subtree)
        elif isinstance(node, list):
            for item in node:
                walk(item, subtree)

    for key, value in doc.items():
        if key == "_meta":
            continue
        walk(value, key)
    return out


def humanize(label: str) -> str:
    """BSO-AD labels use underscores ('Exercise_equipment')."""
    return label.replace("_", " ").strip()


class Matcher:
    def __init__(self, vocab_path: Path):
        self.conn = sqlite3.connect(f"file:{vocab_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        # Name lookups need an index the CP build doesn't create; build it in a
        # temp in-memory index rather than mutating the read-only vocabulary.
        self.conn.execute("PRAGMA temp_store = MEMORY")
        print("[coverage] indexing concept names (one-off, ~1 min)...", file=sys.stderr)
        self.conn.execute("ATTACH DATABASE ':memory:' AS idx")
        self.conn.execute(
            """CREATE TABLE idx.name_lookup AS
               SELECT concept_id, LOWER(concept_name) AS lname, domain_id,
                      vocabulary_id, standard_concept
                 FROM concept
                WHERE standard_concept = 'S'"""
        )
        self.conn.execute("CREATE INDEX idx.i_name ON name_lookup(lname)")
        n = self.conn.execute("SELECT COUNT(*) c FROM idx.name_lookup").fetchone()["c"]
        print(f"[coverage] indexed {n:,} standard concepts", file=sys.stderr)
        self._build_word_index()

    def exact(self, term: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT concept_id, lname, domain_id, vocabulary_id
                 FROM idx.name_lookup WHERE lname = ?""",
            (term.lower(),),
        ).fetchall()

    def _build_word_index(self) -> None:
        """Inverted word -> concept index, for cheap partial matching.

        495 `LIKE '%term%'` scans over 3.5M rows takes minutes; one pass building
        a word index and intersecting in memory takes seconds.
        """
        self._by_word: dict[str, list[tuple[int, str, str, str]]] = {}
        rows = self.conn.execute(
            """SELECT concept_id, lname, domain_id, vocabulary_id
                 FROM idx.name_lookup
                WHERE domain_id IN ('Observation','Condition','Measurement')"""
        ).fetchall()
        for r in rows:
            entry = (r["concept_id"], r["lname"], r["domain_id"], r["vocabulary_id"])
            for word in set(re.findall(r"[a-z0-9]+", r["lname"])):
                self._by_word.setdefault(word, []).append(entry)
        print(f"[coverage] word index over {len(rows):,} clinical concepts "
              f"({len(self._by_word):,} words)", file=sys.stderr)

    def substring(self, term: str, limit: int = 3) -> list[dict]:
        """Concepts whose name CONTAINS the term.

        Reported separately and never treated as a match: substring hits are
        frequently the wrong concept.  "Treadmill" finds "Treadmill speed
        achieved" (a Measurement), and "Homeless" finds "Education about
        healthcare for the homeless program" — neither is what the ontology
        means.  This tier measures how much a human would have to adjudicate.
        """
        needle = term.lower()
        words = re.findall(r"[a-z0-9]+", needle)
        if not words:
            return []
        # Start from the rarest word to keep the candidate set small.
        pivot = min(words, key=lambda w: len(self._by_word.get(w, ())))
        out = []
        for cid, lname, domain, vocab in self._by_word.get(pivot, ()):
            if needle in lname:
                out.append({"concept_id": cid, "lname": lname,
                            "domain_id": domain, "vocabulary_id": vocab})
                if len(out) >= limit:
                    break
        return out

    def close(self) -> None:
        self.conn.close()


def classify(label: str, matcher: Matcher) -> dict:
    term = humanize(label)
    rows = matcher.exact(term)
    writable = [r for r in rows if r["domain_id"] in WRITABLE_DOMAINS]

    if not rows:
        near = matcher.substring(term)
        if near:
            return {"status": "near_match_needs_review", "n_candidates": len(near),
                    "concept_id": near[0]["concept_id"], "domain_id": near[0]["domain_id"],
                    "vocabulary_id": near[0]["vocabulary_id"],
                    "near_example": near[0]["lname"]}
        return {"status": "no_match", "n_candidates": 0, "concept_id": None,
                "domain_id": None, "vocabulary_id": None}
    if not writable:
        # Matched, but only in domains a clinical fact cannot be written to
        # (Geography, Metadata, Meas Value, ...).
        r = rows[0]
        return {"status": "match_unwritable_domain", "n_candidates": len(rows),
                "concept_id": r["concept_id"], "domain_id": r["domain_id"],
                "vocabulary_id": r["vocabulary_id"]}
    if len({r["domain_id"] for r in writable}) > 1 or len(writable) > 1:
        r = writable[0]
        return {"status": "ambiguous", "n_candidates": len(writable),
                "concept_id": r["concept_id"], "domain_id": r["domain_id"],
                "vocabulary_id": r["vocabulary_id"]}
    r = writable[0]
    return {"status": "clean_match", "n_candidates": 1, "concept_id": r["concept_id"],
            "domain_id": r["domain_id"], "vocabulary_id": r["vocabulary_id"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ontology", type=Path, required=True)
    ap.add_argument("--vocab", type=Path, default=ROOT / "vocab" / "concept.db")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "bso_ad_coverage.csv")
    args = ap.parse_args()

    concepts = load_ontology(args.ontology)
    if not concepts:
        print(f"error: no concepts found in {args.ontology}", file=sys.stderr)
        return 2

    matcher = Matcher(args.vocab)
    results = []
    for c in concepts:
        row = {"near_example": None, **c, **classify(c["label"], matcher)}
        row.setdefault("near_example", None)
        results.append(row)
    matcher.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]))
        w.writeheader()
        w.writerows(results)

    # ---------------------------------------------------------------- report
    total = len(results)
    counts = Counter(r["status"] for r in results)
    print(f"\nBSO-AD ontology vs OMOP vocabulary")
    print(f"  ontology : {args.ontology}")
    print(f"  vocabulary: {args.vocab}")
    print(f"\n{total} BSO-AD concepts, matched by exact name against standard OMOP concepts:\n")
    labels = {
        "clean_match": "exactly one standard concept in a writable domain",
        "ambiguous": "several standard candidates — needs a human or context",
        "near_match_needs_review": "only partial-name hits, often the wrong concept",
        "match_unwritable_domain": "matched only in a non-clinical domain",
        "no_match": "nothing resembling it in OMOP",
    }
    for status in ("clean_match", "ambiguous", "near_match_needs_review",
                   "match_unwritable_domain", "no_match"):
        n = counts.get(status, 0)
        print(f"  {status:<26} {n:>5}  ({n/total:>5.1%})  {labels[status]}")

    auto = counts.get("clean_match", 0)
    review = counts.get("ambiguous", 0) + counts.get("near_match_needs_review", 0)
    none = counts.get("no_match", 0) + counts.get("match_unwritable_domain", 0)
    print(f"\n  -> {auto}/{total} ({auto/total:.1%}) could map automatically")
    print(f"  -> {review}/{total} ({review/total:.1%}) need human adjudication")
    print(f"  -> {none}/{total} ({none/total:.1%}) need a custom concept or a "
          f"broader-term decision")

    print("\nBy subtree:")
    print(f"  {'subtree':<52} {'total':>6} {'clean':>6} {'none':>6}")
    by_subtree: dict[str, Counter] = {}
    for r in results:
        by_subtree.setdefault(r["subtree"], Counter())[r["status"]] += 1
    for subtree, c in sorted(by_subtree.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {subtree[:51]:<52} {sum(c.values()):>6} "
              f"{c.get('clean_match', 0):>6} {c.get('no_match', 0):>6}")

    print("\nExamples of clean matches:")
    for r in [x for x in results if x["status"] == "clean_match"][:8]:
        print(f"  {humanize(r['label'])[:34]:<36} -> {r['concept_id']:>9}  "
              f"{r['domain_id']} / {r['vocabulary_id']}")
    print("\nExamples needing review — note how often the partial hit is the WRONG concept:")
    for r in [x for x in results if x["status"] == "near_match_needs_review"][:8]:
        print(f"  {humanize(r['label'])[:26]:<28} ~ {str(r.get('near_example'))[:46]}")
    print("\nExamples with nothing close (custom concept candidates):")
    for r in [x for x in results if x["status"] == "no_match"][:8]:
        print(f"  {humanize(r['label'])[:34]:<36} (under {r['subtree'][:28]})")

    print(f"\nFull results: {args.out}")
    print("NOTE: exact-name matching only — a floor on coverage, not a normalizer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
