"""
═══════════════════════════════════════════════════════════════════
  Signal Monitor Bridge - 시그널 모니터 통합 브리지
  
  다른 대화에서 만든 Signal Monitor (COT + 옵션 흐름)를
  Smart Trader 앙상블에 6번째 전략으로 통합합니다.
  
  기능:
    8. COT + 옵션 복합 신호 → 앙상블 6번째 전략
    9. BEAR 시장 브레이크 - 약세 신호 시 신규 매수 중단
   10. BULL 부스터 - 강세 신호 시 매수 가중치 증가
   11. Washout(Whipsaw) 방지 - 거짓 크로스 연속 매매 차단
  
  Signal Monitor 서버 (port 5050)와 독립 실행 모두 지원
═══════════════════════════════════════════════════════════════════
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict, deque

logger = logging.getLogger("SignalBridge")


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

def is_market_open() -> bool:
    """미국 주식 시장 개장 여부 (ET 기준 9:30~16:00, 월~금)"""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # 주말
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def is_premarket() -> bool:
    """프리마켓 (ET 4:00~9:30)"""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    pre_open = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return pre_open <= now_et < market_open


@dataclass
class SignalBridgeConfig:
    """시그널 브리지 설정"""

    # ── Signal Monitor 서버 ──
    monitor_url: str = "http://localhost:5050"
    use_server: bool = False         # True: 서버 연동, False: 내장 엔진 사용
    poll_interval_sec: int = 60      # 장중 서버 폴링 간격
    poll_interval_off_hours_sec: int = 3600  # 장외 폴링 간격 (1시간)
    
    # ── COT 설정 ──
    cot_enabled: bool = True
    cot_bull_threshold: float = 0.3   # 헤지펀드 순매수 비율 이상 = BULL
    cot_bear_threshold: float = -0.2  # 헤지펀드 순매도 비율 이하 = BEAR
    
    # ── 옵션 설정 ──
    options_enabled: bool = True
    pc_ratio_bull: float = 0.7        # Put/Call < 0.7 = BULL (콜 우세)
    pc_ratio_bear: float = 1.3        # Put/Call > 1.3 = BEAR (풋 우세)
    
    # ── 시장 브레이크 ──
    market_brake_enabled: bool = True
    brake_on_bear: bool = True        # BEAR 시 신규 매수 중단
    brake_reduce_pct: float = 50.0    # BEAR 시 기존 포지션 축소 권고 %
    
    # ── BULL 부스터 ──
    bull_boost_enabled: bool = True
    bull_confidence_boost: float = 0.15  # BULL 시 앙상블 매수 신뢰도 추가
    
    # ── Washout(Whipsaw) 방지 ──
    washout_enabled: bool = True
    cooldown_hours: int = 4           # 매매 후 N시간 동일 종목 재매매 금지
    min_ma_gap_pct: float = 0.5       # MA 간격 최소 0.5% 이상이어야 유효
    confirmation_bars: int = 2         # 크로스 후 N봉 연속 유지 필요
    max_signals_per_day: int = 3       # 종목당 일일 최대 신호 수
    
    # ── 앙상블 가중치 ──
    ensemble_weight: float = 0.15     # 6번째 전략의 가중치


# ═══════════════════════════════════════════════════════════════
#  복합 시장 신호
# ═══════════════════════════════════════════════════════════════

class MarketSignal:
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


@dataclass
class CompositeSignal:
    """COT + 옵션 복합 신호"""
    composite: str = MarketSignal.NEUTRAL    # BULL / BEAR / NEUTRAL
    cot_signal: str = MarketSignal.NEUTRAL
    options_signal: str = MarketSignal.NEUTRAL
    confidence: float = 0.5
    reason: str = ""
    timestamp: str = ""
    
    # 상세 데이터
    cot_net_long: float = 0.0
    pc_ratio: float = 1.0
    
    @property
    def is_bull(self) -> bool:
        return self.composite == MarketSignal.BULL
    
    @property
    def is_bear(self) -> bool:
        return self.composite == MarketSignal.BEAR


# ═══════════════════════════════════════════════════════════════
#  내장 COT 엔진 (서버 없이 독립 실행)
# ═══════════════════════════════════════════════════════════════

class BuiltInCOTEngine:
    """
    CFTC COT 데이터 기반 시장 포지셔닝 분석
    
    핵심: 헤지펀드(Managed Money)의 순포지션 변화를 추적
    - 순매수 증가 → BULL (스마트머니가 매수 중)
    - 순매도 증가 → BEAR (스마트머니가 매도 중)
    
    실제 운영 시 CFTC API에서 주간 데이터를 자동 수집
    """
    
    # 최근 COT 데이터 (수동 업데이트 또는 API 연동)
    # 형식: {"date": "2026-03-14", "net_long_pct": 0.35, "change": 0.05}
    _latest_data = {
        "S&P500": {"net_long_pct": 0.25, "change": -0.05, "date": "2026-03-14"},
        "CRUDE_OIL": {"net_long_pct": 0.45, "change": 0.12, "date": "2026-03-14"},
        "GOLD": {"net_long_pct": 0.38, "change": 0.08, "date": "2026-03-14"},
        "US_DOLLAR": {"net_long_pct": 0.15, "change": 0.03, "date": "2026-03-14"},
    }
    
    @classmethod
    def update_data(cls, instrument: str, net_long_pct: float, change: float):
        """수동으로 COT 데이터 업데이트"""
        cls._latest_data[instrument] = {
            "net_long_pct": net_long_pct,
            "change": change,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
    
    @classmethod
    def get_signal(cls, config: SignalBridgeConfig) -> tuple[str, float, str]:
        """
        COT 종합 신호
        Returns: (signal, confidence, reason)
        """
        if not config.cot_enabled:
            return MarketSignal.NEUTRAL, 0.3, "COT 비활성화"
        
        sp = cls._latest_data.get("S&P500", {})
        oil = cls._latest_data.get("CRUDE_OIL", {})
        gold = cls._latest_data.get("GOLD", {})
        
        sp_net = sp.get("net_long_pct", 0)
        sp_chg = sp.get("change", 0)
        oil_net = oil.get("net_long_pct", 0)
        gold_net = gold.get("net_long_pct", 0)
        
        bull_count = 0
        bear_count = 0
        reasons = []
        
        # S&P 500 순매수 분석
        if sp_net > config.cot_bull_threshold:
            bull_count += 1
            reasons.append(f"S&P 순매수 {sp_net:.0%}")
        elif sp_net < config.cot_bear_threshold:
            bear_count += 1
            reasons.append(f"S&P 순매도 {sp_net:.0%}")
        
        # 원유 순매수 (유가 상승 기대 = 인플레 위험)
        if oil_net > 0.4:
            bear_count += 1  # 고유가 = 주식에 부정적
            reasons.append(f"원유 순매수 {oil_net:.0%} (인플레 위험)")
        
        # 금 순매수 (안전자산 선호 = 위험 회피)
        if gold_net > 0.35:
            bear_count += 1
            reasons.append(f"금 순매수 {gold_net:.0%} (리스크 오프)")
        elif gold_net < 0.1:
            bull_count += 1
            reasons.append("금 매도 (리스크 온)")
        
        # 포지션 변화 방향
        if sp_chg > 0.05:
            bull_count += 1
            reasons.append(f"S&P 포지션 주간 +{sp_chg:.0%}")
        elif sp_chg < -0.05:
            bear_count += 1
            reasons.append(f"S&P 포지션 주간 {sp_chg:.0%}")
        
        if bull_count > bear_count:
            conf = min(0.8, 0.5 + (bull_count - bear_count) * 0.1)
            return MarketSignal.BULL, conf, " | ".join(reasons)
        elif bear_count > bull_count:
            conf = min(0.8, 0.5 + (bear_count - bull_count) * 0.1)
            return MarketSignal.BEAR, conf, " | ".join(reasons)
        
        return MarketSignal.NEUTRAL, 0.4, " | ".join(reasons) or "COT 중립"


# ═══════════════════════════════════════════════════════════════
#  내장 옵션 흐름 엔진
# ═══════════════════════════════════════════════════════════════

class BuiltInOptionsEngine:
    """
    옵션 Put/Call Ratio 기반 시장 센티먼트
    
    - P/C < 0.7: 콜옵션 압도적 → BULL (매수 기대)
    - P/C > 1.3: 풋옵션 압도적 → BEAR (매도/헤지 기대)
    - 0.7~1.3: 중립
    
    실제 운영 시 IB API로 실시간 옵션 OI 수집
    """
    
    _latest_pc_ratio = 1.05  # 현재 P/C Ratio
    _latest_vix = 27.19       # 현재 VIX
    
    @classmethod
    def update(cls, pc_ratio: float, vix: float = 0):
        cls._latest_pc_ratio = pc_ratio
        if vix > 0:
            cls._latest_vix = vix
    
    @classmethod
    def get_signal(cls, config: SignalBridgeConfig) -> tuple[str, float, str]:
        """
        옵션 신호
        Returns: (signal, confidence, reason)
        """
        if not config.options_enabled:
            return MarketSignal.NEUTRAL, 0.3, "옵션 비활성화"
        
        pc = cls._latest_pc_ratio
        vix = cls._latest_vix
        
        reasons = []
        signal = MarketSignal.NEUTRAL
        confidence = 0.4
        
        # P/C Ratio
        if pc < config.pc_ratio_bull:
            signal = MarketSignal.BULL
            confidence = min(0.85, 0.6 + (config.pc_ratio_bull - pc) * 0.5)
            reasons.append(f"P/C={pc:.2f} 콜 우세")
        elif pc > config.pc_ratio_bear:
            signal = MarketSignal.BEAR
            confidence = min(0.85, 0.6 + (pc - config.pc_ratio_bear) * 0.3)
            reasons.append(f"P/C={pc:.2f} 풋 우세")
        else:
            reasons.append(f"P/C={pc:.2f} 중립")
        
        # VIX 보조
        if vix > 30:
            if signal != MarketSignal.BEAR:
                signal = MarketSignal.BEAR
            confidence = min(confidence + 0.1, 0.9)
            reasons.append(f"VIX={vix:.1f} 공포")
        elif vix > 25:
            reasons.append(f"VIX={vix:.1f} 경계")
        elif vix < 15:
            if signal != MarketSignal.BULL:
                signal = MarketSignal.BULL
            reasons.append(f"VIX={vix:.1f} 안정")
        
        return signal, confidence, " | ".join(reasons)


# ═══════════════════════════════════════════════════════════════
#  Washout(Whipsaw) 방지 필터
# ═══════════════════════════════════════════════════════════════

class WashoutFilter:
    """
    MA Crossover 거짓 신호(Whipsaw) 방지
    
    규칙:
    1. Cooldown: 매매 후 N시간 동일 종목 재매매 금지
    2. MA Gap: 단기/장기 MA 간격 최소 X% 이상
    3. Confirmation: 크로스 후 N봉 연속 유지
    4. Rate Limit: 종목당 일일 최대 N회 신호
    
    기존 Wash Sale (tax_optimizer.py)과는 다른 개념:
    - Wash Sale = 세금 규정 (30일 동일종목 재매수 방지)
    - Washout = 기술적 신호 품질 (거짓 크로스 방지)
    """
    
    def __init__(self, config: SignalBridgeConfig = None):
        self.config = config or SignalBridgeConfig()
        self._last_trade_time: dict[str, datetime] = {}  # {symbol: last_trade_time}
        self._daily_signal_count: dict[str, int] = defaultdict(int)
        self._cross_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self._last_reset_date: Optional[str] = None
    
    def _reset_daily_counts(self):
        """일일 카운트 리셋"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_signal_count.clear()
            self._last_reset_date = today
    
    def record_trade(self, symbol: str):
        """매매 실행 기록"""
        self._last_trade_time[symbol] = datetime.now()
    
    def check(
        self,
        symbol: str,
        ma_short: float,
        ma_long: float,
        current_price: float,
    ) -> dict:
        """
        Washout 체크
        
        Returns:
            {
                "allowed": bool,
                "reason": str,
                "cooldown_remaining_min": int,
            }
        """
        if not self.config.washout_enabled:
            return {"allowed": True, "reason": ""}
        
        self._reset_daily_counts()
        
        # 1. Cooldown 체크
        if symbol in self._last_trade_time:
            elapsed = (datetime.now() - self._last_trade_time[symbol]).total_seconds()
            cooldown_sec = self.config.cooldown_hours * 3600
            if elapsed < cooldown_sec:
                remaining = int((cooldown_sec - elapsed) / 60)
                return {
                    "allowed": False,
                    "reason": f"⏱️ 쿨다운 {remaining}분 남음 ({self.config.cooldown_hours}시간 제한)",
                    "cooldown_remaining_min": remaining,
                }
        
        # 2. MA Gap 체크
        if ma_long > 0:
            gap_pct = abs(ma_short - ma_long) / ma_long * 100
            if gap_pct < self.config.min_ma_gap_pct:
                return {
                    "allowed": False,
                    "reason": f"📏 MA 간격 {gap_pct:.2f}% < {self.config.min_ma_gap_pct}% (너무 좁음)",
                    "cooldown_remaining_min": 0,
                }
        
        # 3. 일일 신호 제한
        if self._daily_signal_count[symbol] >= self.config.max_signals_per_day:
            return {
                "allowed": False,
                "reason": f"📊 일일 신호 {self._daily_signal_count[symbol]}/{self.config.max_signals_per_day} 초과",
                "cooldown_remaining_min": 0,
            }
        
        # 통과
        self._daily_signal_count[symbol] += 1
        return {"allowed": True, "reason": "", "cooldown_remaining_min": 0}


