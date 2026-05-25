"""KIS Open API 데이터 페처 (가격 + 외인/기관 수급).

- 가격: inquire_daily_itemchartprice (100건/호출 → 페이지네이션)
- 수급: investor_trade_by_stock_daily (래퍼가 tr_cont 자동 페이지네이션)

모든 응답은 종목별 parquet 파일로 캐시한다. 동일 기간 재실행 시 API 호출 없이 캐시만 읽는다.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_kis_modules():
    """examples_llm 의 KIS 인증/API 래퍼를 동적으로 import.

    각 example 파일은 sys.path에 '../..' 와 '.' 를 추가하는 패턴이라 격리해서 로드한다.
    """
    paths = [
        REPO_ROOT / "examples_llm",
        REPO_ROOT / "examples_llm" / "domestic_stock" / "inquire_daily_itemchartprice",
        REPO_ROOT / "examples_llm" / "domestic_stock" / "investor_trade_by_stock_daily",
    ]
    for p in paths:
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)

    import kis_auth as ka  # noqa: E402
    from inquire_daily_itemchartprice import inquire_daily_itemchartprice  # noqa: E402
    from investor_trade_by_stock_daily import investor_trade_by_stock_daily  # noqa: E402

    return ka, inquire_daily_itemchartprice, investor_trade_by_stock_daily


@dataclass
class StockMeta:
    code: str
    name: str
    sector: str
    tier: str


def load_value_chain(yaml_path: Path) -> List[StockMeta]:
    """value_chain.yaml → flat list of StockMeta."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out: List[StockMeta] = []
    for sector, tiers in data.items():
        for tier, stocks in tiers.items():
            for s in stocks:
                out.append(StockMeta(code=s["code"], name=s["name"], sector=sector, tier=tier))
    return out


