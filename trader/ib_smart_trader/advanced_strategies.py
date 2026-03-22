"""
═══════════════════════════════════════════════════════════════════
  Advanced Strategies Module - 고급 전략 엔진
  
  추가 전략:
    3. ATR 동적 손절/익절 - 변동성 기반 리스크 관리
    4. 적응형 RSI - 트렌드 맥락 기반 과매수/과매도 신호
    5. 멀티 전략 앙상블 - 복수 전략 합의 기반 매매
  
  기존 전략과 통합:
    1. MA Crossover (smart_trader.py)
    2. % 변동 (smart_trader.py)
═══════════════════════════════════════════════════════════════════
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  신호 타입 (smart_trader.py와 호환)
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class StrategySignal:
    """개별 전략의 신호"""
    strategy_name: str
    signal: SignalType
    confidence: float      # 0.0 ~ 1.0 신뢰도
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class EnsembleDecision:
    """앙상블 최종 결정"""
    symbol: str
    final_signal: SignalType
    consensus_score: float       # -1.0 (강한 SELL) ~ +1.0 (강한 BUY)
    individual_signals: list     # 각 전략의 신호
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    atr_value: float = 0.0
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __str__(self):
        sigs = ", ".join(
            f"{s.strategy_name}={s.signal.name}({s.confidence:.0%})"
            for s in self.individual_signals
        )
        sl = f"SL=${self.stop_loss_price:.2f}" if self.stop_loss_price > 0 else "SL=없음"
        tp = f"TP=${self.take_profit_price:.2f}" if self.take_profit_price > 0 else "TP=없음"
        return (
            f"{self.final_signal.value} {self.symbol} | "
            f"합의: {self.consensus_score:+.2f} | "
            f"{sl} | {tp} | "
            f"전략: [{sigs}]"
        )


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class AdvancedConfig:
    """고급 전략 설정"""
    
    # ── ATR 동적 손절 ──
    atr_period: int = 14              # ATR 계산 기간
    atr_stop_multiplier: float = 2.0  # 손절 = 현재가 - (ATR × 배수)
    atr_profit_multiplier: float = 3.0  # 익절 = 현재가 + (ATR × 배수)
    trailing_stop_enabled: bool = True  # 트레일링 손절 활성화
    trailing_atr_multiplier: float = 1.5  # 트레일링 배수
    
    # ── 적응형 RSI ──
    rsi_period: int = 14
    # 상승 추세에서의 RSI 기준 (더 공격적)
    rsi_bull_oversold: float = 40.0     # 상승장 과매도 (일반: 30)
    rsi_bull_overbought: float = 80.0   # 상승장 과매수 (일반: 70)
    # 하락 추세에서의 RSI 기준 (더 보수적)
    rsi_bear_oversold: float = 20.0     # 하락장 과매도
    rsi_bear_overbought: float = 60.0   # 하락장 과매수
    # 다이버전스 감지
    rsi_divergence_lookback: int = 10   # 다이버전스 확인 기간
    
    # ── 앙상블 ──
    ensemble_buy_threshold: float = 0.4   # 이 값 이상이면 BUY
    ensemble_sell_threshold: float = -0.4  # 이 값 이하이면 SELL
    min_strategies_agree: int = 3          # 최소 N개 전략 동의 필요
    
    # ── 전략별 가중치 (합 = 1.0) ──
    weight_ma_crossover: float = 0.25
    weight_pct_change: float = 0.15
    weight_adaptive_rsi: float = 0.25
    weight_atr_trend: float = 0.15
    weight_volume_confirm: float = 0.20


# ═══════════════════════════════════════════════════════════════
#  전략 3: ATR 동적 손절/익절
# ═══════════════════════════════════════════════════════════════

class ATRStopLoss:
    """
    ATR(Average True Range) 기반 동적 리스크 관리
    
    핵심:
    - 변동성이 클 때 → 넓은 손절 (불필요한 조기 손절 방지)
    - 변동성이 작을 때 → 좁은 손절 (작은 손실도 빠르게 차단)
    - 트레일링 스탑: 수익이 나면 손절선이 따라 올라감
    """
    
    @staticmethod
    def calculate_atr(
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series, 
        period: int = 14
    ) -> pd.Series:
        """
        True Range 및 ATR 계산
        TR = max(H-L, |H-이전C|, |L-이전C|)
        ATR = TR의 N일 이동평균
        """
        prev_close = close.shift(1)
        
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        
        return atr
    
    @staticmethod
    def get_stop_levels(
        current_price: float,
        atr_value: float,
        config: AdvancedConfig,
        position_side: str = "LONG"
    ) -> dict:
        """
        현재 가격과 ATR로 손절/익절 레벨 계산
        
        Returns:
            {
                "stop_loss": 손절 가격,
                "take_profit": 익절 가격,
                "trailing_stop": 트레일링 손절 거리,
                "risk_reward_ratio": 손익비,
            }
        """
        if position_side == "LONG":
            stop_loss = current_price - (atr_value * config.atr_stop_multiplier)
            take_profit = current_price + (atr_value * config.atr_profit_multiplier)
            trailing_distance = atr_value * config.trailing_atr_multiplier
        else:  # SHORT (향후 확장용)
            stop_loss = current_price + (atr_value * config.atr_stop_multiplier)
            take_profit = current_price - (atr_value * config.atr_profit_multiplier)
            trailing_distance = atr_value * config.trailing_atr_multiplier
        
        risk = abs(current_price - stop_loss)
        reward = abs(take_profit - current_price)
        rr_ratio = reward / risk if risk > 0 else 0
        
        return {
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "trailing_distance": round(trailing_distance, 2),
            "risk_reward_ratio": round(rr_ratio, 2),
            "risk_pct": round((risk / current_price) * 100, 2),
            "reward_pct": round((reward / current_price) * 100, 2),
            "atr": round(atr_value, 2),
        }
    
    @staticmethod
    def check_atr_trend_signal(
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        config: AdvancedConfig,
    ) -> StrategySignal:
        """
        ATR 트렌드 확인 — 변동성 확장/수축 감지
        - ATR 증가 + 가격 상승 = 강한 상승 모멘텀 (BUY)
        - ATR 증가 + 가격 하락 = 강한 하락 모멘텀 (SELL)
        - ATR 감소 = 에너지 축적 중 (HOLD, 브레이크아웃 대기)
        """
        atr = ATRStopLoss.calculate_atr(high, low, close, config.atr_period)
        
        if len(atr.dropna()) < 5:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="데이터 부족",
            )
        
        current_atr = atr.iloc[-1]
        prev_atr = atr.iloc[-5:].mean()
        atr_change = (current_atr - prev_atr) / prev_atr * 100 if prev_atr > 0 else 0
        
        price_change = (close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100
        
        # ATR 확장 + 가격 상승 = 강한 매수
        if atr_change > 10 and price_change > 2:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.BUY,
                confidence=min(0.9, 0.5 + atr_change / 100),
                reason=f"변동성 확장 +{atr_change:.1f}% + 가격 +{price_change:.1f}%",
                metadata={"atr": current_atr, "atr_change": atr_change},
            )
        
        # ATR 확장 + 가격 하락 = 강한 매도
        if atr_change > 10 and price_change < -2:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.SELL,
                confidence=min(0.9, 0.5 + atr_change / 100),
                reason=f"변동성 확장 +{atr_change:.1f}% + 가격 {price_change:.1f}%",
                metadata={"atr": current_atr, "atr_change": atr_change},
            )
        
        return StrategySignal(
            strategy_name="ATR_TREND",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"ATR 변동: {atr_change:+.1f}%, 가격: {price_change:+.1f}%",
            metadata={"atr": current_atr, "atr_change": atr_change},
        )


# ═══════════════════════════════════════════════════════════════
#  전략 4: 적응형 RSI
# ═══════════════════════════════════════════════════════════════

class AdaptiveRSI:
    """
    적응형 RSI - 트렌드에 따라 과매수/과매도 기준 변경
    
    핵심:
    - 상승 추세: RSI 40에서 매수 (일반 30보다 빠른 진입)
    - 하락 추세: RSI 20까지 기다림 (더 보수적)
    - 다이버전스: 가격은 신저가인데 RSI는 높아짐 → 반등 신호
    """
    
    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """RSI 시리즈 계산"""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        
        # Wilder's smoothing (EMA)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def detect_trend(prices: pd.Series, ma_period: int = 50) -> str:
        """
        추세 판별
        - 가격 > 50일 MA → BULL
        - 가격 < 50일 MA → BEAR
        - 가격 ≈ 50일 MA → NEUTRAL
        """
        if len(prices) < ma_period:
            return "NEUTRAL"
        
        ma = prices.rolling(ma_period).mean()
        current = prices.iloc[-1]
        current_ma = ma.iloc[-1]
        
        if pd.isna(current_ma):
            return "NEUTRAL"
        
        pct_from_ma = (current - current_ma) / current_ma * 100
        
        if pct_from_ma > 2:
            return "BULL"
        elif pct_from_ma < -2:
            return "BEAR"
        else:
            return "NEUTRAL"
    
    @staticmethod
    def detect_divergence(
        prices: pd.Series, 
        rsi: pd.Series, 
        lookback: int = 10
    ) -> Optional[str]:
        """
        RSI 다이버전스 감지
        
        - Bullish Divergence: 가격 ↓ 신저가, RSI ↑ = 반등 임박
        - Bearish Divergence: 가격 ↑ 신고가, RSI ↓ = 하락 임박
        """
        if len(prices) < lookback + 1 or len(rsi) < lookback + 1:
            return None
        
        recent_prices = prices.iloc[-lookback:]
        recent_rsi = rsi.iloc[-lookback:]
        
        # NaN 제거
        if recent_rsi.isna().any():
            return None
        
        price_min_idx = recent_prices.idxmin()
        price_max_idx = recent_prices.idxmax()
        
        current_price = prices.iloc[-1]
        current_rsi = rsi.iloc[-1]
        
        # Bullish: 가격은 낮은데 RSI는 이전 저점보다 높음
        if (current_price <= recent_prices.quantile(0.2) and 
            current_rsi > recent_rsi.min() + 5):
            return "BULLISH"
        
        # Bearish: 가격은 높은데 RSI는 이전 고점보다 낮음
        if (current_price >= recent_prices.quantile(0.8) and
            current_rsi < recent_rsi.max() - 5):
            return "BEARISH"
        
        return None
    
    @staticmethod
    def get_signal(
        prices: pd.Series,
        config: AdvancedConfig,
    ) -> StrategySignal:
        """
        적응형 RSI 매매 신호 생성
        
        로직:
        1. 현재 추세 판별 (BULL/BEAR/NEUTRAL)
        2. 추세에 맞는 RSI 기준 적용
        3. 다이버전스 확인 (보너스 신뢰도)
        4. 최종 신호 생성
        """
        rsi_series = AdaptiveRSI.calculate_rsi(prices, config.rsi_period)
        
        if rsi_series.isna().all() or len(rsi_series.dropna()) < 5:
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="RSI 데이터 부족",
            )
        
        current_rsi = float(rsi_series.iloc[-1])
        trend = AdaptiveRSI.detect_trend(prices)
        divergence = AdaptiveRSI.detect_divergence(
            prices, rsi_series, config.rsi_divergence_lookback
        )
        
        # 추세에 따른 기준 선택
        if trend == "BULL":
            oversold = config.rsi_bull_oversold
            overbought = config.rsi_bull_overbought
            trend_label = "상승추세"
        elif trend == "BEAR":
            oversold = config.rsi_bear_oversold
            overbought = config.rsi_bear_overbought
            trend_label = "하락추세"
        else:
            oversold = 30.0
            overbought = 70.0
            trend_label = "중립"
        
        # 기본 신뢰도
        confidence = 0.5
        
        # ── 매수 신호 ──
        if current_rsi <= oversold:
            confidence = 0.7
            reason = f"RSI {current_rsi:.1f} ≤ {oversold} ({trend_label} 과매도)"
            
            # 다이버전스 보너스
            if divergence == "BULLISH":
                confidence = 0.9
                reason += " + 🔥 Bullish 다이버전스!"
            
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.BUY,
                confidence=confidence,
                reason=reason,
                metadata={
                    "rsi": current_rsi, "trend": trend,
                    "oversold": oversold, "overbought": overbought,
                    "divergence": divergence,
                },
            )
        
        # ── 매도 신호 ──
        if current_rsi >= overbought:
            confidence = 0.7
            reason = f"RSI {current_rsi:.1f} ≥ {overbought} ({trend_label} 과매수)"
            
            if divergence == "BEARISH":
                confidence = 0.9
                reason += " + ⚠️ Bearish 다이버전스!"
            
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.SELL,
                confidence=confidence,
                reason=reason,
                metadata={
                    "rsi": current_rsi, "trend": trend,
                    "oversold": oversold, "overbought": overbought,
                    "divergence": divergence,
                },
            )
        
        # ── HOLD ──
        # 강세 구간(50~70)에서는 약한 매수 신호
        if trend == "BULL" and 50 < current_rsi < 65:
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.BUY,
                confidence=0.4,
                reason=f"RSI {current_rsi:.1f} 강세 구간 ({trend_label})",
                metadata={"rsi": current_rsi, "trend": trend},
            )
        
        return StrategySignal(
            strategy_name="ADAPTIVE_RSI",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"RSI {current_rsi:.1f} 중립 ({trend_label}, 기준: {oversold}/{overbought})",
            metadata={"rsi": current_rsi, "trend": trend},
        )


# ═══════════════════════════════════════════════════════════════
#  전략 5: 거래량 확인 (앙상블 보조 전략)
# ═══════════════════════════════════════════════════════════════

class VolumeConfirmation:
    """거래량 기반 신호 확인"""
    
    @staticmethod
    def get_signal(
        close: pd.Series,
        volume: pd.Series,
    ) -> StrategySignal:
        """
        거래량 + 가격 방향 확인
        - 거래량 급증 + 가격 상승 = BUY 확인
        - 거래량 급증 + 가격 하락 = SELL 확인
        - 거래량 감소 = 신뢰도 낮음
        """
        if len(volume) < 20:
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="데이터 부족",
            )
        
        avg_vol_20 = float(volume.iloc[-20:].mean())
        recent_vol = float(volume.iloc[-3:].mean())
        vol_ratio = recent_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
        
        price_change_3d = (
            (close.iloc[-1] - close.iloc[-4]) / close.iloc[-4] * 100
            if len(close) >= 4 else 0
        )
        
        # 거래량 급증 + 가격 상승
        if vol_ratio > 1.5 and price_change_3d > 1:
            conf = min(0.9, 0.5 + (vol_ratio - 1) * 0.3)
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.BUY,
                confidence=conf,
                reason=f"거래량 {vol_ratio:.1f}x + 가격 +{price_change_3d:.1f}%",
                metadata={"vol_ratio": vol_ratio, "price_3d": price_change_3d},
            )
        
        # 거래량 급증 + 가격 하락
        if vol_ratio > 1.5 and price_change_3d < -1:
            conf = min(0.9, 0.5 + (vol_ratio - 1) * 0.3)
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.SELL,
                confidence=conf,
                reason=f"거래량 {vol_ratio:.1f}x + 가격 {price_change_3d:.1f}%",
                metadata={"vol_ratio": vol_ratio, "price_3d": price_change_3d},
            )
        
        return StrategySignal(
            strategy_name="VOLUME_CONFIRM",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"거래량 {vol_ratio:.1f}x, 가격 {price_change_3d:+.1f}%",
            metadata={"vol_ratio": vol_ratio},
        )


# ═══════════════════════════════════════════════════════════════
#  전략 앙상블 엔진
# ═══════════════════════════════════════════════════════════════

class StrategyEnsemble:
    """
    멀티 전략 앙상블 - 여러 전략의 합의로 최종 결정
    
    작동 방식:
    1. 5개 전략 각각 신호 생성 (BUY/SELL/HOLD + 신뢰도)
    2. 가중 합산: BUY=+1, SELL=-1, HOLD=0 × 신뢰도 × 가중치
    3. 합산 점수가 threshold 초과 시 매매
    4. ATR로 손절/익절 자동 설정
    
    장점:
    - 단일 전략의 거짓 신호(whipsaw) 대폭 감소
    - 여러 전략이 동시에 합의할 때만 매매 → 승률 향상
    - 각 전략의 강점을 결합 (추세 + 모멘텀 + 변동성)
    """
    
    def __init__(self, config: AdvancedConfig = None):
        self.config = config or AdvancedConfig()
        self.atr_engine = ATRStopLoss()
        self.rsi_engine = AdaptiveRSI()
        self.volume_engine = VolumeConfirmation()
    
    def analyze(
        self,
        symbol: str,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        volume: pd.Series,
        ma_signal: Optional[SignalType] = None,
        pct_signal: Optional[SignalType] = None,
        pct_change: float = 0.0,
    ) -> EnsembleDecision:
        """
        모든 전략 실행 → 앙상블 결정 반환
        
        Parameters:
            symbol: 종목 코드
            close, high, low, volume: OHLCV 데이터
            ma_signal: 기존 MA Crossover 신호 (smart_trader.py에서 전달)
            pct_signal: 기존 % 변동 신호 (smart_trader.py에서 전달)
            pct_change: % 변동 값
        """
        signals: list[StrategySignal] = []
        cfg = self.config
        
        # ── 전략 1: MA Crossover (기존, 외부에서 전달) ──
        if ma_signal is not None:
            ma_conf = 0.7 if ma_signal != SignalType.HOLD else 0.3
            signals.append(StrategySignal(
                strategy_name="MA_CROSSOVER",
                signal=ma_signal,
                confidence=ma_conf,
                reason="골든/데드 크로스" if ma_signal != SignalType.HOLD else "크로스 없음",
            ))
        
        # ── 전략 2: % 변동 (기존, 외부에서 전달) ──
        if pct_signal is not None:
            pct_conf = min(0.8, 0.3 + abs(pct_change) / 20)
            signals.append(StrategySignal(
                strategy_name="PCT_CHANGE",
                signal=pct_signal,
                confidence=pct_conf,
                reason=f"{pct_change:+.1f}% 변동",
            ))
        
        # ── 전략 3: ATR 트렌드 ──
        atr_signal = self.atr_engine.check_atr_trend_signal(
            close, high, low, cfg
        )
        signals.append(atr_signal)
        
        # ── 전략 4: 적응형 RSI ──
        rsi_signal = self.rsi_engine.get_signal(close, cfg)
        signals.append(rsi_signal)
        
        # ── 전략 5: 거래량 확인 ──
        vol_signal = self.volume_engine.get_signal(close, volume)
        signals.append(vol_signal)
        
        # ═══ 앙상블 점수 계산 ═══
        weight_map = {
            "MA_CROSSOVER": cfg.weight_ma_crossover,
            "PCT_CHANGE": cfg.weight_pct_change,
            "ATR_TREND": cfg.weight_atr_trend,
            "ADAPTIVE_RSI": cfg.weight_adaptive_rsi,
            "VOLUME_CONFIRM": cfg.weight_volume_confirm,
        }
        
        consensus_score = 0.0
        buy_count = 0
        sell_count = 0
        
        for sig in signals:
            weight = weight_map.get(sig.strategy_name, 0.1)
            
            if sig.signal == SignalType.BUY:
                consensus_score += sig.confidence * weight
                buy_count += 1
            elif sig.signal == SignalType.SELL:
                consensus_score -= sig.confidence * weight
                sell_count += 1
            # HOLD contributes 0
        
        # 정규화 (-1 ~ +1)
        max_possible = sum(weight_map.values())
        if max_possible > 0:
            consensus_score = consensus_score / max_possible
        
        # ═══ 최종 결정 ═══
        final_signal = SignalType.HOLD
        reason_parts = []
        
        if (consensus_score >= cfg.ensemble_buy_threshold and 
            buy_count >= cfg.min_strategies_agree):
            final_signal = SignalType.BUY
            reason_parts.append(
                f"합의 BUY ({buy_count}개 전략 동의, "
                f"점수: {consensus_score:+.2f})"
            )
        elif (consensus_score <= cfg.ensemble_sell_threshold and 
              sell_count >= cfg.min_strategies_agree):
            final_signal = SignalType.SELL
            reason_parts.append(
                f"합의 SELL ({sell_count}개 전략 동의, "
                f"점수: {consensus_score:+.2f})"
            )
        else:
            reason_parts.append(
                f"합의 미달 (BUY:{buy_count} SELL:{sell_count}, "
                f"점수: {consensus_score:+.2f}, "
                f"기준: ±{cfg.ensemble_buy_threshold})"
            )
        
        # ═══ ATR 손절/익절 계산 ═══
        current_price = float(close.iloc[-1])
        atr_series = self.atr_engine.calculate_atr(high, low, close, cfg.atr_period)
        atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
        
        stop_levels = self.atr_engine.get_stop_levels(
            current_price, atr_value, cfg
        )
        
        reason_parts.append(
            f"ATR=${atr_value:.2f} | "
            f"SL=${stop_levels['stop_loss']} ({stop_levels['risk_pct']}%) | "
            f"TP=${stop_levels['take_profit']} ({stop_levels['reward_pct']}%) | "
            f"R:R={stop_levels['risk_reward_ratio']}"
        )
        
        return EnsembleDecision(
            symbol=symbol,
            final_signal=final_signal,
            consensus_score=round(consensus_score, 3),
            individual_signals=signals,
            stop_loss_price=stop_levels["stop_loss"],
            take_profit_price=stop_levels["take_profit"],
            atr_value=atr_value,
            reason=" | ".join(reason_parts),
        )


# ═══════════════════════════════════════════════════════════════
#  테스트 / 데모
# ═══════════════════════════════════════════════════════════════

def demo():
    """데모 — 가상 데이터로 앙상블 테스트"""
    np.random.seed(42)
    
    # 60일 가상 가격 데이터 (상승 추세)
    n = 60
    base = 100
    trend = np.linspace(0, 20, n)
    noise = np.random.randn(n) * 2
    close_arr = base + trend + noise
    high_arr = close_arr + np.abs(np.random.randn(n)) * 1.5
    low_arr = close_arr - np.abs(np.random.randn(n)) * 1.5
    vol_arr = np.random.randint(500000, 2000000, n).astype(float)
    # 최근 거래량 급증 시뮬레이션
    vol_arr[-5:] *= 2.5
    
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = pd.Series(close_arr, index=dates)
    high = pd.Series(high_arr, index=dates)
    low = pd.Series(low_arr, index=dates)
    volume = pd.Series(vol_arr, index=dates)
    
    print("=" * 70)
    print("  🧪 Advanced Strategies 데모")
    print("=" * 70)
    
    config = AdvancedConfig()
    ensemble = StrategyEnsemble(config)
    
    # 앙상블 분석 실행
    decision = ensemble.analyze(
        symbol="DEMO",
        close=close,
        high=high,
        low=low,
        volume=volume,
        ma_signal=SignalType.BUY,   # 가정: 골든크로스 발생
        pct_signal=SignalType.HOLD,
        pct_change=2.3,
    )
    
    print(f"\n  📊 앙상블 결정:")
    print(f"  {decision}")
    print(f"\n  개별 전략 결과:")
    for sig in decision.individual_signals:
        print(
            f"    {sig.strategy_name:18s} | {sig.signal.value} | "
            f"신뢰도: {sig.confidence:.0%} | {sig.reason}"
        )
    
    print(f"\n  🛡️ 리스크 관리:")
    print(f"    손절가: ${decision.stop_loss_price}")
    print(f"    익절가: ${decision.take_profit_price}")
    print(f"    ATR: ${decision.atr_value:.2f}")
    print(f"\n  최종: {decision.final_signal.value} "
          f"(합의 점수: {decision.consensus_score:+.3f})")
    print("=" * 70)


if __name__ == "__main__":
    demo()
