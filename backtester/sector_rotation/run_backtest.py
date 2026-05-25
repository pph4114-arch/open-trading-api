"""CLI: 밸류체인 섹터 순환 백테스트 실행.

사용 예:
    python -m backtester.sector_rotation.run_backtest \
        --start 2023-01-01 --end 2024-12-31

    또는 backtester/ 경로에서:
    python sector_rotation/run_backtest.py --start 2023-01-01 --end 2024-12-31

결과:
    output/sector_rotation/
        equity_curve.csv
        trades.csv
        cycles.csv
        summary.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# 모듈 import 패스 보정 (스크립트로 직접 실행할 때)
_HERE = Path(__file__).resolve().parent
_BACKTESTER = _HERE.parent
if str(_BACKTESTER) not in sys.path:
    sys.path.insert(0, str(_BACKTESTER))

from sector_rotation.data_fetcher import DataFetcher, load_value_chain
from sector_rotation.engine import SectorRotationBacktest, SectorRotationConfig
from sector_rotation.metrics import compute_metrics, format_metrics_table


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    parser = argparse.ArgumentParser(description="밸류체인 섹터 순환 백테스트")
    parser.add_argument("--start", type=parse_date, default=parse_date("2023-01-01"))
    parser.add_argument("--end", type=parse_date, default=parse_date("2024-12-31"))
    parser.add_argument("--value-chain", type=Path, default=_HERE / "value_chain.yaml")
    parser.add_argument("--cache-dir", type=Path, default=_HERE / "cache")
    parser.add_argument("--output-dir", type=Path, default=_BACKTESTER / "examples" / "output" / "sector_rotation")
    parser.add_argument("--initial-capital", type=float, default=100_000_000)
    parser.add_argument("--flow-window", type=int, default=5)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--take-profit", type=float, default=0.40)
    parser.add_argument("--stop-loss", type=float, default=-0.10)
    parser.add_argument("--sector-strength-min", type=float, default=0.0)
    parser.add_argument("--transaction-cost", type=float, default=0.0025)
    parser.add_argument("--force-refresh", action="store_true", help="캐시 무시하고 KIS API 재호출")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s | %(levelname)s | %(message)s")
    log = logging.getLogger("sector_rotation")

    log.info("밸류체인 로드: %s", args.value_chain)
    stocks = load_value_chain(args.value_chain)
    log.info("총 %d 종목 (섹터: %s)", len(stocks), sorted({s.sector for s in stocks}))

    fetcher = DataFetcher(cache_dir=args.cache_dir)
    log.info("데이터 수집 (캐시: %s)", args.cache_dir)
    data = fetcher.fetch_universe(stocks, args.start, args.end, force_refresh=args.force_refresh)

    cfg = SectorRotationConfig(
        flow_window_days=args.flow_window,
        top_n_within_sector=args.top_n,
        take_profit_pct=args.take_profit,
        stop_loss_pct=args.stop_loss,
        sector_strength_min=args.sector_strength_min,
        transaction_cost=args.transaction_cost,
        initial_capital=args.initial_capital,
    )
    log.info("백테스트 시작: %s ~ %s", args.start, args.end)
    bt = SectorRotationBacktest(data=data, config=cfg)
    result = bt.run(args.start, args.end)

    metrics = compute_metrics(result.equity_curve, result.cycles)
    print()
    print(format_metrics_table(metrics))
    print()

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    eq_df, trades_df, cycles_df = result.to_dataframes()
    eq_df.to_csv(out_dir / "equity_curve.csv", encoding="utf-8-sig")
    trades_df.to_csv(out_dir / "trades.csv", encoding="utf-8-sig", index=False)
    cycles_df.to_csv(out_dir / "cycles.csv", encoding="utf-8-sig", index=False)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump({**metrics, "start": str(args.start), "end": str(args.end),
                   "config": cfg.__dict__}, f, ensure_ascii=False, indent=2, default=str)
    log.info("결과 저장: %s", out_dir)


if __name__ == "__main__":
    main()
