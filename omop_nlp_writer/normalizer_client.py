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
        package_path: Path | None = None,
    ):
        self.vocab_path = vocab_path
        self.target_name = target
        self._cache: dict[str, Resolved] = {}

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

        self._aliases = None
        if alias_table:
            as_path = Path(alias_table)
            self._aliases = (
                alias_mod.load(as_path) if as_path.exists()
                else alias_mod.load_builtin(str(alias_table))
            )

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

    def close(self) -> None:
        close = getattr(self._target, "close", None)
        if close:
            close()