# ═══════════════════════════════════════════════════════════════
#  통합 브리지
# ═══════════════════════════════════════════════════════════════

class SignalBridge:
    """
    Signal Monitor ↔ Smart Trader 통합 브리지
    
    사용법 (smart_trader.py에서):
        from signal_bridge import SignalBridge
        bridge = SignalBridge()
        
        # 앙상블 분석 시
        market_signal = bridge.get_composite_signal()
        
        # 매수 전 체크
        if bridge.should_block_buy():
            # 매수 중단
        
        # 신호 가중치
        boost = bridge.get_ensemble_boost()
        
        # Washout 체크
        washout = bridge.check_washout("NVDA", ma10, ma30, price)
    """
    
    def __init__(self, config: SignalBridgeConfig = None):
        self.config = config or SignalBridgeConfig()
        self.cot_engine = BuiltInCOTEngine()
        self.options_engine = BuiltInOptionsEngine()
        self.washout_filter = WashoutFilter(self.config)
        self._latest_signal = CompositeSignal()
        self._server_thread = None
        self._running = False
    
    def get_composite_signal(self) -> CompositeSignal:
        """COT + 옵션 복합 신호 계산"""
        # COT 신호
        cot_sig, cot_conf, cot_reason = self.cot_engine.get_signal(self.config)
        
        # 옵션 신호
        opt_sig, opt_conf, opt_reason = self.options_engine.get_signal(self.config)
        
        # 복합 판정
        bull_score = 0
        bear_score = 0
        
        if cot_sig == MarketSignal.BULL:
            bull_score += cot_conf
        elif cot_sig == MarketSignal.BEAR:
            bear_score += cot_conf
        
        if opt_sig == MarketSignal.BULL:
            bull_score += opt_conf
        elif opt_sig == MarketSignal.BEAR:
            bear_score += opt_conf
        
        # 최종 판정
        if bull_score > bear_score and bull_score > 0.8:
            composite = MarketSignal.BULL
            confidence = min(0.9, bull_score / 2)
        elif bear_score > bull_score and bear_score > 0.8:
            composite = MarketSignal.BEAR
            confidence = min(0.9, bear_score / 2)
        else:
            composite = MarketSignal.NEUTRAL
            confidence = 0.4
        
        signal = CompositeSignal(
            composite=composite,
            cot_signal=cot_sig,
            options_signal=opt_sig,
            confidence=confidence,
            reason=f"COT: {cot_reason} | OPT: {opt_reason}",
            timestamp=datetime.now().isoformat(),
            cot_net_long=self.cot_engine._latest_data.get("S&P500", {}).get("net_long_pct", 0),
            pc_ratio=self.options_engine._latest_pc_ratio,
        )
        
        self._latest_signal = signal
        return signal
    
    def should_block_buy(self) -> tuple[bool, str]:
        """
        BEAR 시장 브레이크 — 매수 차단 여부
        
        Returns: (blocked, reason)
        """
        if not self.config.market_brake_enabled:
            return False, ""
        
        signal = self.get_composite_signal()
        
        if signal.is_bear and self.config.brake_on_bear:
            return True, (
                f"🛑 BEAR 시장 브레이크! 신규 매수 중단 | "
                f"COT: {signal.cot_signal} | "
                f"옵션: {signal.options_signal} | "
                f"P/C: {signal.pc_ratio:.2f}"
            )
        
        return False, ""
    
    def get_ensemble_boost(self) -> float:
        """
        앙상블 신뢰도 부스트
        - BULL: +0.15 (매수 신뢰도 증가)
        - BEAR: -0.15 (매수 신뢰도 감소)
        - NEUTRAL: 0
        """
        if not self.config.bull_boost_enabled:
            return 0.0
        
        signal = self._latest_signal
        
        if signal.is_bull:
            return self.config.bull_confidence_boost
        elif signal.is_bear:
            return -self.config.bull_confidence_boost
        
        return 0.0
    
    def get_ensemble_strategy_signal(self) -> dict:
        """
        앙상블의 6번째 전략으로 사용할 신호
        advanced_strategies.py의 StrategySignal 형태로 반환
        """
        signal = self.get_composite_signal()
        
        if signal.is_bull:
            return {
                "strategy_name": "SIGNAL_MONITOR",
                "signal": "BUY",
                "confidence": signal.confidence,
                "reason": f"시장 BULL — {signal.reason}",
                "weight": self.config.ensemble_weight,
            }
        elif signal.is_bear:
            return {
                "strategy_name": "SIGNAL_MONITOR",
                "signal": "SELL",
                "confidence": signal.confidence,
                "reason": f"시장 BEAR — {signal.reason}",
                "weight": self.config.ensemble_weight,
            }
        
        return {
            "strategy_name": "SIGNAL_MONITOR",
            "signal": "HOLD",
            "confidence": 0.3,
            "reason": f"시장 NEUTRAL — {signal.reason}",
            "weight": self.config.ensemble_weight,
        }
    
    def check_washout(
        self, symbol: str, 
        ma_short: float, ma_long: float, 
        price: float
    ) -> dict:
        """Washout 체크 (Whipsaw 방지)"""
        return self.washout_filter.check(symbol, ma_short, ma_long, price)
    
    def record_trade(self, symbol: str):
        """매매 기록 (Washout cooldown용)"""
        self.washout_filter.record_trade(symbol)
    
    def update_market_data(
        self,
        pc_ratio: float = None,
        vix: float = None,
        cot_sp_net: float = None,
        cot_sp_change: float = None,
        cot_oil_net: float = None,
        cot_gold_net: float = None,
    ):
        """실시간 데이터 업데이트 (IB API 또는 수동)"""
        if pc_ratio is not None:
            self.options_engine.update(pc_ratio, vix or 0)
        if vix is not None and pc_ratio is None:
            self.options_engine._latest_vix = vix
        if cot_sp_net is not None:
            self.cot_engine.update_data(
                "S&P500", cot_sp_net, cot_sp_change or 0
            )
        if cot_oil_net is not None:
            self.cot_engine.update_data("CRUDE_OIL", cot_oil_net, 0)
        if cot_gold_net is not None:
            self.cot_engine.update_data("GOLD", cot_gold_net, 0)
    
    def print_status(self):
        """현재 시그널 상태 출력"""
        sig = self.get_composite_signal()
        icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
        
        print(f"\n  {icon[sig.composite]} Signal Monitor: {sig.composite} "
              f"(신뢰도: {sig.confidence:.0%})")
        print(f"    COT: {sig.cot_signal} | 옵션: {sig.options_signal}")
        print(f"    P/C Ratio: {sig.pc_ratio:.2f} | "
              f"S&P 순매수: {sig.cot_net_long:.0%}")
        print(f"    {sig.reason}")
        
        blocked, reason = self.should_block_buy()
        if blocked:
            print(f"    {reason}")


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  📡 Signal Bridge 데모 — COT + 옵션 + Washout 통합      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    bridge = SignalBridge()
    
    # 1) 현재 시장 데이터 (이란 전쟁 3주차)
    print("  📊 현재 시장 데이터 설정:")
    bridge.update_market_data(
        pc_ratio=1.05,          # 풋/콜 약간 풋 우세
        vix=27.19,              # VIX 높음 (경계)
        cot_sp_net=0.25,        # S&P 순매수 25%
        cot_sp_change=-0.05,    # 주간 감소
        cot_oil_net=0.45,       # 원유 순매수 45% (고유가 기대)
        cot_gold_net=0.38,      # 금 순매수 38% (안전자산)
    )
    
    # 2) 복합 신호
    bridge.print_status()
    
    # 3) 앙상블 전략 신호
    print("\n  🎯 앙상블 6번째 전략:")
    ens = bridge.get_ensemble_strategy_signal()
    print(f"    {ens['strategy_name']} → {ens['signal']} "
          f"(신뢰도: {ens['confidence']:.0%}, 가중치: {ens['weight']})")
    print(f"    이유: {ens['reason']}")
    
    # 4) BEAR 브레이크 체크
    print("\n  🛑 매수 브레이크 체크:")
    blocked, reason = bridge.should_block_buy()
    print(f"    매수 차단: {'예' if blocked else '아니오'}")
    if reason:
        print(f"    {reason}")
    
    # 5) Washout 체크
    print("\n  🔄 Washout 체크 (NVDA):")
    # 첫 번째 매매
    wo1 = bridge.check_washout("NVDA", 182.0, 185.0, 183.5)
    print(f"    1차: {'허용' if wo1['allowed'] else '차단'} {wo1.get('reason','')}")
    
    # 매매 기록
    bridge.record_trade("NVDA")
    
    # 바로 재매매 시도
    wo2 = bridge.check_washout("NVDA", 182.5, 184.8, 184.0)
    print(f"    2차 (직후): {'허용' if wo2['allowed'] else '차단'} {wo2.get('reason','')}")
    
    # 6) BULL 시나리오
    print("\n  📈 BULL 시나리오 테스트:")
    bridge.update_market_data(pc_ratio=0.6, vix=15, cot_sp_net=0.4, cot_sp_change=0.1, cot_gold_net=0.05)
    bridge.print_status()
    boost = bridge.get_ensemble_boost()
    print(f"    앙상블 부스트: {boost:+.2f}")


if __name__ == "__main__":
    demo()
