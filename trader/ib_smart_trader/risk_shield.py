"""
═══════════════════════════════════════════════════════════════════
  Risk Shield Module - 리스크 방어 시스템
  
  AVAV 사태에서 배운 3가지 방어 전략:
    1. 실적 발표 캘린더 필터 - 발표 당일 매수 차단 + 포지션 축소
    2. Beta 기반 포지션 사이징 - 변동성 높은 종목은 투자금 축소
    3. 실적 미스 패턴 필터 - 상습 미스 종목 감점/제외
  
  + 추가 방어:
    4. 섹터 집중 제한 - 섹터당 최대 종목 수 설정
    5. 일일 손실 한도 - 포트폴리오 전체 손실 제한
  
  Smart Trader 및 Auto Screener와 통합하여 사용
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
    HAS_IB = True
except ImportError:
    HAS_IB = False
    IB = None


logger = logging.getLogger("RiskShield")


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskShieldConfig:
    """리스크 방어 설정"""
    
    # ── 1. 실적 발표 캘린더 ──
    earnings_block_days_before: int = 1     # 발표 N일 전부터 매수 차단
    earnings_block_days_after: int = 1      # 발표 N일 후까지 매수 차단
    earnings_reduce_position_pct: float = 50.0  # 발표 전 포지션 축소 비율 (%)
    earnings_enabled: bool = True
    
    # ── 2. Beta 기반 포지션 사이징 ──
    beta_enabled: bool = True
    base_investment: float = 10000.0        # 기본 종목당 투자금
    beta_neutral: float = 1.0               # 기준 Beta (시장 평균)
    beta_scale_factor: float = 0.5          # 스케일링 강도 (0=무시, 1=완전비례)
    beta_max_multiplier: float = 1.5        # 최대 투자금 배수 (저Beta 종목)
    beta_min_multiplier: float = 0.3        # 최소 투자금 배수 (고Beta 종목)
    
    # ── 3. 실적 미스 패턴 ──
    miss_pattern_enabled: bool = True
    miss_lookback_quarters: int = 4         # 최근 N분기 확인
    miss_threshold: int = 2                 # N회 이상 미스 시 경고
    miss_penalty_score: float = 20.0        # 스크리너 점수 감점
    miss_block_threshold: int = 3           # N회 이상이면 매수 완전 차단
    
    # ── 4. 섹터 집중 제한 ──
    sector_limit_enabled: bool = True
    max_stocks_per_sector: int = 3          # 섹터당 최대 종목
    
    # ── 5. 일일 손실 한도 ──
    daily_loss_limit_enabled: bool = True
    daily_loss_limit_pct: float = -3.0      # 포트폴리오 -3% 시 전체 매매 중단


# ═══════════════════════════════════════════════════════════════
#  리스크 체크 결과
# ═══════════════════════════════════════════════════════════════

class RiskAction(Enum):
    ALLOW = "✅ 허용"
    REDUCE = "⚠️ 축소"
    BLOCK = "🚫 차단"


@dataclass 
class RiskCheckResult:
    """리스크 체크 결과"""
    symbol: str
    action: RiskAction = RiskAction.ALLOW
    reasons: list = field(default_factory=list)
    adjusted_investment: float = 0.0   # Beta 조정 후 투자금
    position_reduce_pct: float = 0.0   # 포지션 축소 비율
    score_penalty: float = 0.0         # 스크리너 점수 감점
    
    # 상세 정보
    has_earnings_soon: bool = False
    earnings_date: str = ""
    beta: float = 1.0
    earnings_miss_count: int = 0
    sector: str = ""
    sector_count: int = 0
    
    def __str__(self):
        flags = " | ".join(self.reasons) if self.reasons else "리스크 없음"
        return (
            f"{self.action.value} {self.symbol} | "
            f"투자금: ${self.adjusted_investment:,.0f} | "
            f"Beta: {self.beta:.2f} | "
            f"미스: {self.earnings_miss_count}/{4}분기 | "
            f"{flags}"
        )


# ═══════════════════════════════════════════════════════════════
#  1. 실적 발표 캘린더 필터
# ═══════════════════════════════════════════════════════════════

class EarningsCalendarFilter:
    """
    실적 발표 전후 매매 제한
    
    AVAV 교훈: 실적 발표 당일에 보유/매수하면
    시간외에서 -14% 급락을 고스란히 맞을 수 있음.
    → 발표 전 포지션 축소, 발표일 매수 차단
    """
    
    def __init__(self, ib=None):
        self.ib = ib
        self._cache = {}  # {symbol: earnings_date}
    
    def get_next_earnings_date(self, symbol: str) -> Optional[datetime]:
        """
        IB API로 다음 실적 발표일 조회
        캐시 사용으로 API 호출 최소화
        """
        if symbol in self._cache:
            cached = self._cache[symbol]
            # 캐시가 미래 날짜면 재사용
            if cached and cached > datetime.now():
                return cached
        
        if self.ib is None or not self.ib.isConnected():
            return None
        
        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            
            # IB의 fundamentalData에서 실적일정 조회
            # 또는 reqContractDetails의 nextEarningsDate 사용
            details = self.ib.reqContractDetails(contract)
            if details:
                # IB에서 제공하는 경우
                for d in details:
                    # contractDetails에 earningsDate가 있을 수 있음
                    pass
            
            # 대안: Wall Street Horizon 또는 자체 데이터 사용
            # 여기서는 IB에서 못 가져올 경우 None 반환
            self._cache[symbol] = None
            return None
            
        except Exception as e:
            logger.warning(f"  ⚠️ {symbol} 실적일정 조회 실패: {e}")
            return None
    
    def set_earnings_date(self, symbol: str, date: datetime):
        """수동으로 실적 발표일 설정 (API 불가 시)"""
        self._cache[symbol] = date
    
    def load_earnings_calendar(self, calendar: dict):
        """
        실적 캘린더 일괄 로드
        
        형식: {"AVAV": "2026-03-10", "MU": "2026-03-18", ...}
        """
        for symbol, date_str in calendar.items():
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                self._cache[symbol] = dt
            except ValueError:
                pass
        
        logger.info(f"  📅 실적 캘린더 로드: {len(calendar)}종목")
    
    def check(
        self, 
        symbol: str, 
        config: RiskShieldConfig,
    ) -> dict:
        """
        실적 발표 근접 여부 확인
        
        Returns:
            {
                "has_earnings_soon": bool,
                "earnings_date": str,
                "action": "BLOCK" | "REDUCE" | "ALLOW",
                "reduce_pct": float,
            }
        """
        if not config.earnings_enabled:
            return {"has_earnings_soon": False, "action": "ALLOW", "reduce_pct": 0}
        
        earnings_date = self.get_next_earnings_date(symbol)
        
        # 캐시에서 직접 확인
        if earnings_date is None and symbol in self._cache:
            earnings_date = self._cache.get(symbol)
        
        if earnings_date is None:
            return {"has_earnings_soon": False, "action": "ALLOW", "reduce_pct": 0}
        
        now = datetime.now()
        days_until = (earnings_date - now).days
        
        # 발표 당일 또는 직전: 매수 차단
        if -config.earnings_block_days_after <= days_until <= config.earnings_block_days_before:
            return {
                "has_earnings_soon": True,
                "earnings_date": earnings_date.strftime("%Y-%m-%d"),
                "action": "BLOCK",
                "reduce_pct": config.earnings_reduce_position_pct,
                "days_until": days_until,
            }
        
        # 발표 2~3일 전: 포지션 축소 권고
        if config.earnings_block_days_before < days_until <= config.earnings_block_days_before + 2:
            return {
                "has_earnings_soon": True,
                "earnings_date": earnings_date.strftime("%Y-%m-%d"),
                "action": "REDUCE",
                "reduce_pct": config.earnings_reduce_position_pct * 0.5,
                "days_until": days_until,
            }
        
        return {
            "has_earnings_soon": False,
            "earnings_date": earnings_date.strftime("%Y-%m-%d") if earnings_date else "",
            "action": "ALLOW",
            "reduce_pct": 0,
        }


# ═══════════════════════════════════════════════════════════════
#  2. Beta 기반 포지션 사이징
# ═══════════════════════════════════════════════════════════════

class BetaPositionSizer:
    """
    Beta 기반 동적 투자금 조절
    
    AVAV 교훈: Beta 2.21인 종목에 동일 금액을 투자하면
    -14% 하락 시 포트폴리오 타격이 Beta 1.0 종목의 2배.
    → Beta가 높으면 투자금을 줄여 리스크 균등화
    
    공식:
      조정 투자금 = 기본 투자금 × (기준Beta / 종목Beta)^스케일팩터
      → Beta 2.0 종목: $10,000 × (1.0/2.0)^0.5 = $7,071
      → Beta 0.5 종목: $10,000 × (1.0/0.5)^0.5 = $14,142 (cap 적용)
    """
    
    # 주요 종목 Beta 값 (사전 정의 + 동적 업데이트 가능)
    KNOWN_BETAS = {
        # Tech / AI
        "NVDA": 1.95, "AAPL": 1.18, "MSFT": 1.05, "GOOGL": 1.10,
        "AMZN": 1.15, "META": 1.35, "TSLA": 2.05, "TSM": 1.30,
        "AMD": 1.72, "INTC": 1.08, "MU": 1.45, "PLTR": 2.10,
        "APP": 2.50, "AVGO": 1.40, "ORCL": 1.12, "CRM": 1.25,
        
        # Energy
        "XOM": 0.85, "CVX": 0.90, "COP": 1.05, "OXY": 1.60,
        "DVN": 1.75, "EOG": 1.20, "SLB": 1.35, "BP": 0.80,
        "MPC": 1.15, "VLO": 1.30, "PSX": 1.10,
        
        # Defense
        "LMT": 0.55, "NOC": 0.50, "RTX": 0.75, "GD": 0.60,
        "BA": 1.45, "LHX": 0.70, "AVAV": 2.21, "KTOS": 1.80,
        
        # Tankers
        "FRO": 1.90, "DHT": 1.70, "INSW": 1.65, "STNG": 1.85,
        
        # Nuclear/Utilities
        "CEG": 1.30, "VST": 1.25, "GEV": 1.15, "NEE": 0.65,
        
        # Financials
        "JPM": 1.10, "GS": 1.35, "MS": 1.40, "BAC": 1.30,
        "WFC": 1.15, "BRK-B": 0.55, "AXP": 1.20,
        
        # Healthcare
        "UNH": 0.70, "JNJ": 0.55, "LLY": 0.75, "PFE": 0.65,
        "ABBV": 0.60, "MRK": 0.50, "AMGN": 0.55,
        
        # Consumer
        "WMT": 0.50, "COST": 0.75, "HD": 1.05, "TGT": 1.10,
        
        # Fintech
        "HOOD": 2.30, "MSTR": 3.50, "COIN": 2.80, "SQ": 2.15,
    }
    
    @classmethod
    def get_beta(cls, symbol: str) -> float:
        """종목 Beta 반환 (미등록 시 기본 1.0)"""
        return cls.KNOWN_BETAS.get(symbol, 1.0)
    
    @classmethod
    def calculate_position_size(
        cls,
        symbol: str,
        config: RiskShieldConfig,
    ) -> dict:
        """
        Beta 조정 투자금 계산
        
        Returns:
            {
                "beta": float,
                "base_investment": float,
                "adjusted_investment": float,
                "multiplier": float,
                "risk_level": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
            }
        """
        if not config.beta_enabled:
            return {
                "beta": 1.0,
                "base_investment": config.base_investment,
                "adjusted_investment": config.base_investment,
                "multiplier": 1.0,
                "risk_level": "MEDIUM",
            }
        
        beta = cls.get_beta(symbol)
        
        # 조정 배수 계산: (기준Beta / 종목Beta) ^ 스케일팩터
        if beta > 0:
            raw_multiplier = (config.beta_neutral / beta) ** config.beta_scale_factor
        else:
            raw_multiplier = 1.0
        
        # 최소/최대 제한
        multiplier = max(
            config.beta_min_multiplier,
            min(config.beta_max_multiplier, raw_multiplier)
        )
        
        adjusted = config.base_investment * multiplier
        
        # 리스크 레벨
        if beta <= 0.7:
            risk_level = "LOW"
        elif beta <= 1.3:
            risk_level = "MEDIUM"
        elif beta <= 2.0:
            risk_level = "HIGH"
        else:
            risk_level = "EXTREME"
        
        return {
            "beta": beta,
            "base_investment": config.base_investment,
            "adjusted_investment": round(adjusted, 2),
            "multiplier": round(multiplier, 3),
            "risk_level": risk_level,
        }


# ═══════════════════════════════════════════════════════════════
#  3. 실적 미스 패턴 필터
# ═══════════════════════════════════════════════════════════════

class EarningsMissFilter:
    """
    최근 N분기 실적 미스 패턴 감지
    
    AVAV 교훈: 최근 4분기 중 3분기 미스 → 또 미스할 확률 높음.
    → 상습 미스 종목은 스크리너 점수 감점 또는 제외
    """
    
    # 최근 4분기 실적 미스 이력 (True = 미스)
    # 실제로는 IB API 또는 외부 데이터로 자동 업데이트
    KNOWN_MISS_HISTORY = {
        "AVAV": [True, True, False, True],    # 4분기 중 3회 미스!
        "TSLA": [False, True, False, False],
        "INTC": [True, True, False, True],
        "BA":   [True, False, True, False],
        "PLTR": [False, False, False, False],  # 미스 없음
        "NVDA": [False, False, False, False],
        "XOM":  [False, False, True, False],
        "LMT":  [False, False, False, False],
        "HOOD": [False, True, False, True],
        "MSTR": [True, False, True, False],
    }
    
    @classmethod
    def get_miss_count(cls, symbol: str) -> int:
        """최근 4분기 실적 미스 횟수"""
        history = cls.KNOWN_MISS_HISTORY.get(symbol, [])
        return sum(1 for x in history if x)
    
    @classmethod
    def check(cls, symbol: str, config: RiskShieldConfig) -> dict:
        """
        실적 미스 패턴 확인
        
        Returns:
            {
                "miss_count": int,
                "miss_rate": float (0~1),
                "action": "BLOCK" | "PENALIZE" | "ALLOW",
                "penalty": float (스코어 감점),
            }
        """
        if not config.miss_pattern_enabled:
            return {"miss_count": 0, "action": "ALLOW", "penalty": 0}
        
        miss_count = cls.get_miss_count(symbol)
        total_quarters = config.miss_lookback_quarters
        miss_rate = miss_count / total_quarters if total_quarters > 0 else 0
        
        # 3회 이상 미스: 매수 완전 차단
        if miss_count >= config.miss_block_threshold:
            return {
                "miss_count": miss_count,
                "miss_rate": miss_rate,
                "action": "BLOCK",
                "penalty": config.miss_penalty_score * 2,
                "reason": f"🚫 {miss_count}/{total_quarters}분기 미스 — 상습 미스 종목 차단",
            }
        
        # 2회 미스: 점수 감점
        if miss_count >= config.miss_threshold:
            return {
                "miss_count": miss_count,
                "miss_rate": miss_rate,
                "action": "PENALIZE",
                "penalty": config.miss_penalty_score,
                "reason": f"⚠️ {miss_count}/{total_quarters}분기 미스 — 점수 -{config.miss_penalty_score}",
            }
        
        return {
            "miss_count": miss_count,
            "miss_rate": miss_rate,
            "action": "ALLOW",
            "penalty": 0,
        }


# ═══════════════════════════════════════════════════════════════
#  4. 섹터 집중 제한
# ═══════════════════════════════════════════════════════════════

class SectorLimiter:
    """
    동일 섹터 과다 노출 방지
    
    방위 섹터에 LMT, NOC, RTX, AVAV 4개 보유 시
    → 섹터 전체 악재에 한꺼번에 타격
    → 섹터당 최대 2~3개로 제한
    """
    
    SECTOR_MAP = {
        "Tech/AI": ["NVDA","AAPL","MSFT","GOOGL","AMZN","META","TSLA","TSM","AMD","INTC","MU","PLTR","APP","AVGO","ORCL","CRM"],
        "Energy": ["XOM","CVX","COP","OXY","DVN","EOG","SLB","BP","MPC","VLO","PSX","HES","HAL","SHEL","PXD"],
        "Defense": ["LMT","NOC","RTX","GD","BA","LHX","AVAV","KTOS"],
        "Tankers": ["FRO","DHT","INSW","STNG","TNK"],
        "Nuclear": ["CEG","VST","GEV","NRG","NEE"],
        "Financials": ["JPM","GS","MS","BAC","WFC","BRK-B","AXP"],
        "Healthcare": ["UNH","JNJ","LLY","PFE","ABBV","MRK","AMGN"],
        "Consumer": ["WMT","COST","HD","TGT","LULU","NKE"],
        "Fintech": ["HOOD","MSTR","COIN","SQ"],
    }
    
    @classmethod
    def get_sector(cls, symbol: str) -> str:
        for sector, symbols in cls.SECTOR_MAP.items():
            if symbol in symbols:
                return sector
        return "Other"
    
    @classmethod
    def check(
        cls,
        symbol: str,
        current_holdings: list,
        config: RiskShieldConfig,
    ) -> dict:
        """
        섹터 초과 여부 확인
        
        Parameters:
            symbol: 매수 대상 종목
            current_holdings: 현재 보유 종목 리스트 ["LMT", "NOC", ...]
        """
        if not config.sector_limit_enabled:
            return {"action": "ALLOW", "sector": "", "count": 0}
        
        sector = cls.get_sector(symbol)
        sector_holdings = [s for s in current_holdings if cls.get_sector(s) == sector]
        count = len(sector_holdings)
        
        if count >= config.max_stocks_per_sector:
            return {
                "action": "BLOCK",
                "sector": sector,
                "count": count,
                "holdings": sector_holdings,
                "reason": f"🚫 {sector} 섹터 {count}/{config.max_stocks_per_sector} 초과",
            }
        
        return {
            "action": "ALLOW",
            "sector": sector,
            "count": count,
        }


# ═══════════════════════════════════════════════════════════════
#  통합 리스크 체크 엔진
# ═══════════════════════════════════════════════════════════════

class RiskShield:
    """
    통합 리스크 방어 시스템
    
    모든 리스크 필터를 한 번에 실행하고 최종 판정을 내림.
    Smart Trader의 매수 전에 반드시 이 체크를 통과해야 함.
    """
    
    def __init__(self, config: RiskShieldConfig = None, ib=None):
        self.config = config or RiskShieldConfig()
        self.earnings_filter = EarningsCalendarFilter(ib)
        self.daily_pnl = 0.0
        self.initial_portfolio = 0.0
    
    def set_daily_baseline(self, portfolio_value: float):
        """장 시작 시 포트폴리오 기준값 설정"""
        self.initial_portfolio = portfolio_value
        self.daily_pnl = 0.0
    
    def update_daily_pnl(self, pnl: float):
        """일일 P&L 업데이트"""
        self.daily_pnl += pnl
    
    def check_daily_limit(self) -> bool:
        """일일 손실 한도 초과 여부"""
        if not self.config.daily_loss_limit_enabled or self.initial_portfolio <= 0:
            return False
        
        pnl_pct = (self.daily_pnl / self.initial_portfolio) * 100
        return pnl_pct <= self.config.daily_loss_limit_pct
    
    def full_check(
        self,
        symbol: str,
        current_holdings: list = None,
    ) -> RiskCheckResult:
        """
        전체 리스크 체크 실행
        
        순서:
        1. 일일 손실 한도 체크
        2. 실적 발표 캘린더 체크
        3. 실적 미스 패턴 체크
        4. Beta 포지션 사이징
        5. 섹터 집중 체크
        
        → 하나라도 BLOCK이면 최종 BLOCK
        → REDUCE가 있으면 최종 REDUCE
        → 모두 ALLOW면 최종 ALLOW
        """
        current_holdings = current_holdings or []
        result = RiskCheckResult(symbol=symbol)
        reasons = []
        final_action = RiskAction.ALLOW
        
        # ── 0. 일일 손실 한도 ──
        if self.check_daily_limit():
            result.action = RiskAction.BLOCK
            result.reasons = [f"🛑 일일 손실 한도 초과 ({self.daily_pnl/self.initial_portfolio*100:.1f}%)"]
            result.adjusted_investment = 0
            return result
        
        # ── 1. 실적 발표 캘린더 ──
        earnings = self.earnings_filter.check(symbol, self.config)
        result.has_earnings_soon = earnings.get("has_earnings_soon", False)
        result.earnings_date = earnings.get("earnings_date", "")
        
        if earnings["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            days = earnings.get("days_until", 0)
            reasons.append(
                f"📅 실적 발표 {'오늘' if days == 0 else f'{days}일 후'} "
                f"({result.earnings_date}) → 매수 차단"
            )
            result.position_reduce_pct = earnings.get("reduce_pct", 0)
        elif earnings["action"] == "REDUCE":
            if final_action != RiskAction.BLOCK:
                final_action = RiskAction.REDUCE
            reasons.append(
                f"📅 실적 발표 {earnings.get('days_until', '?')}일 후 → 포지션 축소"
            )
            result.position_reduce_pct = earnings.get("reduce_pct", 0)
        
        # ── 2. 실적 미스 패턴 ──
        miss = EarningsMissFilter.check(symbol, self.config)
        result.earnings_miss_count = miss["miss_count"]
        
        if miss["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            reasons.append(miss.get("reason", "상습 미스 차단"))
            result.score_penalty = miss["penalty"]
        elif miss["action"] == "PENALIZE":
            reasons.append(miss.get("reason", "실적 미스 감점"))
            result.score_penalty = miss["penalty"]
        
        # ── 3. Beta 포지션 사이징 ──
        sizing = BetaPositionSizer.calculate_position_size(symbol, self.config)
        result.beta = sizing["beta"]
        result.adjusted_investment = sizing["adjusted_investment"]
        
        if sizing["risk_level"] == "EXTREME":
            reasons.append(
                f"🔴 Beta {sizing['beta']:.2f} (극고위험) → "
                f"투자금 ${sizing['adjusted_investment']:,.0f} "
                f"(기본 대비 {sizing['multiplier']:.0%})"
            )
        elif sizing["risk_level"] == "HIGH":
            reasons.append(
                f"🟡 Beta {sizing['beta']:.2f} (고위험) → "
                f"투자금 ${sizing['adjusted_investment']:,.0f}"
            )
        
        # ── 4. 섹터 집중 ──
        sector = SectorLimiter.check(symbol, current_holdings, self.config)
        result.sector = sector.get("sector", "")
        result.sector_count = sector.get("count", 0)
        
        if sector["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            reasons.append(sector.get("reason", "섹터 초과"))
        
        # ── 최종 결정 ──
        # BLOCK 시 투자금 = 0
        if final_action == RiskAction.BLOCK:
            result.adjusted_investment = 0.0
        
        result.action = final_action
        result.reasons = reasons
        
        return result
    
    def print_report(self, results: list):
        """리스크 체크 리포트 출력"""
        print("\n" + "═" * 70)
        print("  🛡️ Risk Shield Report")
        print("═" * 70)
        
        blocked = [r for r in results if r.action == RiskAction.BLOCK]
        reduced = [r for r in results if r.action == RiskAction.REDUCE]
        allowed = [r for r in results if r.action == RiskAction.ALLOW]
        
        if blocked:
            print(f"\n  🚫 차단 ({len(blocked)}종목):")
            for r in blocked:
                print(f"    {r}")
        
        if reduced:
            print(f"\n  ⚠️ 축소 ({len(reduced)}종목):")
            for r in reduced:
                print(f"    {r}")
        
        print(f"\n  ✅ 허용 ({len(allowed)}종목):")
        for r in allowed:
            beta_note = ""
            if r.beta > 1.5:
                beta_note = f" [Beta {r.beta:.1f} → ${r.adjusted_investment:,.0f}]"
            print(f"    {r.symbol:6s} ${r.adjusted_investment:>8,.0f}{beta_note}")
        
        total_investment = sum(r.adjusted_investment for r in results if r.action != RiskAction.BLOCK)
        print(f"\n  💰 총 투자금: ${total_investment:,.0f}")
        print("═" * 70)


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    """AVAV 사례 시뮬레이션"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🛡️ Risk Shield 데모 — AVAV 실적 발표일 시뮬레이션      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    config = RiskShieldConfig()
    shield = RiskShield(config)
    
    # 실적 캘린더 로드 (AVAV: 3/10, MU: 3/18)
    shield.earnings_filter.load_earnings_calendar({
        "AVAV": "2026-03-10",
        "MU": "2026-03-18",
        "LULU": "2026-03-17",
    })
    
    # 현재 보유 종목
    current_holdings = ["LMT", "NOC", "RTX", "XOM", "CVX"]
    
    # 매수 후보 10종목 체크
    candidates = ["XOM", "CVX", "COP", "DVN", "LMT", "NOC", "AVAV", "FRO", "OXY", "NVDA"]
    
    print("  📋 매수 후보 10종목 리스크 체크:\n")
    results = []
    
    for sym in candidates:
        result = shield.full_check(sym, current_holdings)
        results.append(result)
        
        icon = {"✅ 허용": "✅", "⚠️ 축소": "⚠️", "🚫 차단": "🚫"}[result.action.value]
        print(f"  {icon} {sym:6s} | Beta: {result.beta:4.2f} | "
              f"투자금: ${result.adjusted_investment:>8,.0f} | "
              f"미스: {result.earnings_miss_count}/4")
        for reason in result.reasons:
            print(f"           {reason}")
        print()
    
    shield.print_report(results)


if __name__ == "__main__":
    demo()
