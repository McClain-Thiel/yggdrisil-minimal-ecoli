from dataclasses import dataclass
from pathlib import Path

import pytest

from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.scorers.base import ScoreCache, ScoreResult, ScorerSuite
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _CountingScorer:
    name: str = "counting"
    version: str = "1"
    config_hash: str = "config-a"
    calls: int = 0

    async def score(self, state: GenomeState) -> ScoreResult:
        self.calls += 1
        return ScoreResult(
            scorer=self.name,
            version=self.version,
            metrics={"deleted": len(state.deleted_genes)},
        )


@pytest.mark.asyncio
async def test_genome_size_scorer_returns_only_exact_gene_counts() -> None:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    scorer = GenomeSizeScorer(registry)

    result = await scorer.score(GenomeState(frozenset({"b0001"})))

    assert result.metrics == {"genes_deleted": 1, "genes_remaining": 2}
    assert result.coverage == {}


@pytest.mark.asyncio
async def test_cache_reuses_exact_identity_and_invalidates_version_or_config() -> None:
    state = GenomeState(frozenset({"b0001"}))
    cache = ScoreCache(":memory:")
    first = _CountingScorer()
    suite = ScorerSuite((first,), cache)

    await suite.score(state)
    cached = await suite.score(state)

    assert first.calls == 1
    assert cached["counting"].metrics == {"deleted": 1}

    version_changed = _CountingScorer(version="2")
    await ScorerSuite((version_changed,), cache).score(state)
    config_changed = _CountingScorer(config_hash="config-b")
    await ScorerSuite((config_changed,), cache).score(state)

    assert version_changed.calls == 1
    assert config_changed.calls == 1
    cache.close()


@pytest.mark.asyncio
async def test_cache_identity_is_derived_from_state() -> None:
    cache = ScoreCache(":memory:")
    scorer = _CountingScorer()
    suite = ScorerSuite((scorer,), cache)

    first = await suite.score(GenomeState(frozenset({"b0001"})))
    second = await suite.score(GenomeState(frozenset({"b0001", "b0002"})))

    assert scorer.calls == 2
    assert first["counting"].metrics == {"deleted": 1}
    assert second["counting"].metrics == {"deleted": 2}
    cache.close()


def test_suite_rejects_duplicate_scorer_names() -> None:
    cache = ScoreCache(":memory:")
    with pytest.raises(ValueError, match="unique"):
        ScorerSuite((_CountingScorer(), _CountingScorer()), cache)
    cache.close()
