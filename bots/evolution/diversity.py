"""Bounded, deterministic diversity in strategy discovery.

Every search continues to rank candidates by training fitness, but multiple
independent pairs and strategy families should be represented in the elite
pool and the shortlist. Diversity never bypasses validation or test gates.
"""
from __future__ import annotations

from collections import Counter
from math import ceil
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def diverse_top(items: Sequence[T], count: int, key: Callable[[T], tuple[str, str]],
                max_pair_share: float = 0.4, max_family_share: float = 0.2) -> list[T]:
    """Take fit-ranked items with pair/family caps; relax caps if the pool lacks variety.

    Input must already be ordered by decreasing fitness. Always preserve the
    top-ranked item, and fill vacancies from the same ranking rather than
    reducing population size or inventing extra candidates. No randomness.
    """
    if count <= 0:
        return []
    if not (0 < max_pair_share <= 1 and 0 < max_family_share <= 1):
        raise ValueError("diversity share caps must be between 0 and 1")
    count = min(count, len(items))
    pair_limit = max(1, ceil(count * max_pair_share))
    family_limit = max(1, ceil(count * max_family_share))
    selected: list[T] = []
    indices: set[int] = set()
    pair_count: Counter[str] = Counter()
    family_count: Counter[tuple[str, str]] = Counter()
    for strict in (True, False):
        for idx, item in enumerate(items):
            if len(selected) == count:
                break
            if idx in indices:
                continue
            pair, family = key(item)
            if strict and (pair_count[pair] >= pair_limit or
                           family_count[(pair, family)] >= family_limit):
                continue
            selected.append(item)
            indices.add(idx)
            pair_count[pair] += 1
            family_count[(pair, family)] += 1
    return selected


def coverage(genomes) -> dict:
    """Compact, truthful coverage summary for a ranked population."""
    pairs: Counter[str] = Counter()
    families: Counter[str] = Counter()
    for genome in genomes:
        pairs[genome.pair] += 1
        families[genome.entry_type] += 1
    return {"pairs": dict(sorted(pairs.items())),
            "entry_types": dict(sorted(families.items()))}
