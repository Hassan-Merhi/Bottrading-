"""The graveyard: every dead bot, why it died, and what the market was doing.

New bots that look too much like bots that died for the same reason are
penalized, so the population stops repeating known mistakes.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import numpy as np

from bots.strategy.genome import CATEGORICAL_GENES, NUMERIC_GENES, Genome

# Diagnoses that describe being beaten rather than a mistake. They are logged
# but do not make similar genomes look bad.
NOT_A_MISTAKE = {"outcompeted by stronger bots"}


def _encode(g: Genome) -> tuple[np.ndarray, np.ndarray]:
    d = g.to_dict()
    cats = np.array([CATEGORICAL_GENES[k].index(d[k]) for k in CATEGORICAL_GENES])
    nums = np.array([(d[k] - lo) / (hi - lo) for k, (lo, hi, _) in NUMERIC_GENES.items()])
    return cats, nums


class Graveyard:
    def __init__(self, conn: sqlite3.Connection, gcfg: dict):
        self.conn = conn
        self.radius = gcfg["similarity_radius"]
        self.saturation = gcfg["saturation"]
        self.max_penalty = gcfg["penalty"]
        self._cats: list[np.ndarray] = []
        self._nums: list[np.ndarray] = []
        self._diag: list[str] = []
        self._matrix: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        for row in conn.execute("SELECT genome_json, diagnosis FROM graveyard"):
            self._remember(Genome.from_dict(json.loads(row[0])), row[1])

    def __len__(self) -> int:
        return len(self._diag)

    def _remember(self, g: Genome, diagnosis: str) -> None:
        if diagnosis in NOT_A_MISTAKE:
            return
        cats, nums = _encode(g)
        self._cats.append(cats)
        self._nums.append(nums)
        self._diag.append(diagnosis)
        self._matrix = None

    def bury(self, run_id: str, generation: int, g: Genome, cause: str, diagnosis: str,
             regime: str, volatility: float, fitness: float, metrics: dict) -> None:
        self.conn.execute(
            "INSERT INTO graveyard (run_id, generation, genome_json, cause, diagnosis, regime, "
            "volatility, fitness, metrics_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, generation, g.to_json(), cause, diagnosis, regime, volatility, fitness,
             json.dumps(metrics), datetime.now(timezone.utc).isoformat()),
        )
        self._remember(g, diagnosis)

    def penalty(self, g: Genome) -> tuple[float, str | None]:
        """Penalty for resembling bots that died of the same cause, and that cause."""
        if not self._diag:
            return 0.0, None
        if self._matrix is None:
            self._matrix = (np.array(self._cats), np.array(self._nums), np.array(self._diag))
        cats, nums, diag = self._matrix
        c, n = _encode(g)
        dist = ((cats != c).sum(axis=1) + np.abs(nums - n).sum(axis=1)) / (len(c) + len(n))
        near = diag[dist < self.radius]
        if near.size == 0:
            return 0.0, None
        cause, count = Counter(near.tolist()).most_common(1)[0]
        return self.max_penalty * min(1.0, count / self.saturation), cause


def failure_summary(generation: int, diagnoses: list[str], top: int = 3) -> str:
    """Short readable summary of why bots died this generation."""
    if not diagnoses:
        return f"Generation {generation}: no deaths."
    counts = Counter(diagnoses).most_common(top)
    parts = ", ".join(f"{reason} ({n})" for reason, n in counts)
    return f"Generation {generation}: {len(diagnoses)} bots retired. Main causes: {parts}."
