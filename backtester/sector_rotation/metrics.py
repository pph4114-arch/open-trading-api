"""백테스트 성과 지표."""

from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd


def compute_metrics(equity_curve: pd.DataFrame, cycles: List, trading_days_per_year: int = 252) -> Dict[str, float]:
    """주요 성과 지표 계산.

    Args:
        equity_curve: index=date, columns 포함: equity
        cycles: Cycle list (engine.Cycle)
        trading_days_per_year: 연환산 기준일

    Returns:
        dict of metrics
    """
    if equity_curve.empty:
        return {}

    eq = equity_curve["equity"].astype(float)
    initial = float(eq.iloc[0])
    final = float(eq.iloc[-1])
    total_return = final / initial - 1.0

    days = (eq.index[-1] - eq.index[0]).days
    years = days / 365.25 if days > 0 else 1.0
    cagr = (final / initial) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    daily_ret = eq.pct_change().dropna()
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        sharpe = (daily_ret.mean() / daily_ret.std()) * math.sqrt(trading_days_per_year)
    else:
        sharpe = 0.0

    downside = daily_ret[daily_ret < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = (daily_ret.mean() / downside.std()) * math.sqrt(trading_days_per_year)
    else:
        sortino = 0.0

    # Max drawdown
    running_max = eq.cummax()
    drawdown = eq / running_max - 1.0
    max_dd = float(drawdown.min())

    # Cycle stats
    cycle_returns = [c.return_pct for c in cycles] if cycles else []
    n_cycles = len(cycle_returns)
    wins = [r for r in cycle_returns if r > 0]
    losses = [r for r in cycle_returns if r <= 0]
    win_rate = len(wins) / n_cycles if n_cycles > 0 else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (math.inf if wins else 0.0)
    avg_cycle = float(np.mean(cycle_returns)) if cycle_returns else 0.0

    exit_reasons: Dict[str, int] = {}
    for c in cycles:
        exit_reasons[c.exit_reason] = exit_reasons.get(c.exit_reason, 0) + 1

    return {
        "initial_capital": initial,
        "final_equity": final,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": max_dd,
        "n_cycles": n_cycles,
        "win_rate": win_rate,
        "avg_cycle_return": avg_cycle,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": float(profit_factor) if math.isfinite(profit_factor) else float("inf"),
        "exit_reasons": exit_reasons,
        "n_days": int(days),
    }


def format_metrics_table(metrics: Dict) -> str:
    """사람이 읽기 좋은 표 문자열."""
    if not metrics:
        return "(no data)"
    fmt = lambda v, kind: (
        f"{v:>15,.0f}" if kind == "money"
        else f"{v:>14.2%}" if kind == "pct"
        else f"{v:>15.3f}" if kind == "ratio"
        else f"{v:>15}"
    )
    lines = [
        "─" * 50,
        "백테스트 성과 요약",
        "─" * 50,
        f"  초기자본         : {fmt(metrics['initial_capital'], 'money')} 원",
        f"  최종평가         : {fmt(metrics['final_equity'], 'money')} 원",
        f"  총수익률         : {fmt(metrics['total_return'], 'pct')}",
        f"  CAGR             : {fmt(metrics['cagr'], 'pct')}",
        f"  Sharpe           : {fmt(metrics['sharpe'], 'ratio')}",
        f"  Sortino          : {fmt(metrics['sortino'], 'ratio')}",
        f"  MDD              : {fmt(metrics['max_drawdown'], 'pct')}",
        "─" * 50,
        f"  사이클 수        : {fmt(metrics['n_cycles'], 'int')}",
        f"  승률             : {fmt(metrics['win_rate'], 'pct')}",
        f"  평균 사이클 수익 : {fmt(metrics['avg_cycle_return'], 'pct')}",
        f"  평균 승          : {fmt(metrics['avg_win'], 'pct')}",
        f"  평균 패          : {fmt(metrics['avg_loss'], 'pct')}",
        f"  Profit Factor    : {fmt(metrics['profit_factor'], 'ratio')}",
        "─" * 50,
        "  청산 사유 분포   :",
    ]
    for reason, n in metrics.get("exit_reasons", {}).items():
        lines.append(f"    {reason:18s} : {n:>5d} 회")
    lines.append("─" * 50)
    return "\n".join(lines)
