"""
═══════════════════════════════════════════════════════════════════
  Portfolio Manager - 멀티 버킷 포트폴리오 관리 시스템
  
  $200K+ 계좌를 위한 공격/방어 분리 포트폴리오 관리
  
  버킷 구조:
    🔴 공격 (Offensive)  — Tech/AI + 모멘텀 + 고성장
    🔵 방어 (Defensive)  — 배당 + 안전자산 + 방위 + 필수소비
  
  핵심 기능:
    1. 시장 상황에 따른 동적 비율 조정 (Signal Monitor 연동)
       - BULL: 60공격/40방어
       - NEUTRAL: 50/50
       - BEAR: 40공격/60방어
    2. 버킷별 독립 리스크 관리
    3. 종목당 최대 5% 비중 제한
    4. 섹터 분산 강제
    5. 자동 리밸런싱
  
  Smart Trader, Risk Shield, Tax Optimizer, Signal Bridge와 통합
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger("PortfolioManager")


# ═══════════════════════════════════════════════════════════════
#  버킷 정의
# ═══════════════════════════════════════════════════════════════

class BucketType(Enum):
    OFFENSIVE = "🔴 공격"
    DEFENSIVE = "🔵 방어"
    CASH = "💵 현금"


class MarketRegime(Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class PortfolioConfig:
    """멀티 버킷 포트폴리오 설정"""
    
    # ── 총 자본금 ──
    total_capital: float = 200_000.0
    
    # ── 동적 비율 (시장 상황별) ──
    # {regime: (공격%, 방어%, 현금%)}
    regime_allocation: dict = field(default_factory=lambda: {
        "BULL":    (60, 30, 10),
        "NEUTRAL": (50, 40, 10),
        "BEAR":    (30, 55, 15),
    })
    
    # ── 종목 제한 ──
    max_weight_per_stock: float = 5.0     # 종목당 최대 5%
    max_stocks_offensive: int = 15        # 공격 버킷 최대 종목
    max_stocks_defensive: int = 15        # 방어 버킷 최대 종목
    max_stocks_per_sector: int = 4        # 섹터당 최대
    
    # ── 리밸런싱 ──
    rebalance_threshold_pct: float = 5.0  # 목표 비율에서 5% 이탈 시 리밸런싱
    rebalance_frequency_days: int = 7     # 최소 N일마다 리밸런싱 체크
    
    # ── 공격 버킷 — 손절 ──
    offensive_stop_loss_pct: float = -8.0   # 종목당 -8% 손절
    offensive_take_profit_pct: float = 15.0 # 종목당 +15% 익절
    
    # ── 방어 버킷 — 더 넓은 범위 ──
    defensive_stop_loss_pct: float = -12.0  # 방어주는 변동 작으니 넓게
    defensive_take_profit_pct: float = 20.0


# ═══════════════════════════════════════════════════════════════
#  종목 유니버스
# ═══════════════════════════════════════════════════════════════

@dataclass
class StockInfo:
    """종목 정보"""
    symbol: str
    name: str
    sector: str
    bucket: BucketType
    beta: float
    dividend_yield: float = 0.0
    base_weight: float = 0.0    # 기본 배분 비중 (%)
    
    def to_dict(self):
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "bucket": self.bucket.value,
            "beta": self.beta,
            "dividend_yield": self.dividend_yield,
            "base_weight": self.base_weight,
        }


# 공격 포트 유니버스
OFFENSIVE_UNIVERSE = [
    StockInfo("NVDA",  "Nvidia",          "AI/반도체",    BucketType.OFFENSIVE, 1.95, 0.02, 15),
    StockInfo("AMD",   "AMD",             "반도체",       BucketType.OFFENSIVE, 1.60, 0.00, 10),
    StockInfo("META",  "Meta Platforms",   "AI/소셜",     BucketType.OFFENSIVE, 1.25, 0.00, 10),
    StockInfo("MSFT",  "Microsoft",        "클라우드",     BucketType.OFFENSIVE, 0.90, 0.80, 10),
    StockInfo("AMZN",  "Amazon",           "클라우드",     BucketType.OFFENSIVE, 1.15, 0.00, 8),
    StockInfo("TSLA",  "Tesla",            "EV/로봇",     BucketType.OFFENSIVE, 2.05, 0.00, 5),
    StockInfo("MU",    "Micron",           "메모리",       BucketType.OFFENSIVE, 1.45, 0.40, 7),
    StockInfo("COP",   "ConocoPhillips",   "에너지",       BucketType.OFFENSIVE, 1.30, 1.80, 8),
    StockInfo("DVN",   "Devon Energy",     "셰일",        BucketType.OFFENSIVE, 1.75, 2.50, 5),
    StockInfo("HOOD",  "Robinhood",        "핀테크",       BucketType.OFFENSIVE, 2.10, 0.00, 5),
    StockInfo("PLTR",  "Palantir",         "AI/방위",     BucketType.OFFENSIVE, 1.80, 0.00, 7),
    StockInfo("AVGO",  "Broadcom",         "반도체",       BucketType.OFFENSIVE, 1.20, 1.20, 5),
    StockInfo("COIN",  "Coinbase",         "크립토",       BucketType.OFFENSIVE, 2.30, 0.00, 5),
]

# 방어 포트 유니버스
DEFENSIVE_UNIVERSE = [
    StockInfo("LMT",   "Lockheed Martin",  "방위",        BucketType.DEFENSIVE, 0.55, 2.50, 12),
    StockInfo("NOC",   "Northrop Grumman", "방위",        BucketType.DEFENSIVE, 0.50, 1.60, 10),
    StockInfo("RTX",   "RTX Corp",         "방위",        BucketType.DEFENSIVE, 0.65, 2.10, 8),
    StockInfo("XOM",   "ExxonMobil",       "에너지",       BucketType.DEFENSIVE, 0.85, 2.60, 12),
    StockInfo("CVX",   "Chevron",          "에너지",       BucketType.DEFENSIVE, 0.90, 3.00, 10),
    StockInfo("GLD",   "Gold ETF",         "금",          BucketType.DEFENSIVE, 0.15, 0.00, 12),
    StockInfo("WMT",   "Walmart",          "필수소비",     BucketType.DEFENSIVE, 0.50, 1.10, 8),
    StockInfo("JNJ",   "Johnson & Johnson","헬스케어",     BucketType.DEFENSIVE, 0.55, 3.20, 8),
    StockInfo("PG",    "Procter & Gamble", "필수소비",     BucketType.DEFENSIVE, 0.40, 2.40, 7),
    StockInfo("UNH",   "UnitedHealth",     "헬스케어",     BucketType.DEFENSIVE, 0.60, 1.50, 8),
    StockInfo("KO",    "Coca-Cola",        "필수소비",     BucketType.DEFENSIVE, 0.55, 2.80, 5),
]


# ═══════════════════════════════════════════════════════════════
#  포트폴리오 매니저
# ═══════════════════════════════════════════════════════════════

class PortfolioManager:
    """
    멀티 버킷 포트폴리오 매니저
    
    사용법:
        pm = PortfolioManager(PortfolioConfig(total_capital=200000))
        
        # 시장 상황 설정 (Signal Bridge 연동)
        pm.set_regime(MarketRegime.NEUTRAL)
        
        # 포트폴리오 구성
        portfolio = pm.build_portfolio()
        
        # 리밸런싱 체크
        rebalance = pm.check_rebalance(current_positions)
    """
    
    def __init__(self, config: PortfolioConfig = None):
        self.config = config or PortfolioConfig()
        self.regime = MarketRegime.NEUTRAL
        self.last_rebalance = None
        self._portfolio: dict = {}
        self._history: list = []
    
    def set_regime(self, regime: MarketRegime):
        """시장 레짐 설정 (Signal Bridge에서 호출)"""
        if self.regime != regime:
            logger.info(
                f"  🔄 시장 레짐 변경: {self.regime.value} → {regime.value}"
            )
            self.regime = regime
    
    def get_allocation(self) -> dict:
        """현재 레짐에 따른 자본 배분"""
        alloc = self.config.regime_allocation.get(
            self.regime.value, (50, 40, 10)
        )
        off_pct, def_pct, cash_pct = alloc
        total = self.config.total_capital
        
        return {
            "offensive": total * off_pct / 100,
            "defensive": total * def_pct / 100,
            "cash": total * cash_pct / 100,
            "offensive_pct": off_pct,
            "defensive_pct": def_pct,
            "cash_pct": cash_pct,
        }
    
    def build_portfolio(self) -> dict:
        """
        전체 포트폴리오 구성
        
        Returns:
            {
                "regime": str,
                "allocation": dict,
                "offensive_stocks": [dict],
                "defensive_stocks": [dict],
                "cash": float,
                "total_stocks": int,
                "est_annual_dividend": float,
            }
        """
        alloc = self.get_allocation()
        off_capital = alloc["offensive"]
        def_capital = alloc["defensive"]
        
        # 종목당 최대 금액
        max_per_stock = self.config.total_capital * self.config.max_weight_per_stock / 100
        
        # ── 공격 포트 구성 ──
        offensive_stocks = []
        off_total_weight = sum(s.base_weight for s in OFFENSIVE_UNIVERSE)
        
        for stock in OFFENSIVE_UNIVERSE:
            weight_ratio = stock.base_weight / off_total_weight
            raw_amount = off_capital * weight_ratio
            
            # Beta 조정: 고Beta → 축소, 저Beta → 증액
            beta_adj = 1.0 / max(stock.beta, 0.5)
            beta_adj = max(0.5, min(1.5, beta_adj))
            adjusted = raw_amount * beta_adj
            
            # 최대 한도 적용
            final_amount = min(adjusted, max_per_stock)
            
            offensive_stocks.append({
                "symbol": stock.symbol,
                "name": stock.name,
                "sector": stock.sector,
                "bucket": "공격",
                "amount": round(final_amount, 0),
                "weight_pct": round(final_amount / self.config.total_capital * 100, 2),
                "beta": stock.beta,
                "dividend_yield": stock.dividend_yield,
                "stop_loss": self.config.offensive_stop_loss_pct,
                "take_profit": self.config.offensive_take_profit_pct,
            })
        
        # 공격 포트 정규화 (총합 = off_capital)
        off_sum = sum(s["amount"] for s in offensive_stocks)
        if off_sum > 0:
            scale = off_capital / off_sum
            for s in offensive_stocks:
                s["amount"] = round(s["amount"] * scale, 0)
                s["weight_pct"] = round(s["amount"] / self.config.total_capital * 100, 2)
        
        # ── 방어 포트 구성 ──
        defensive_stocks = []
        def_total_weight = sum(s.base_weight for s in DEFENSIVE_UNIVERSE)
        
        for stock in DEFENSIVE_UNIVERSE:
            weight_ratio = stock.base_weight / def_total_weight
            raw_amount = def_capital * weight_ratio
            
            # 방어주는 Beta 조정 약하게
            final_amount = min(raw_amount, max_per_stock)
            
            defensive_stocks.append({
                "symbol": stock.symbol,
                "name": stock.name,
                "sector": stock.sector,
                "bucket": "방어",
                "amount": round(final_amount, 0),
                "weight_pct": round(final_amount / self.config.total_capital * 100, 2),
                "beta": stock.beta,
                "dividend_yield": stock.dividend_yield,
                "stop_loss": self.config.defensive_stop_loss_pct,
                "take_profit": self.config.defensive_take_profit_pct,
            })
        
        # 방어 포트 정규화
        def_sum = sum(s["amount"] for s in defensive_stocks)
        if def_sum > 0:
            scale = def_capital / def_sum
            for s in defensive_stocks:
                s["amount"] = round(s["amount"] * scale, 0)
                s["weight_pct"] = round(s["amount"] / self.config.total_capital * 100, 2)
        
        # 연간 배당 추정
        est_dividend = sum(
            s["amount"] * s["dividend_yield"] / 100
            for s in offensive_stocks + defensive_stocks
        )
        
        # 평균 Beta
        all_stocks = offensive_stocks + defensive_stocks
        total_invested = sum(s["amount"] for s in all_stocks)
        avg_beta = sum(
            s["amount"] * s["beta"] for s in all_stocks
        ) / total_invested if total_invested > 0 else 1.0
        
        self._portfolio = {
            "regime": self.regime.value,
            "allocation": alloc,
            "offensive_stocks": offensive_stocks,
            "defensive_stocks": defensive_stocks,
            "cash": alloc["cash"],
            "total_stocks": len(offensive_stocks) + len(defensive_stocks),
            "est_annual_dividend": round(est_dividend, 0),
            "avg_beta": round(avg_beta, 2),
            "timestamp": datetime.now().isoformat(),
        }
        
        return self._portfolio
    
    def check_rebalance(self, current_positions: dict) -> dict:
        """
        리밸런싱 필요 여부 체크
        
        Parameters:
            current_positions: {symbol: {"market_value": float, "pnl_pct": float}}
        
        Returns:
            {"needed": bool, "actions": [dict], "reason": str}
        """
        if not self._portfolio:
            return {"needed": True, "actions": [], "reason": "포트폴리오 미구성"}
        
        alloc = self.get_allocation()
        actions = []
        
        # 현재 버킷별 총액 계산
        off_symbols = {s["symbol"] for s in self._portfolio["offensive_stocks"]}
        def_symbols = {s["symbol"] for s in self._portfolio["defensive_stocks"]}
        
        current_off = sum(
            pos.get("market_value", 0) 
            for sym, pos in current_positions.items() 
            if sym in off_symbols
        )
        current_def = sum(
            pos.get("market_value", 0)
            for sym, pos in current_positions.items()
            if sym in def_symbols
        )
        current_total = current_off + current_def
        
        if current_total <= 0:
            return {"needed": True, "actions": [], "reason": "포지션 없음"}
        
        # 목표 비율 vs 현재 비율
        target_off_pct = alloc["offensive_pct"]
        target_def_pct = alloc["defensive_pct"]
        actual_off_pct = (current_off / current_total) * 100
        actual_def_pct = (current_def / current_total) * 100
        
        off_drift = abs(actual_off_pct - target_off_pct)
        def_drift = abs(actual_def_pct - target_def_pct)
        
        needed = (off_drift > self.config.rebalance_threshold_pct or
                  def_drift > self.config.rebalance_threshold_pct)
        
        reason = (
            f"공격: {actual_off_pct:.1f}% (목표 {target_off_pct}%, "
            f"이탈 {off_drift:.1f}%) | "
            f"방어: {actual_def_pct:.1f}% (목표 {target_def_pct}%, "
            f"이탈 {def_drift:.1f}%)"
        )
        
        if needed:
            # 공격 → 방어 또는 방어 → 공격 이동 금액 계산
            target_off = current_total * target_off_pct / 100
            move = current_off - target_off
            
            if move > 0:
                actions.append({
                    "action": "REDUCE_OFFENSIVE",
                    "amount": round(abs(move), 0),
                    "reason": f"공격 포트 ${abs(move):,.0f} 축소 → 방어로 이동",
                })
            else:
                actions.append({
                    "action": "REDUCE_DEFENSIVE",
                    "amount": round(abs(move), 0),
                    "reason": f"방어 포트 ${abs(move):,.0f} 축소 → 공격으로 이동",
                })
        
        # 종목별 손절/익절 체크
        for sym, pos in current_positions.items():
            pnl_pct = pos.get("pnl_pct", 0)
            
            if sym in off_symbols:
                if pnl_pct <= self.config.offensive_stop_loss_pct:
                    actions.append({
                        "action": "STOP_LOSS",
                        "symbol": sym,
                        "bucket": "공격",
                        "pnl_pct": pnl_pct,
                        "reason": f"🛑 {sym} 손절 {pnl_pct:.1f}%",
                    })
                elif pnl_pct >= self.config.offensive_take_profit_pct:
                    actions.append({
                        "action": "TAKE_PROFIT",
                        "symbol": sym,
                        "bucket": "공격",
                        "pnl_pct": pnl_pct,
                        "reason": f"🎯 {sym} 익절 +{pnl_pct:.1f}%",
                    })
            
            elif sym in def_symbols:
                if pnl_pct <= self.config.defensive_stop_loss_pct:
                    actions.append({
                        "action": "STOP_LOSS",
                        "symbol": sym,
                        "bucket": "방어",
                        "pnl_pct": pnl_pct,
                        "reason": f"🛑 {sym} 손절 {pnl_pct:.1f}%",
                    })
                elif pnl_pct >= self.config.defensive_take_profit_pct:
                    actions.append({
                        "action": "TAKE_PROFIT",
                        "symbol": sym,
                        "bucket": "방어",
                        "pnl_pct": pnl_pct,
                        "reason": f"🎯 {sym} 익절 +{pnl_pct:.1f}%",
                    })
        
        return {"needed": needed, "actions": actions, "reason": reason}
    
    def print_portfolio(self):
        """포트폴리오 리포트 출력"""
        if not self._portfolio:
            self.build_portfolio()
        
        p = self._portfolio
        alloc = p["allocation"]
        
        print("\n" + "═" * 75)
        print(f"  📊 Multi-Bucket Portfolio — ${self.config.total_capital:,.0f}")
        print(f"  시장 레짐: {p['regime']} | 평균 Beta: {p['avg_beta']}")
        print("═" * 75)
        
        # 배분 요약
        print(f"\n  💰 자본 배분:")
        print(f"    🔴 공격: ${alloc['offensive']:>10,.0f} ({alloc['offensive_pct']}%)")
        print(f"    🔵 방어: ${alloc['defensive']:>10,.0f} ({alloc['defensive_pct']}%)")
        print(f"    💵 현금: ${alloc['cash']:>10,.0f} ({alloc['cash_pct']}%)")
        
        # 공격 포트
        print(f"\n  🔴 공격 포트 ({len(p['offensive_stocks'])}종목):")
        print(f"    {'종목':6s} {'이름':18s} {'섹터':10s} {'금액':>10s} {'비중':>6s} {'Beta':>5s} {'SL':>6s}")
        print(f"    {'─'*6} {'─'*18} {'─'*10} {'─'*10} {'─'*6} {'─'*5} {'─'*6}")
        
        for s in sorted(p["offensive_stocks"], key=lambda x: -x["amount"]):
            print(
                f"    {s['symbol']:6s} {s['name']:18s} {s['sector']:10s} "
                f"${s['amount']:>9,.0f} {s['weight_pct']:>5.1f}% "
                f"{s['beta']:>5.2f} {s['stop_loss']:>5.0f}%"
            )
        
        off_total = sum(s["amount"] for s in p["offensive_stocks"])
        print(f"    {'':6s} {'소계':18s} {'':10s} ${off_total:>9,.0f}")
        
        # 방어 포트
        print(f"\n  🔵 방어 포트 ({len(p['defensive_stocks'])}종목):")
        print(f"    {'종목':6s} {'이름':18s} {'섹터':10s} {'금액':>10s} {'비중':>6s} {'Beta':>5s} {'배당':>5s}")
        print(f"    {'─'*6} {'─'*18} {'─'*10} {'─'*10} {'─'*6} {'─'*5} {'─'*5}")
        
        for s in sorted(p["defensive_stocks"], key=lambda x: -x["amount"]):
            print(
                f"    {s['symbol']:6s} {s['name']:18s} {s['sector']:10s} "
                f"${s['amount']:>9,.0f} {s['weight_pct']:>5.1f}% "
                f"{s['beta']:>5.2f} {s['dividend_yield']:>4.1f}%"
            )
        
        def_total = sum(s["amount"] for s in p["defensive_stocks"])
        print(f"    {'':6s} {'소계':18s} {'':10s} ${def_total:>9,.0f}")
        
        # 요약
        print(f"\n  📈 포트폴리오 요약:")
        print(f"    총 종목: {p['total_stocks']}개")
        print(f"    연간 배당 예상: ${p['est_annual_dividend']:,.0f}")
        print(f"    평균 Beta: {p['avg_beta']}")
        
        # 시나리오 분석
        print(f"\n  📉 시나리오 분석:")
        scenarios = [
            ("시장 +2%", 0.02),
            ("시장 -2%", -0.02),
            ("시장 -5%", -0.05),
            ("시장 -10%", -0.10),
        ]
        
        off_beta = sum(
            s["amount"] * s["beta"] for s in p["offensive_stocks"]
        ) / off_total if off_total > 0 else 1.5
        def_beta = sum(
            s["amount"] * s["beta"] for s in p["defensive_stocks"]
        ) / def_total if def_total > 0 else 0.55
        
        for name, market_move in scenarios:
            off_move = off_total * market_move * off_beta
            def_move = def_total * market_move * def_beta
            total_move = off_move + def_move
            total_pct = total_move / self.config.total_capital * 100
            print(
                f"    {name:10s} → "
                f"공격: ${off_move:>+9,.0f} | "
                f"방어: ${def_move:>+9,.0f} | "
                f"합계: ${total_move:>+9,.0f} ({total_pct:+.1f}%)"
            )
        
        print("═" * 75)


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  📊 Multi-Bucket Portfolio Manager 데모                  ║
    ║  $200,000 공격/방어 분리 포트폴리오                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    config = PortfolioConfig(total_capital=200_000)
    pm = PortfolioManager(config)
    
    # 1) NEUTRAL 레짐 (50/40/10)
    print("  ━━━ 시나리오 1: NEUTRAL 시장 ━━━")
    pm.set_regime(MarketRegime.NEUTRAL)
    pm.build_portfolio()
    pm.print_portfolio()
    
    # 2) BEAR 레짐 (30/55/15)
    print("\n\n  ━━━ 시나리오 2: BEAR 시장 (이란 전쟁 악화) ━━━")
    pm.set_regime(MarketRegime.BEAR)
    pm.build_portfolio()
    pm.print_portfolio()
    
    # 3) BULL 레짐 (60/30/10)
    print("\n\n  ━━━ 시나리오 3: BULL 시장 (전쟁 종료) ━━━")
    pm.set_regime(MarketRegime.BULL)
    pm.build_portfolio()
    pm.print_portfolio()
    
    # 4) 리밸런싱 체크
    print("\n\n  ━━━ 리밸런싱 체크 ━━━")
    # 가상 현재 포지션 (공격이 너무 올라서 비율 이탈)
    mock_positions = {
        "NVDA": {"market_value": 18000, "pnl_pct": 20.0},  # 익절 트리거
        "AMD": {"market_value": 11000, "pnl_pct": 10.0},
        "META": {"market_value": 10500, "pnl_pct": 5.0},
        "TSLA": {"market_value": 3000, "pnl_pct": -40.0},  # 이미 손절됨
        "LMT": {"market_value": 13000, "pnl_pct": 8.0},
        "XOM": {"market_value": 12500, "pnl_pct": 4.0},
        "GLD": {"market_value": 11000, "pnl_pct": -2.0},
    }
    
    result = pm.check_rebalance(mock_positions)
    print(f"\n  리밸런싱 필요: {'예' if result['needed'] else '아니오'}")
    print(f"  상태: {result['reason']}")
    if result["actions"]:
        print(f"\n  📋 액션 목록:")
        for act in result["actions"]:
            print(f"    → {act['reason']}")


if __name__ == "__main__":
    demo()
