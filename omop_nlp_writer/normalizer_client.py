"""Calling concept-normalizer to turn a source term into an OMOP concept.

The dependency is deliberately one-way and loose:

  * one-way — the normalizer knows nothing about the CDM, so any project without a
    database can use it.  Nothing here may leak back into it.
  * loose — imported lazily and optional.  The writer must stay usable when a
    record already carries a concept_id (which is how it was originally built and
    how it is tested), so a missing normalizer is a reported condition rather than
    an import error at startup.

Two resolution paths, both belonging to the normalizer:

    reviewed alias table   "mmse_score" -> 4169175    a decision, signed off
    search the target      "Pack years" -> 4151768    for anything not in the table

A reviewed "no suitable concept" is distinct from "not found" and is passed
through as such, so the writer can report the difference rather than treating a
deliberate decision as a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class NormalizerUnavailable(RuntimeError):
    """concept-normalizer is not importable."""


@dataclass(slots=True)
class Resolved:
    concept_id: int | None
    status: str
    detail: str | None = None

    @property
    def is_reviewed_nonmapping(self) -> bool:
        """Someone checked and concluded the target has nothing suitable."""
        return self.status == "not_in_target"


class NormalizerClient:
    """Thin wrapper over concept_normalizer.normalize()."""

    def __init__(
        self,
        vocab_path: Path,
        *,
        target: str = "OMOP",
        alias_table: str | Path | None = None,
        value_alias_table: str | Path | None = None,
        unit_alias_table: str | Path | None = None,
        package_path: Path | None = None,
    ):
        self.vocab_path = vocab_path
        self.target_name = target
        self._cache: dict[str, Resolved] = {}
        self._value_cache: dict[str, Resolved] = {}
        self._unit_cache: dict[str, Resolved] = {}

        try:
            import concept_normalizer  # noqa: F401
        except ImportError:
            # Allow a sibling checkout without an install step, since neither repo
            # is packaged yet.
            import sys

            candidates = [package_path] if package_path else []
            candidates += [
                Path.home() / "Downloads" / "concept-normalizer",
                vocab_path.parent.parent.parent / "concept-normalizer",
            ]
            for candidate in candidates:
                if candidate and (candidate / "concept_normalizer").is_dir():
                    sys.path.insert(0, str(candidate))
                    break
            try:
                import concept_normalizer  # noqa: F401
            except ImportError as exc:
                raise NormalizerUnavailable(
                    "concept-normalizer is not importable. Clone "
                    "github.com/apanduri/concept-normalizer next to this repo, or "
                    "pass --normalizer-path."
                ) from exc

        from concept_normalizer import OmopVocabulary
        from concept_normalizer import aliases as alias_mod

        vocabs = None
        if target.upper() != "OMOP":
            vocabs = tuple(v.strip() for v in target.split(",") if v.strip())
        self._target = OmopVocabulary(vocab_path, vocabulary_ids=vocabs)

        def _table(spec):
            if not spec:
                return None
            p = Path(spec)
            return alias_mod.load(p) if p.exists() else alias_mod.load_builtin(str(spec))

        self._aliases = _table(alias_table)
        self._value_aliases = _table(value_alias_table)
        self._unit_aliases = _table(unit_alias_table)

        # Values and units live in DIFFERENT OMOP domains from events. The concept
        # for "smoking status" is an Observation; the concept for "Former smoker"
        # is a Meas Value, which is what value_as_concept_id points at. Searching
        # the event domains for a value finds nothing at all, so each needs its own
        # target. Built lazily — most loads never need them.
        from concept_normalizer import unit_target, value_target

        self._value_target_factory = lambda: value_target(vocab_path)
        self._unit_target_factory = lambda: unit_target(vocab_path)
        self._value_target = None
        self._unit_target = None

        from concept_normalizer import normalize

        self._normalize = normalize

    @property
    def alias_summary(self) -> str:
        return "no alias table" if self._aliases is None else repr(self._aliases)

    def resolve(self, term: str) -> Resolved:
        if term in self._cache:
            return self._cache[term]
        result = self._normalize(term, self._target, aliases=self._aliases)
        resolved = Resolved(
            concept_id=result.concept.concept_id if result.concept else None,
            status=result.status.value,
            detail=result.detail,
        )
        self._cache[term] = resolved
        return resolved

    def resolve_value(self, field_id: str, answer: object) -> Resolved:
        """A categorical ANSWER -> a value concept for value_as_concept_id.

        Keyed "<field_id>.<answer>" because the same answer text means different
        things under different fields ("former" under smoking_status is not
        "former" under anything else).
        """
        key = f"{field_id}.{str(answer).strip().lower()}"
        if key in self._value_cache:
            return self._value_cache[key]
        if self._value_target is None:
            self._value_target = self._value_target_factory()
        result = self._normalize(key, self._value_target, aliases=self._value_aliases)
        if result.concept is None and self._value_aliases is not None:
            # No entry under the compound key: fall back to searching the answer
            # text itself in the value domains ("Former smoker" is findable).
            if self._value_aliases.get(key) is None:
                result = self._normalize(str(answer), self._value_target)
        resolved = Resolved(
            concept_id=result.concept.concept_id if result.concept else None,
            status=result.status.value,
            detail=result.detail,
        )
        self._value_cache[key] = resolved
        return resolved

    def resolve_unit(self, field_id: str) -> Resolved:
        """A field's unit -> a unit concept for unit_concept_id."""
        if field_id in self._unit_cache:
            return self._unit_cache[field_id]
        if self._unit_target is None:
            self._unit_target = self._unit_target_factory()
        result = self._normalize(field_id, self._unit_target, aliases=self._unit_aliases)
        resolved = Resolved(
            concept_id=result.concept.concept_id if result.concept else None,
            status=result.status.value,
            detail=result.detail,
        )
        self._unit_cache[field_id] = resolved
        return resolved

    def close(self) -> None:
        for target in (self._target, self._value_target, self._unit_target):
            close = getattr(target, "close", None)
            if close:
                close()
