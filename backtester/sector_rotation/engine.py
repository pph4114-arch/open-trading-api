"""밸류체인 섹터 순환 백테스트 엔진.

규칙
----
1. 진입: 외인+기관 수급이 가장 강한 섹터를 선택, 그 섹터의 후보 종목 중
         5일 누적 (외인+기관) 순매수 거래대금 Top-N 종목에 균등 가중 매수.
2. 청산/회전 (어느 조건이든 먼저 발동):
   (a) 보유 섹터의 sector_strength (모든 종목 5일 누적 순매수 합) 가 임계값 이하
   (b) 사이클 누적 수익률 ≥ +40%  → 익절
   (c) 사이클 누적 수익률 ≤ -10%  → 손절
3. 회전: 청산 다음 영업일에 그 시점 가장 강한 섹터로 진입 (이전 섹터는 후보에서 제외하지 않음 — 신호가 다시 살아나면 재진입 가능).
4. 매매: 전일 종가까지의 신호로 다음 영업일 시가에 체결 (look-ahead 방지).
5. 비용: 매매대금의 transaction_cost (기본 0.25%) 차감 (수수료+세금 통합 근사).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from .data_fetcher import StockMeta

logger = logging.getLogger(__name__)


@dataclass
class SectorRotationConfig:
    flow_window_days: int = 5           # 수급 누적 윈도우
    top_n_within_sector: int = 5        # 섹터 내 Top-N 종목
    take_profit_pct: float = 0.40       # 익절 임계
    stop_loss_pct: float = -0.10        # 손절 임계
    sector_strength_min: float = 0.0    # 보유 섹터 강도 < 이 값이면 회전
    entry_strength_min: float = 0.0     # 진입 시 최소 섹터 강도
    transaction_cost: float = 0.0025    # 편도 0.25% (왕복 0.5%) 근사 — 매수+매도 시 각각 차감
    initial_capital: float = 100_000_000


@dataclass
class Trade:
    date: pd.Timestamp
    action: str           # "buy" or "sell"
    code: str
    name: str
    sector: str
    tier: str
    price: float
    shares: int
    value: float          # gross
    cost: float           # transaction cost paid
    reason: str           # entry / take_profit / stop_loss / sector_weak


@dataclass
class Cycle:
    sector: str
    entry_date: pd.Timestamp
    exit_date: Optional[pd.Timestamp]
    entry_value: float
    exit_value: float
    return_pct: float
    exit_reason: str
    holdings: List[str] = field(default_factory=list)   # codes


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame      # date, equity, cash, position_value, sector, return
    trades: List[Trade]
    cycles: List[Cycle]
    config: SectorRotationConfig

    def to_dataframes(self):
        trades_df = pd.DataFrame([t.__dict__ for t in self.trades])
        cycles_df = pd.DataFrame([c.__dict__ for c in self.cycles])
        return self.equity_curve, trades_df, cycles_df


class SectorRotationBacktest:
    def __init__(
        self,
        data: Dict[str, Dict[str, object]],
        config: Optional[SectorRotationConfig] = None,
    ):
        """
        Args:
            data: DataFetcher.fetch_universe() 반환값
                  { code: {"price": price_df, "flow": flow_df, "meta": StockMeta} }
            config: 백테스트 파라미터
        """
        self.data = data
        self.config = config or SectorRotationConfig()
        self._build_panels()

    # ----------------------- Panel construction -----------------------

    def _build_panels(self):
        """모든 종목의 가격·수급을 (date × code) 와이드 패널로 정렬."""
        closes, opens, foreigns, institutions = {}, {}, {}, {}
        meta_by_code: Dict[str, StockMeta] = {}
        for code, bundle in self.data.items():
            price: pd.DataFrame = bundle["price"]
            flow: pd.DataFrame = bundle["flow"]
            meta: StockMeta = bundle["meta"]
            if price.empty or flow.empty:
                logger.warning("skip %s: empty price or flow", code)
                continue
            p = price.set_index("date")
            f = flow.set_index("date")
            closes[code] = p["close"]
            opens[code] = p["open"]
            foreigns[code] = f["foreign_net"]
            institutions[code] = f["institution_net"]
            meta_by_code[code] = meta

        self.close_df = pd.DataFrame(closes).sort_index()
        self.open_df = pd.DataFrame(opens).sort_index().reindex(self.close_df.index)
        self.foreign_df = pd.DataFrame(foreigns).reindex(self.close_df.index)
        self.institution_df = pd.DataFrame(institutions).reindex(self.close_df.index)
        self.meta_by_code = meta_by_code

        # 섹터 구성
        self.sector_to_codes: Dict[str, List[str]] = {}
        for code, m in meta_by_code.items():
            self.sector_to_codes.setdefault(m.sector, []).append(code)

        # 종목별 수급 합 (외인+기관) 의 N일 누적
        net_flow = self.foreign_df.add(self.institution_df, fill_value=0.0)
        self.stock_flow_window = net_flow.rolling(self.config.flow_window_days, min_periods=1).sum()

        # 섹터별 강도 = 섹터 내 모든 종목 stock_flow_window 의 일별 합
        sector_strength = {}
        for sector, codes in self.sector_to_codes.items():
            cols = [c for c in codes if c in self.stock_flow_window.columns]
            if not cols:
                continue
            sector_strength[sector] = self.stock_flow_window[cols].sum(axis=1, skipna=True)
        self.sector_strength_df = pd.DataFrame(sector_strength)

    # ----------------------- Run -----------------------

    def run(self, start: Optional[date] = None, end: Optional[date] = None) -> BacktestResult:
        cfg = self.config
        dates = self.close_df.index
        if start is not None:
            dates = dates[dates >= pd.Timestamp(start)]
        if end is not None:
            dates = dates[dates <= pd.Timestamp(end)]
        if len(dates) == 0:
            raise ValueError("백테스트 기간에 데이터가 없습니다.")

        cash = cfg.initial_capital
        positions: Dict[str, int] = {}        # code → shares
        current_sector: Optional[str] = None
        cycle_entry_value: float = 0.0        # 사이클 진입 시점 포지션 평가액
        cycle_entry_date: Optional[pd.Timestamp] = None
        cycle_holdings: List[str] = []
        pending_action: Optional[Dict] = None  # 다음 영업일 시가에 실행할 액션

        trades: List[Trade] = []
        cycles: List[Cycle] = []
        equity_rows: List[Dict] = []

        date_list = list(dates)
        for i, d in enumerate(date_list):
            # 1) 전일 신호로 결정된 액션을 오늘 시가에 실행
            if pending_action is not None:
                if pending_action["type"] == "exit":
                    cash, exit_value = self._execute_exit(d, positions, trades, reason=pending_action["reason"])
                    cycles.append(Cycle(
                        sector=current_sector,
                        entry_date=cycle_entry_date,
                        exit_date=d,
                        entry_value=cycle_entry_value,
                        exit_value=exit_value,
                        return_pct=(exit_value / cycle_entry_value - 1.0) if cycle_entry_value > 0 else 0.0,
                        exit_reason=pending_action["reason"],
                        holdings=cycle_holdings.copy(),
                    ))
                    positions = {}
                    current_sector = None
                    cycle_entry_value = 0.0
                    cycle_entry_date = None
                    cycle_holdings = []
                elif pending_action["type"] == "enter":
                    sector = pending_action["sector"]
                    picks = pending_action["picks"]
                    cash, positions, entry_value = self._execute_entry(
                        d, sector, picks, cash, trades
                    )
                    if positions:
                        current_sector = sector
                        cycle_entry_value = entry_value
                        cycle_entry_date = d
                        cycle_holdings = list(positions.keys())
                pending_action = None

            # 2) 오늘 종가 기준 평가 + 신호 생성
            equity, position_value = self._mark_to_market(d, positions, cash)
            cycle_return = 0.0
            if cycle_entry_value > 0 and positions:
                cycle_return = position_value / cycle_entry_value - 1.0

            decision = self._decide(d, current_sector, cycle_return, positions)
            if decision is not None:
                pending_action = decision

            equity_rows.append({
                "date": d,
                "equity": equity,
                "cash": cash,
                "position_value": position_value,
                "sector": current_sector,
                "cycle_return": cycle_return,
            })

        # 마지막 날 강제 청산 (백테스트 종료)
        if positions:
            last_d = date_list[-1]
            cash, exit_value = self._execute_exit(last_d, positions, trades, reason="end_of_backtest")
            cycles.append(Cycle(
                sector=current_sector,
                entry_date=cycle_entry_date,
                exit_date=last_d,
                entry_value=cycle_entry_value,
                exit_value=exit_value,
                return_pct=(exit_value / cycle_entry_value - 1.0) if cycle_entry_value > 0 else 0.0,
                exit_reason="end_of_backtest",
                holdings=cycle_holdings.copy(),
            ))
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["position_value"] = 0.0
            equity_rows[-1]["equity"] = cash

        equity_df = pd.DataFrame(equity_rows).set_index("date")
        equity_df["equity"] = equity_df["equity"].ffill()
        return BacktestResult(equity_curve=equity_df, trades=trades, cycles=cycles, config=cfg)

    # ----------------------- Decision -----------------------

    def _decide(
        self,
        today: pd.Timestamp,
        current_sector: Optional[str],
        cycle_return: float,
        positions: Dict[str, int],
    ) -> Optional[Dict]:
        cfg = self.config
        if current_sector is None or not positions:
            # 진입 신호
            best_sector, picks = self._select_entry(today)
            if best_sector is None:
                return None
            return {"type": "enter", "sector": best_sector, "picks": picks}

        # 보유 중 — 청산 트리거 체크 (우선순위: SL → TP → 섹터 약화)
        if cycle_return <= cfg.stop_loss_pct:
            return {"type": "exit", "reason": "stop_loss"}
        if cycle_return >= cfg.take_profit_pct:
            return {"type": "exit", "reason": "take_profit"}

        strength = self.sector_strength_df.loc[today, current_sector] if (
            today in self.sector_strength_df.index and current_sector in self.sector_strength_df.columns
        ) else float("nan")
        if pd.notna(strength) and strength <= cfg.sector_strength_min:
            return {"type": "exit", "reason": "sector_weak"}

        return None

    def _select_entry(self, today: pd.Timestamp):
        cfg = self.config
        if today not in self.sector_strength_df.index:
            return None, []
        row = self.sector_strength_df.loc[today].dropna()
        if row.empty:
            return None, []
        row = row.sort_values(ascending=False)
        best_sector = row.index[0]
        best_strength = row.iloc[0]
        if best_strength < cfg.entry_strength_min:
            return None, []

        # Top-N 종목 (5일 누적 순매수)
        codes_in_sector = self.sector_to_codes.get(best_sector, [])
        if not codes_in_sector or today not in self.stock_flow_window.index:
            return best_sector, []
        flow_row = self.stock_flow_window.loc[today, [c for c in codes_in_sector if c in self.stock_flow_window.columns]]
        flow_row = flow_row.dropna()
        flow_row = flow_row[flow_row > 0]
        if flow_row.empty:
            return best_sector, []
        picks = flow_row.sort_values(ascending=False).head(cfg.top_n_within_sector).index.tolist()
        return best_sector, picks

    # ----------------------- Execution -----------------------

    def _execute_entry(
        self,
        today: pd.Timestamp,
        sector: str,
        picks: List[str],
        cash: float,
        trades: List[Trade],
    ):
        cfg = self.config
        if not picks:
            return cash, {}, 0.0

        # 시가 부재 종목 제거
        valid_picks: List[str] = []
        for code in picks:
            if today in self.open_df.index and code in self.open_df.columns:
                px = self.open_df.loc[today, code]
                if pd.notna(px) and px > 0:
                    valid_picks.append(code)
        if not valid_picks:
            return cash, {}, 0.0

        # 균등 가중 (수수료 고려해 안전 마진 적용)
        budget_per_stock = (cash * (1 - cfg.transaction_cost)) / len(valid_picks)
        positions: Dict[str, int] = {}
        total_spent = 0.0
        total_cost = 0.0
        for code in valid_picks:
            px = float(self.open_df.loc[today, code])
            shares = int(budget_per_stock // px)
            if shares <= 0:
                continue
            value = shares * px
            cost = value * cfg.transaction_cost
            positions[code] = shares
            total_spent += value
            total_cost += cost
            meta = self.meta_by_code[code]
            trades.append(Trade(
                date=today, action="buy", code=code, name=meta.name,
                sector=meta.sector, tier=meta.tier,
                price=px, shares=shares, value=value, cost=cost, reason="entry",
            ))
        cash -= (total_spent + total_cost)
        # 진입 평가액은 매수 직후 시점의 포지션 가치
        return cash, positions, total_spent

    def _execute_exit(
        self,
        today: pd.Timestamp,
        positions: Dict[str, int],
        trades: List[Trade],
        reason: str,
    ):
        cfg = self.config
        total_proceeds = 0.0
        total_cost = 0.0
        for code, shares in positions.items():
            # 시가가 없으면 종가로 대체
            px = float("nan")
            if today in self.open_df.index and code in self.open_df.columns:
                px = self.open_df.loc[today, code]
            if pd.isna(px) or px <= 0:
                if today in self.close_df.index and code in self.close_df.columns:
                    px = self.close_df.loc[today, code]
            if pd.isna(px) or px <= 0:
                continue
            value = shares * float(px)
            cost = value * cfg.transaction_cost
            total_proceeds += value
            total_cost += cost
            meta = self.meta_by_code[code]
            trades.append(Trade(
                date=today, action="sell", code=code, name=meta.name,
                sector=meta.sector, tier=meta.tier,
                price=float(px), shares=shares, value=value, cost=cost, reason=reason,
            ))
        cash_after = total_proceeds - total_cost
        return cash_after, total_proceeds  # exit_value = gross proceeds (수수료 차감 전 포지션 가치)

    def _mark_to_market(self, today: pd.Timestamp, positions: Dict[str, int], cash: float):
        if not positions:
            return cash, 0.0
        position_value = 0.0
        for code, shares in positions.items():
            if today in self.close_df.index and code in self.close_df.columns:
                px = self.close_df.loc[today, code]
                if pd.notna(px):
                    position_value += shares * float(px)
        return cash + position_value, position_value
