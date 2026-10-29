"""Phase 4: sealed historical test protocols, robustness criteria and audit metadata.

The research window is selected without viewing the newest (holdout) results.
Each genome can consume its one-time holdout only once; using new fees, code or
candles must NOT silently replace a failed (or passed) old test verdict.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

PROTOCOL_VERSION = 4
DAY_MS = 86_400_000


def finite_metrics(metrics: dict) -> bool:
    required = ("total_return", "hold_return", "max_drawdown", "sharpe",
                "profit_factor", "n_trades", "fees_paid")
    return all(k in metrics and isinstance(metrics[k], (int, float))
               and math.isfinite(metrics[k]) for k in required)


def validate_performance(metrics: dict, gate: dict, prefix: str) -> list[str]:
    """Independent holdout/validation checks, on already-fee-adjusted metrics."""
    reasons = []
    if not finite_metrics(metrics):
        return [f"{prefix}: missing or nonfinite audited risk metrics"]
    r = metrics["total_return"]
    if r <= 0:
        reasons.append(f"lost money on {prefix}")
    trades_key = "min_validation_trades" if prefix == "validation" else "min_test_trades"
    need = int(gate.get(trades_key, 0))
    if metrics["n_trades"] < need:
        reasons.append(f"only {metrics['n_trades']} trades on {prefix} (need {need})")
    drawdown_key = "max_validation_drawdown_pct" if prefix == "validation" else "max_test_drawdown_pct"
    dd_limit = gate.get(drawdown_key)
    if dd_limit is not None and metrics["max_drawdown"] > dd_limit / 100:
        reasons.append(f"drawdown {metrics['max_drawdown']:.0%} on {prefix} (limit {dd_limit}%)")
    pf_key = "min_validation_profit_factor" if prefix == "validation" else "min_test_profit_factor"
    min_pf = float(gate.get(pf_key, 0))
    if min_pf > 0 and metrics["profit_factor"] < min_pf:
        reasons.append(f"profit factor {metrics['profit_factor']:.2f} on {prefix} (need {min_pf:g})")
    if metrics["sharpe"] < float(gate.get("min_test_sharpe" if prefix == "unseen data"
                                               else "min_validation_sharpe", -999)):
        reasons.append(f"insufficient risk-adjusted return on {prefix}")
    if prefix == "unseen data" and gate.get("test_must_beat_hold", False) and (
            r <= metrics["hold_return"]):
        reasons.append("failed to beat buy-and-hold after fees on unseen data")
    return reasons


def rules_hash(config: dict) -> str:
    frozen = {
        "backtest": config["backtest"], "execution": config.get("execution", {}),
        "gate": config["evolution"].get("gate", {}),
        "protocol": PROTOCOL_VERSION,
    }
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode())
    bot_dir = Path(__file__).resolve().parents[1]
    for path in ("backtest/engine.py", "execution/model.py", "strategy/signals.py",
                 "strategy/context.py", "strategy/indicators.py", "evolution/runner.py",
                 "evolution/verification.py", "data/loader.py"):
        digest.update(path.encode())
        digest.update((bot_dir / path).read_bytes())
    return digest.hexdigest()


def seal(config: dict, splits, series, genome) -> dict:
    """Audit fingerprint of the exact unseen candles and execution rules.

    Called ONLY after a genome is chosen and the test verdict is obtained.
    The resulting hash is an audit identifier, not a source of training input.
    """
    a = series.index_of(splits.validation_end)
    b = series.index_of(splits.end)
    digest = hashlib.sha256()
    for item in (series.ts, series.open, series.high, series.low, series.close, series.volume):
        digest.update(np.ascontiguousarray(item[a:b]).tobytes())
    rule_digest = rules_hash(config)
    return {
        "protocol": PROTOCOL_VERSION,
        "pair": genome.pair, "timeframe": genome.timeframe,
        "test_start_ms": splits.validation_end, "test_end_ms": splits.end,
        "test_candles": b - a,
        "test_data_sha256": digest.hexdigest(),
        "rules_sha256": rule_digest,
        "holdout_epoch": splits.validation_end // (30 * DAY_MS),
    }


def holdout_used(conn, epoch: int) -> int:
    """Count the one-time attempts in this holdout epoch, across all runs."""
    used = 0
    for (blob,) in conn.execute("SELECT test_json FROM candidates"):
        try:
            audit = json.loads(blob).get("_audit", {})
        except (ValueError, TypeError):
            continue
        if audit.get("holdout_epoch") == epoch:
            used += 1
    return used
