"""밸류체인 섹터 순환 전략 (Sector Rotation Backtester).

KIS Open API로 종목 가격 + 외인/기관 수급을 받아 섹터 단위로 자금 흐름을 추적하고,
한 번에 한 섹터 종목을 보유하다가 (a) 수급 약화 (b) +40% 익절 (c) -10% 손절 시 회전.
"""

from .engine import SectorRotationBacktest, SectorRotationConfig
from .metrics import compute_metrics
