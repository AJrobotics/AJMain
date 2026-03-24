"""
═══════════════════════════════════════════════════════════════════
  Day Trading Strategies - 분봉 기반 데이 트레이딩 전략 엔진

  전략 (4개 앙상블):
    1. VWAP Bounce     (30%) - VWAP 지지/저항 반등 매매
    2. EMA Scalp       (25%) - EMA(9)/EMA(21) 크로스 스캘핑
    3. Volume Breakout (25%) - 거래량 급증 + 가격 돌파
    4. RSI+MACD Combo  (20%) - 과매수/과매도 + MACD 확인

  타임프레임: 1분봉, 5분봉
  대상: 유동성 높은 대형주 + 프리마켓 갭 종목
═══════════════════════════════════════════════════════════════════
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  신호 타입 (기존 시스템 호환)
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class DaySignal:
    """개별 전략의 분봉 신호"""
    strategy_name: str
    signal: SignalType
    confidence: float      # 0.0 ~ 1.0
    reason: str
    timeframe: str = "5min"  # "1min" or "5min"
    metadata: dict = field(default_factory=dict)


@dataclass
class DayEnsembleDecision:
    """데이 트레이딩 앙상블 최종 결정"""
    symbol: str
    final_signal: SignalType
    consensus_score: float       # -1.0 ~ +1.0
    individual_signals: list
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    position_size_pct: float = 0.0  # 자본 대비 포지션 크기 %
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
class DayStrategyConfig:
    """데이 트레이딩 전략 설정"""

    # ── VWAP Bounce ──
    vwap_bounce_zone_pct: float = 0.3      # VWAP ± 0.3% 내 접근 시 반등 감지 (기존 0.15)
    vwap_bounce_confirm_bars: int = 1      # 반등 확인 1봉 (기존 2)
    vwap_morning_boost: bool = True        # 9:30~10:30 ET 가중치 부여
    vwap_morning_boost_pct: float = 0.15   # 오전 추가 신뢰도

    # ── EMA Scalp ──
    ema_fast_period: int = 8               # 더 민감한 EMA (기존 9)
    ema_slow_period: int = 18              # 더 빠른 크로스 감지 (기존 21)
    ema_confirm_ticks: float = 0.05        # 크로스 후 확인 거리 ($)
    ema_scalp_target_pct: float = 0.5      # 스캘핑 목표 0.5% (기존 0.4)
    ema_scalp_stop_pct: float = 0.25       # 스캘핑 손절 0.25% (기존 0.2)

    # ── Volume Breakout ──
    vol_spike_ratio: float = 1.3           # 평균 대비 1.3배 이상 (기존 2.0)
    vol_lookback_bars: int = 15            # 평균 거래량 계산 기간 (기존 20)
    vol_breakout_confirm: int = 1          # 돌파 후 1봉 유지 (기존 2)
    vol_high_lookback: int = 8             # 고점/저점 참조 기간 (기존 10)

    # ── RSI + MACD ──
    rsi_period: int = 10                   # 더 민감한 RSI (기존 14)
    rsi_oversold: float = 35.0             # 과매도 기준 완화 (기존 30)
    rsi_overbought: float = 65.0           # 과매수 기준 완화 (기존 70)
    macd_fast: int = 8                     # 더 빠른 MACD (기존 12)
    macd_slow: int = 21                    # (기존 26)
    macd_signal: int = 7                   # (기존 9)

    # ── 앙상블 ──
    ensemble_buy_threshold: float = 0.20   # 매수 임계값 낮춤 (기존 0.35)
    ensemble_sell_threshold: float = -0.20  # 매도 임계값 낮춤 (기존 -0.35)
    min_strategies_agree: int = 1          # 최소 1개 전략만 동의해도 매매 (기존 2)

    # ── 가중치 ──
    weight_vwap: float = 0.30
    weight_ema: float = 0.30              # EMA 가중치 증가 (기존 0.25)
    weight_volume: float = 0.20           # (기존 0.25)
    weight_rsi_macd: float = 0.20

    # ── 손절/익절 (ATR 기반) ──
    atr_period: int = 10                   # 더 민감한 ATR (기존 14)
    atr_stop_multiplier: float = 1.2       # 더 좁은 손절 (기존 1.5)
    atr_profit_multiplier: float = 2.0     # 목표 R:R = 1.67


# ═══════════════════════════════════════════════════════════════
#  기술적 지표 계산
# ═══════════════════════════════════════════════════════════════

class DayIndicators:
    """분봉 기술적 지표"""

    @staticmethod
    def vwap(close: pd.Series, high: pd.Series, low: pd.Series,
             volume: pd.Series) -> pd.Series:
        """
        VWAP (Volume Weighted Average Price)
        = 누적(TP × Volume) / 누적(Volume)
        TP = (High + Low + Close) / 3
        """
        typical_price = (high + low + close) / 3
        cum_tp_vol = (typical_price * volume).cumsum()
        cum_vol = volume.cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    @staticmethod
    def ema(prices: pd.Series, period: int) -> pd.Series:
        """지수 이동평균"""
        return prices.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """RSI (Relative Strength Index)"""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(prices: pd.Series, fast: int = 12, slow: int = 26,
             signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD
        Returns: (macd_line, signal_line, histogram)
        """
        ema_fast = prices.ewm(span=fast, adjust=False).mean()
        ema_slow = prices.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def atr(high: pd.Series, low: pd.Series, close: pd.Series,
            period: int = 14) -> pd.Series:
        """ATR (Average True Range)"""
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return true_range.rolling(window=period).mean()