class DataFetcher:
    """KIS 데이터 페처 (캐시 우선).

    Usage:
        fetcher = DataFetcher(cache_dir=Path("cache"))
        fetcher.authenticate()
        prices = fetcher.fetch_prices("005930", date(2023,1,1), date(2024,12,31))
        flow = fetcher.fetch_investor_flow("005930", date(2023,1,1), date(2024,12,31))
    """

    PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "trade_value"]
    FLOW_COLUMNS = ["date", "foreign_net", "institution_net", "trade_value"]

    def __init__(self, cache_dir: Path, throttle_sec: float = 0.25):
        self.cache_dir = Path(cache_dir)
        self.price_dir = self.cache_dir / "prices"
        self.flow_dir = self.cache_dir / "flows"
        self.price_dir.mkdir(parents=True, exist_ok=True)
        self.flow_dir.mkdir(parents=True, exist_ok=True)
        self.throttle_sec = throttle_sec
        self._authenticated = False
        self._ka = None
        self._fn_price = None
        self._fn_flow = None

    def authenticate(self):
        if self._authenticated:
            return
        ka, fn_price, fn_flow = _import_kis_modules()
        ka.auth()
        self._ka = ka
        self._fn_price = fn_price
        self._fn_flow = fn_flow
        self._authenticated = True
        logger.info("KIS authenticated")

    # ----------------------- Prices -----------------------

    def fetch_prices(self, code: str, start: date, end: date, force_refresh: bool = False) -> pd.DataFrame:
        cache_path = self.price_dir / f"{code}.parquet"
        if cache_path.exists() and not force_refresh:
            df = pd.read_parquet(cache_path)
            if not df.empty and df["date"].min().date() <= start and df["date"].max().date() >= end:
                return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))].reset_index(drop=True)

        self.authenticate()
        df = self._fetch_prices_paginated(code, start, end)
        if not df.empty:
            df.to_parquet(cache_path, index=False)
        return df

    def _fetch_prices_paginated(self, code: str, start: date, end: date) -> pd.DataFrame:
        """100건/호출 → 종료일을 역방향으로 이동시키며 페이지네이션."""
        all_chunks: List[pd.DataFrame] = []
        current_end = end
        guard = 0
        while current_end >= start and guard < 60:
            guard += 1
            current_start = max(start, current_end - timedelta(days=140))
            try:
                _, df2 = self._fn_price(
                    env_dv="real",
                    fid_cond_mrkt_div_code="J",
                    fid_input_iscd=code,
                    fid_input_date_1=current_start.strftime("%Y%m%d"),
                    fid_input_date_2=current_end.strftime("%Y%m%d"),
                    fid_period_div_code="D",
                    fid_org_adj_prc="0",
                )
            except Exception as e:
                logger.warning("price fetch failed for %s [%s~%s]: %s", code, current_start, current_end, e)
                break

            if df2 is None or df2.empty:
                break

            chunk = self._normalize_price_df(df2)
            if chunk.empty:
                break

            all_chunks.append(chunk)
            earliest = chunk["date"].min().date()
            if earliest <= start:
                break
            current_end = earliest - timedelta(days=1)
            time.sleep(self.throttle_sec)

        if not all_chunks:
            return pd.DataFrame(columns=self.PRICE_COLUMNS)
        out = pd.concat(all_chunks, ignore_index=True)
        out = out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))].reset_index(drop=True)
        return out

    @staticmethod
    def _normalize_price_df(raw: pd.DataFrame) -> pd.DataFrame:
        if "stck_bsop_date" not in raw.columns:
            return pd.DataFrame(columns=DataFetcher.PRICE_COLUMNS)
        df = pd.DataFrame({
            "date": pd.to_datetime(raw["stck_bsop_date"], format="%Y%m%d", errors="coerce"),
            "open": pd.to_numeric(raw.get("stck_oprc"), errors="coerce"),
            "high": pd.to_numeric(raw.get("stck_hgpr"), errors="coerce"),
            "low": pd.to_numeric(raw.get("stck_lwpr"), errors="coerce"),
            "close": pd.to_numeric(raw.get("stck_clpr"), errors="coerce"),
            "volume": pd.to_numeric(raw.get("acml_vol"), errors="coerce"),
            "trade_value": pd.to_numeric(raw.get("acml_tr_pbmn"), errors="coerce"),
        }).dropna(subset=["date", "close"])
        return df

    # ----------------------- Investor Flow -----------------------

    def fetch_investor_flow(self, code: str, start: date, end: date, force_refresh: bool = False) -> pd.DataFrame:
        cache_path = self.flow_dir / f"{code}.parquet"
        if cache_path.exists() and not force_refresh:
            df = pd.read_parquet(cache_path)
            if not df.empty and df["date"].min().date() <= start and df["date"].max().date() >= end:
                return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))].reset_index(drop=True)

        self.authenticate()
        df = self._fetch_flow_paginated(code, start, end)
        if not df.empty:
            df.to_parquet(cache_path, index=False)
        return df

    def _fetch_flow_paginated(self, code: str, start: date, end: date) -> pd.DataFrame:
        """투자자 매매동향: fid_input_date_1 을 종료점으로 두고 KIS 래퍼가 역방향 페이지네이션.

        래퍼 max_depth=10 (≈ 100건). 더 긴 기간이 필요하면 청크 단위 반복.
        """
        all_chunks: List[pd.DataFrame] = []
        current_end = end
        guard = 0
        while current_end >= start and guard < 30:
            guard += 1
            try:
                df1, _ = self._fn_flow(
                    fid_cond_mrkt_div_code="J",
                    fid_input_iscd=code,
                    fid_input_date_1=current_end.strftime("%Y%m%d"),
                    fid_org_adj_prc="",
                    fid_etc_cls_code="",
                    max_depth=10,
                )
            except Exception as e:
                logger.warning("flow fetch failed for %s end=%s: %s", code, current_end, e)
                break
            if df1 is None or df1.empty:
                break
            chunk = self._normalize_flow_df(df1)
            if chunk.empty:
                break
            all_chunks.append(chunk)
            earliest = chunk["date"].min().date()
            if earliest <= start:
                break
            current_end = earliest - timedelta(days=1)
            time.sleep(self.throttle_sec)

        if not all_chunks:
            return pd.DataFrame(columns=self.FLOW_COLUMNS)
        out = pd.concat(all_chunks, ignore_index=True)
        out = out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))].reset_index(drop=True)
        return out

    @staticmethod
    def _normalize_flow_df(raw: pd.DataFrame) -> pd.DataFrame:
        if "stck_bsop_date" not in raw.columns:
            return pd.DataFrame(columns=DataFetcher.FLOW_COLUMNS)
        df = pd.DataFrame({
            "date": pd.to_datetime(raw["stck_bsop_date"], format="%Y%m%d", errors="coerce"),
            "foreign_net": pd.to_numeric(raw.get("frgn_ntby_tr_pbmn"), errors="coerce"),
            "institution_net": pd.to_numeric(raw.get("orgn_ntby_tr_pbmn"), errors="coerce"),
            "trade_value": pd.to_numeric(raw.get("acml_tr_pbmn"), errors="coerce"),
        }).dropna(subset=["date"])
        return df

    # ----------------------- Universe fetch -----------------------

    def fetch_universe(self, stocks: List[StockMeta], start: date, end: date, force_refresh: bool = False) -> Dict[str, Dict[str, pd.DataFrame]]:
        """전 종목 가격 + 수급을 받아 dict로 반환.

        Returns: { code: {"price": df, "flow": df, "meta": StockMeta} }
        """
        out: Dict[str, Dict[str, pd.DataFrame]] = {}
        for i, s in enumerate(stocks, 1):
            logger.info("[%d/%d] fetching %s (%s, %s/%s)", i, len(stocks), s.code, s.name, s.sector, s.tier)
            price = self.fetch_prices(s.code, start, end, force_refresh)
            flow = self.fetch_investor_flow(s.code, start, end, force_refresh)
            out[s.code] = {"price": price, "flow": flow, "meta": s}
        return out