# ═══════════════════════════════════════════════════════════════
#  전략 1: VWAP Bounce
# ═══════════════════════════════════════════════════════════════

class VWAPBounce:
    """
    VWAP 지지/저항 반등 전략

    핵심:
    - VWAP은 당일 기관 매매의 평균 가격 → 강력한 지지/저항선
    - 가격이 VWAP 아래에서 터치 후 반등 → 매수 (기관 매수 구간)
    - 가격이 VWAP 위에서 터치 후 하락 → 매도 (기관 매도 구간)
    - 오전 세션(9:30-10:30)에 VWAP 접근 시 신뢰도 가중
    """

    @staticmethod
    def get_signal(
        close: pd.Series, high: pd.Series, low: pd.Series,
        volume: pd.Series, config: DayStrategyConfig,
        is_morning: bool = False,
    ) -> DaySignal:
        if len(close) < 20:
            return DaySignal("VWAP_BOUNCE", SignalType.HOLD, 0.0, "데이터 부족")

        vwap = DayIndicators.vwap(close, high, low, volume)
        current_price = float(close.iloc[-1])
        current_vwap = float(vwap.iloc[-1])

        if pd.isna(current_vwap) or current_vwap <= 0:
            return DaySignal("VWAP_BOUNCE", SignalType.HOLD, 0.0, "VWAP 계산 불가")

        # VWAP 대비 거리 (%)
        dist_pct = (current_price - current_vwap) / current_vwap * 100
        bounce_zone = config.vwap_bounce_zone_pct

        # 최근 봉 방향 확인 (반등 여부)
        recent_trend = 0.0
        if len(close) >= config.vwap_bounce_confirm_bars + 1:
            for i in range(1, config.vwap_bounce_confirm_bars + 1):
                recent_trend += close.iloc[-i] - close.iloc[-i-1]

        confidence = 0.5

        # ── 매수: 가격이 VWAP 아래/근처에서 반등 ──
        if -bounce_zone * 3 <= dist_pct <= bounce_zone and recent_trend > 0:
            confidence = 0.65
            # VWAP에 가까울수록 신뢰도 높음
            proximity_bonus = max(0, (bounce_zone - abs(dist_pct)) / bounce_zone * 0.15)
            confidence += proximity_bonus

            if is_morning and config.vwap_morning_boost:
                confidence += config.vwap_morning_boost_pct

            confidence = min(0.95, confidence)
            return DaySignal(
                "VWAP_BOUNCE", SignalType.BUY, confidence,
                f"VWAP ${current_vwap:.2f} 지지 반등 (거리: {dist_pct:+.2f}%)",
                metadata={"vwap": current_vwap, "dist_pct": dist_pct},
            )

        # ── 매도: 가격이 VWAP 위/근처에서 하락 ──
        if -bounce_zone <= dist_pct <= bounce_zone * 3 and recent_trend < 0:
            confidence = 0.65
            proximity_bonus = max(0, (bounce_zone - abs(dist_pct)) / bounce_zone * 0.15)
            confidence += proximity_bonus

            if is_morning and config.vwap_morning_boost:
                confidence += config.vwap_morning_boost_pct

            confidence = min(0.95, confidence)
            return DaySignal(
                "VWAP_BOUNCE", SignalType.SELL, confidence,
                f"VWAP ${current_vwap:.2f} 저항 하락 (거리: {dist_pct:+.2f}%)",
                metadata={"vwap": current_vwap, "dist_pct": dist_pct},
            )

        return DaySignal(
            "VWAP_BOUNCE", SignalType.HOLD, 0.3,
            f"VWAP ${current_vwap:.2f} | 거리: {dist_pct:+.2f}% (반등 미감지)",
            metadata={"vwap": current_vwap, "dist_pct": dist_pct},
        )


# ═══════════════════════════════════════════════════════════════
#  전략 2: EMA Scalp
# ═══════════════════════════════════════════════════════════════

class EMAScalp:
    """
    EMA 크로스오버 스캘핑 전략

    핵심:
    - EMA(9) / EMA(21) 크로스오버 on 1분봉 또는 5분봉
    - 크로스 발생 + 가격이 크로스 방향으로 확인 진행
    - 빠른 진입/청산: 목표 0.3-0.5%, 손절 0.2%
    """

    @staticmethod
    def get_signal(
        close: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        if len(close) < config.ema_slow_period + 3:
            return DaySignal("EMA_SCALP", SignalType.HOLD, 0.0, "데이터 부족")

        ema_fast = DayIndicators.ema(close, config.ema_fast_period)
        ema_slow = DayIndicators.ema(close, config.ema_slow_period)

        curr_fast = float(ema_fast.iloc[-1])
        prev_fast = float(ema_fast.iloc[-2])
        curr_slow = float(ema_slow.iloc[-1])
        prev_slow = float(ema_slow.iloc[-2])
        current_price = float(close.iloc[-1])

        if any(pd.isna([curr_fast, prev_fast, curr_slow, prev_slow])):
            return DaySignal("EMA_SCALP", SignalType.HOLD, 0.0, "EMA 계산 불가")

        # EMA 간격 (%)
        ema_gap_pct = abs(curr_fast - curr_slow) / curr_slow * 100 if curr_slow > 0 else 0

        # ── 골든 크로스: 빠른 EMA가 느린 EMA를 상향 돌파 ──
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            # 가격이 크로스 방향으로 진행 확인
            price_above = current_price > curr_fast
            confidence = 0.6
            if price_above:
                confidence = 0.75

            return DaySignal(
                "EMA_SCALP", SignalType.BUY, confidence,
                f"EMA 골든크로스 | EMA{config.ema_fast_period}={curr_fast:.2f} > "
                f"EMA{config.ema_slow_period}={curr_slow:.2f} (갭: {ema_gap_pct:.2f}%)",
                metadata={
                    "ema_fast": curr_fast, "ema_slow": curr_slow,
                    "target_pct": config.ema_scalp_target_pct,
                    "stop_pct": config.ema_scalp_stop_pct,
                },
            )

        # ── 데드 크로스: 빠른 EMA가 느린 EMA를 하향 돌파 ──
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            price_below = current_price < curr_fast
            confidence = 0.6
            if price_below:
                confidence = 0.75

            return DaySignal(
                "EMA_SCALP", SignalType.SELL, confidence,
                f"EMA 데드크로스 | EMA{config.ema_fast_period}={curr_fast:.2f} < "
                f"EMA{config.ema_slow_period}={curr_slow:.2f} (갭: {ema_gap_pct:.2f}%)",
                metadata={
                    "ema_fast": curr_fast, "ema_slow": curr_slow,
                    "target_pct": config.ema_scalp_target_pct,
                    "stop_pct": config.ema_scalp_stop_pct,
                },
            )

        # ── 추세 유지 중 (크로스 없음) ──
        if curr_fast > curr_slow and ema_gap_pct > 0.08:
            # 갭이 클수록 신뢰도 증가
            trend_conf = min(0.7, 0.5 + ema_gap_pct * 0.1)
            return DaySignal(
                "EMA_SCALP", SignalType.BUY, trend_conf,
                f"EMA 상승 추세 유지 (갭: {ema_gap_pct:.2f}%)",
                metadata={"ema_fast": curr_fast, "ema_slow": curr_slow},
            )
        elif curr_fast < curr_slow and ema_gap_pct > 0.08:
            trend_conf = min(0.7, 0.5 + ema_gap_pct * 0.1)
            return DaySignal(
                "EMA_SCALP", SignalType.SELL, trend_conf,
                f"EMA 하락 추세 유지 (갭: {ema_gap_pct:.2f}%)",
                metadata={"ema_fast": curr_fast, "ema_slow": curr_slow},
            )

        return DaySignal(
            "EMA_SCALP", SignalType.HOLD, 0.3,
            f"EMA 중립 | EMA{config.ema_fast_period}={curr_fast:.2f} "
            f"EMA{config.ema_slow_period}={curr_slow:.2f}",
        )


# ═══════════════════════════════════════════════════════════════
#  전략 3: Volume Spike Breakout
# ═══════════════════════════════════════════════════════════════

class VolumeSpikeBreakout:
    """
    거래량 급증 + 가격 돌파 전략

    핵심:
    - 거래량이 평균의 2배 이상 급증 = 기관/대형 매매 유입 신호
    - 동시에 최근 고점 돌파 (매수) 또는 저점 붕괴 (매도)
    - 거짓 돌파 필터: 돌파 후 N봉 연속 유지 확인
    """

    @staticmethod
    def get_signal(
        close: pd.Series, high: pd.Series, low: pd.Series,
        volume: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        min_bars = max(config.vol_lookback_bars, config.vol_high_lookback) + 3
        if len(close) < min_bars:
            return DaySignal("VOL_BREAKOUT", SignalType.HOLD, 0.0, "데이터 부족")

        # 거래량 급증 체크
        avg_vol = float(volume.iloc[-config.vol_lookback_bars - 1:-1].mean())
        recent_vol = float(volume.iloc[-1])
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        current_price = float(close.iloc[-1])

        # 최근 N봉 고점/저점
        lookback_slice = slice(-config.vol_high_lookback - 1, -1)
        recent_high = float(high.iloc[lookback_slice].max())
        recent_low = float(low.iloc[lookback_slice].min())

        # 거래량 급증 확인
        is_spike = vol_ratio >= config.vol_spike_ratio

        if not is_spike:
            return DaySignal(
                "VOL_BREAKOUT", SignalType.HOLD, 0.3,
                f"거래량 {vol_ratio:.1f}x (기준: {config.vol_spike_ratio}x 미달)",
                metadata={"vol_ratio": vol_ratio},
            )

        # 돌파 후 유지 확인
        confirm_bars = config.vol_breakout_confirm
        confirmed_up = True
        confirmed_down = True

        if len(close) >= confirm_bars + 1:
            for i in range(confirm_bars):
                idx = -(i + 1)
                if close.iloc[idx] <= recent_high:
                    confirmed_up = False
                if close.iloc[idx] >= recent_low:
                    confirmed_down = False
        else:
            confirmed_up = current_price > recent_high
            confirmed_down = current_price < recent_low

        # ── 상향 돌파: 고점 갱신 + 거래량 급증 ──
        if current_price > recent_high and confirmed_up:
            confidence = min(0.9, 0.55 + (vol_ratio - config.vol_spike_ratio) * 0.1)
            breakout_pct = (current_price - recent_high) / recent_high * 100
            return DaySignal(
                "VOL_BREAKOUT", SignalType.BUY, confidence,
                f"상향 돌파! 고점 ${recent_high:.2f} → ${current_price:.2f} "
                f"(+{breakout_pct:.2f}%) | 거래량 {vol_ratio:.1f}x",
                metadata={"vol_ratio": vol_ratio, "breakout_high": recent_high},
            )

        # ── 하향 붕괴: 저점 갱신 + 거래량 급증 ──
        if current_price < recent_low and confirmed_down:
            confidence = min(0.9, 0.55 + (vol_ratio - config.vol_spike_ratio) * 0.1)
            breakdown_pct = (current_price - recent_low) / recent_low * 100
            return DaySignal(
                "VOL_BREAKOUT", SignalType.SELL, confidence,
                f"하향 붕괴! 저점 ${recent_low:.2f} → ${current_price:.2f} "
                f"({breakdown_pct:.2f}%) | 거래량 {vol_ratio:.1f}x",
                metadata={"vol_ratio": vol_ratio, "breakdown_low": recent_low},
            )

        # 거래량 급증했지만 돌파 미확인
        return DaySignal(
            "VOL_BREAKOUT", SignalType.HOLD, 0.45,
            f"거래량 {vol_ratio:.1f}x 급증 | 돌파 대기 "
            f"(고점: ${recent_high:.2f}, 저점: ${recent_low:.2f})",
            metadata={"vol_ratio": vol_ratio},
        )


# ═══════════════════════════════════════════════════════════════
#  전략 4: RSI + MACD Combo
# ═══════════════════════════════════════════════════════════════

class RSIMACDCombo:
    """
    RSI + MACD 복합 전략

    핵심:
    - RSI로 과매수/과매도 구간 탐지
    - MACD 히스토그램 방향으로 모멘텀 확인
    - 두 지표 동시 일치 시에만 신호 (거짓 신호 감소)
    """

    @staticmethod
    def get_signal(
        close: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        min_bars = max(config.rsi_period, config.macd_slow) + 10
        if len(close) < min_bars:
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "데이터 부족")

        # RSI 계산
        rsi_series = DayIndicators.rsi(close, config.rsi_period)
        current_rsi = float(rsi_series.iloc[-1])

        if pd.isna(current_rsi):
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "RSI 계산 불가")

        # MACD 계산
        macd_line, signal_line, histogram = DayIndicators.macd(
            close, config.macd_fast, config.macd_slow, config.macd_signal
        )
        current_hist = float(histogram.iloc[-1])
        prev_hist = float(histogram.iloc[-2])
        current_macd = float(macd_line.iloc[-1])
        current_sig = float(signal_line.iloc[-1])

        if any(pd.isna([current_hist, prev_hist])):
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "MACD 계산 불가")

        # MACD 모멘텀 방향
        macd_bullish = current_hist > prev_hist  # 히스토그램 증가
        macd_bearish = current_hist < prev_hist  # 히스토그램 감소
        macd_cross_up = current_macd > current_sig and macd_line.iloc[-2] <= signal_line.iloc[-2]
        macd_cross_down = current_macd < current_sig and macd_line.iloc[-2] >= signal_line.iloc[-2]

        # ── 매수: RSI 과매도 + MACD 상승 ──
        if current_rsi <= config.rsi_oversold and (macd_bullish or macd_cross_up):
            confidence = 0.7
            if macd_cross_up:
                confidence = 0.85
            if current_rsi <= 20:
                confidence = min(0.95, confidence + 0.1)

            return DaySignal(
                "RSI_MACD", SignalType.BUY, confidence,
                f"RSI {current_rsi:.1f} 과매도 + MACD {'크로스↑' if macd_cross_up else '반등'}",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        # ── 매도: RSI 과매수 + MACD 하락 ──
        if current_rsi >= config.rsi_overbought and (macd_bearish or macd_cross_down):
            confidence = 0.7
            if macd_cross_down:
                confidence = 0.85
            if current_rsi >= 80:
                confidence = min(0.95, confidence + 0.1)

            return DaySignal(
                "RSI_MACD", SignalType.SELL, confidence,
                f"RSI {current_rsi:.1f} 과매수 + MACD {'크로스↓' if macd_cross_down else '하락'}",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        # ── RSI만 단독 신호 ──
        if current_rsi <= config.rsi_oversold:
            return DaySignal(
                "RSI_MACD", SignalType.BUY, 0.6,
                f"RSI {current_rsi:.1f} 과매도 (MACD 미확인)",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )
        if current_rsi >= config.rsi_overbought:
            return DaySignal(
                "RSI_MACD", SignalType.SELL, 0.6,
                f"RSI {current_rsi:.1f} 과매수 (MACD 미확인)",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        return DaySignal(
            "RSI_MACD", SignalType.HOLD, 0.3,
            f"RSI {current_rsi:.1f} | MACD hist: {current_hist:+.3f}",
            metadata={"rsi": current_rsi, "macd_hist": current_hist},
        )


# ═══════════════════════════════════════════════════════════════
#  데이 트레이딩 앙상블 엔진
# ═══════════════════════════════════════════════════════════════

class DayStrategyEnsemble:
    """
    4개 분봉 전략의 합의 기반 매매 결정

    작동 방식:
    1. 각 전략이 BUY/SELL/HOLD + 신뢰도 반환
    2. 가중 합산: BUY=+1, SELL=-1, HOLD=0 × 신뢰도 × 가중치
    3. 임계값 초과 + 최소 N개 전략 동의 → 매매
    4. ATR로 손절/익절 자동 설정
    """

    def __init__(self, config: DayStrategyConfig = None):
        self.config = config or DayStrategyConfig()

    def analyze(
        self,
        symbol: str,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        volume: pd.Series,
        is_morning: bool = False,
    ) -> DayEnsembleDecision:
        """모든 전략 실행 → 앙상블 결정"""
        cfg = self.config
        signals: list[DaySignal] = []

        # ── 전략 1: VWAP Bounce ──
        signals.append(VWAPBounce.get_signal(close, high, low, volume, cfg, is_morning))

        # ── 전략 2: EMA Scalp ──
        signals.append(EMAScalp.get_signal(close, cfg))

        # ── 전략 3: Volume Breakout ──
        signals.append(VolumeSpikeBreakout.get_signal(close, high, low, volume, cfg))

        # ── 전략 4: RSI + MACD ──
        signals.append(RSIMACDCombo.get_signal(close, cfg))

        # ═══ 앙상블 점수 계산 ═══
        weight_map = {
            "VWAP_BOUNCE": cfg.weight_vwap,
            "EMA_SCALP": cfg.weight_ema,
            "VOL_BREAKOUT": cfg.weight_volume,
            "RSI_MACD": cfg.weight_rsi_macd,
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
                f"합의 BUY ({buy_count}개 전략, 점수: {consensus_score:+.2f})"
            )
        elif (consensus_score <= cfg.ensemble_sell_threshold and
              sell_count >= cfg.min_strategies_agree):
            final_signal = SignalType.SELL
            reason_parts.append(
                f"합의 SELL ({sell_count}개 전략, 점수: {consensus_score:+.2f})"
            )
        else:
            reason_parts.append(
                f"합의 미달 (BUY:{buy_count} SELL:{sell_count}, "
                f"점수: {consensus_score:+.2f}, 기준: ±{cfg.ensemble_buy_threshold})"
            )

        # ═══ ATR 손절/익절 ═══
        current_price = float(close.iloc[-1])
        atr_series = DayIndicators.atr(high, low, close, cfg.atr_period)
        atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0

        if atr_value > 0:
            stop_loss = round(current_price - atr_value * cfg.atr_stop_multiplier, 2)
            take_profit = round(current_price + atr_value * cfg.atr_profit_multiplier, 2)
            risk_pct = round(atr_value * cfg.atr_stop_multiplier / current_price * 100, 2)
            reward_pct = round(atr_value * cfg.atr_profit_multiplier / current_price * 100, 2)
        else:
            stop_loss = round(current_price * 0.998, 2)  # 기본 0.2% 손절
            take_profit = round(current_price * 1.004, 2)  # 기본 0.4% 익절
            risk_pct = 0.2
            reward_pct = 0.4

        reason_parts.append(
            f"ATR=${atr_value:.2f} | SL=${stop_loss} (-{risk_pct}%) | "
            f"TP=${take_profit} (+{reward_pct}%)"
        )

        return DayEnsembleDecision(
            symbol=symbol,
            final_signal=final_signal,
            consensus_score=round(consensus_score, 3),
            individual_signals=signals,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            reason=" | ".join(reason_parts),
        )


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    """가상 5분봉 데이터로 앙상블 테스트"""
    np.random.seed(42)

    # 120봉 (10시간 = 하루 장중) 가상 5분봉 데이터
    n = 120
    base = 180.0  # NVDA 가격대

    # 오전 상승 → 점심 횡보 → 오후 하락 패턴
    trend = np.concatenate([
        np.linspace(0, 5, 40),      # 오전 상승
        np.linspace(5, 4.5, 40),    # 점심 횡보
        np.linspace(4.5, 2, 40),    # 오후 하락
    ])
    noise = np.random.randn(n) * 0.8

    close_arr = base + trend + noise
    high_arr = close_arr + np.abs(np.random.randn(n)) * 0.5
    low_arr = close_arr - np.abs(np.random.randn(n)) * 0.5
    vol_arr = np.random.randint(100000, 500000, n).astype(float)
    # 거래량 급증 구간
    vol_arr[35:42] *= 3.0   # 오전 돌파
    vol_arr[100:110] *= 2.5  # 오후 급락

    idx = pd.date_range("2026-03-23 09:30", periods=n, freq="5min")
    close = pd.Series(close_arr, index=idx)
    high = pd.Series(high_arr, index=idx)
    low = pd.Series(low_arr, index=idx)
    volume = pd.Series(vol_arr, index=idx)

    print("=" * 70)
    print("  🏎️  Day Trading Strategies 데모")
    print("=" * 70)

    config = DayStrategyConfig()
    ensemble = DayStrategyEnsemble(config)

    # 여러 시점에서 분석
    test_points = [
        (40, "오전 돌파 구간", True),
        (70, "점심 횡보 구간", False),
        (105, "오후 급락 구간", False),
    ]

    for end_idx, label, is_morning in test_points:
        c = close.iloc[:end_idx]
        h = high.iloc[:end_idx]
        l = low.iloc[:end_idx]
        v = volume.iloc[:end_idx]

        decision = ensemble.analyze("NVDA", c, h, l, v, is_morning)

        print(f"\n  📊 [{label}] {idx[end_idx-1].strftime('%H:%M')}")
        print(f"  {decision}")
        print(f"  개별 전략:")
        for sig in decision.individual_signals:
            print(
                f"    {sig.strategy_name:15s} | {sig.signal.value} | "
                f"신뢰도: {sig.confidence:.0%} | {sig.reason}"
            )

    print("\n" + "=" * 70)


if __name__ == "__main__":
    demo()
