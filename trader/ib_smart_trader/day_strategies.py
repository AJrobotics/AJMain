"""
═══════════════════════════════════════════════════════════════════
  Day Trading Strategies - Minute-Bar Based Day Trading Strategy Engine

  Strategies (4-strategy Ensemble):
    1. VWAP Bounce     (30%) - VWAP support/resistance bounce trading
    2. EMA Scalp       (25%) - EMA(9)/EMA(21) crossover scalping
    3. Volume Breakout (25%) - Volume spike + price breakout
    4. RSI+MACD Combo  (20%) - Overbought/oversold + MACD confirmation

  Timeframes: 1-min bars, 5-min bars
  Targets: Highly liquid large-caps + premarket gap stocks
═══════════════════════════════════════════════════════════════════
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  Signal Types (compatible with existing system)
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class DaySignal:
    """Individual strategy minute-bar signal"""
    strategy_name: str
    signal: SignalType
    confidence: float      # 0.0 ~ 1.0
    reason: str
    timeframe: str = "5min"  # "1min" or "5min"
    metadata: dict = field(default_factory=dict)


@dataclass
class DayEnsembleDecision:
    """Day trading ensemble final decision"""
    symbol: str
    final_signal: SignalType
    consensus_score: float       # -1.0 ~ +1.0
    individual_signals: list
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    position_size_pct: float = 0.0  # Position size as % of capital
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self):
        sigs = ", ".join(
            f"{s.strategy_name}={s.signal.name}({s.confidence:.0%})"
            for s in self.individual_signals
        )
        sl = f"SL=${self.stop_loss_price:.2f}" if self.stop_loss_price > 0 else "SL=None"
        tp = f"TP=${self.take_profit_price:.2f}" if self.take_profit_price > 0 else "TP=None"
        return (
            f"{self.final_signal.value} {self.symbol} | "
            f"Consensus: {self.consensus_score:+.2f} | "
            f"{sl} | {tp} | "
            f"Strategies: [{sigs}]"
        )


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class DayStrategyConfig:
    """Day trading strategy configuration"""

    # ── VWAP Bounce ──
    vwap_bounce_zone_pct: float = 0.3      # Bounce detection when price within VWAP +/- 0.3% (prev 0.15)
    vwap_bounce_confirm_bars: int = 1      # 1 bar bounce confirmation (prev 2)
    vwap_morning_boost: bool = True        # Weight boost during 9:30~10:30 ET
    vwap_morning_boost_pct: float = 0.15   # Morning extra confidence

    # ── EMA Scalp ──
    ema_fast_period: int = 8               # More sensitive EMA (prev 9)
    ema_slow_period: int = 18              # Faster cross detection (prev 21)
    ema_confirm_ticks: float = 0.05        # Post-cross confirmation distance ($)
    ema_scalp_target_pct: float = 0.5      # Scalp target 0.5% (prev 0.4)
    ema_scalp_stop_pct: float = 0.25       # Scalp stop-loss 0.25% (prev 0.2)

    # ── Volume Breakout ──
    vol_spike_ratio: float = 1.3           # 1.3x above average (prev 2.0)
    vol_lookback_bars: int = 15            # Average volume lookback period (prev 20)
    vol_breakout_confirm: int = 1          # 1 bar hold after breakout (prev 2)
    vol_high_lookback: int = 8             # High/low reference period (prev 10)

    # ── RSI + MACD ──
    rsi_period: int = 10                   # More sensitive RSI (prev 14)
    rsi_oversold: float = 35.0             # Relaxed oversold threshold (prev 30)
    rsi_overbought: float = 65.0           # Relaxed overbought threshold (prev 70)
    macd_fast: int = 8                     # Faster MACD (prev 12)
    macd_slow: int = 21                    # (prev 26)
    macd_signal: int = 7                   # (prev 9)

    # ── Ensemble ──
    ensemble_buy_threshold: float = 0.20   # Lowered buy threshold (prev 0.35)
    ensemble_sell_threshold: float = -0.20  # Lowered sell threshold (prev -0.35)
    min_strategies_agree: int = 1          # Min 1 strategy agreement to trade (prev 2)

    # ── Weights ──
    weight_vwap: float = 0.30
    weight_ema: float = 0.30              # Increased EMA weight (prev 0.25)
    weight_volume: float = 0.20           # (prev 0.25)
    weight_rsi_macd: float = 0.20

    # ── Stop-Loss / Take-Profit (ATR-based) ──
    atr_period: int = 10                   # More sensitive ATR (prev 14)
    atr_stop_multiplier: float = 1.2       # Tighter stop-loss (prev 1.5)
    atr_profit_multiplier: float = 2.0     # Target R:R = 1.67


# ═══════════════════════════════════════════════════════════════
#  Technical Indicator Calculations
# ═══════════════════════════════════════════════════════════════

class DayIndicators:
    """Minute-bar technical indicators"""

    @staticmethod
    def vwap(close: pd.Series, high: pd.Series, low: pd.Series,
             volume: pd.Series) -> pd.Series:
        """
        VWAP (Volume Weighted Average Price)
        = Cumulative(TP x Volume) / Cumulative(Volume)
        TP = (High + Low + Close) / 3
        """
        typical_price = (high + low + close) / 3
        cum_tp_vol = (typical_price * volume).cumsum()
        cum_vol = volume.cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    @staticmethod
    def ema(prices: pd.Series, period: int) -> pd.Series:
        """Exponential Moving Average"""
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
#  Strategy 1: VWAP Bounce
# ═══════════════════════════════════════════════════════════════

class VWAPBounce:
    """
    VWAP Support/Resistance Bounce Strategy

    Core logic:
    - VWAP is the average price of institutional trades for the day -> strong support/resistance
    - Price touches VWAP from below and bounces up -> Buy (institutional buying zone)
    - Price touches VWAP from above and drops -> Sell (institutional selling zone)
    - Higher confidence weight during morning session (9:30-10:30)
    """

    @staticmethod
    def get_signal(
        close: pd.Series, high: pd.Series, low: pd.Series,
        volume: pd.Series, config: DayStrategyConfig,
        is_morning: bool = False,
    ) -> DaySignal:
        if len(close) < 20:
            return DaySignal("VWAP_BOUNCE", SignalType.HOLD, 0.0, "Insufficient data")

        vwap = DayIndicators.vwap(close, high, low, volume)
        current_price = float(close.iloc[-1])
        current_vwap = float(vwap.iloc[-1])

        if pd.isna(current_vwap) or current_vwap <= 0:
            return DaySignal("VWAP_BOUNCE", SignalType.HOLD, 0.0, "Unable to calculate VWAP")

        # Distance from VWAP (%)
        dist_pct = (current_price - current_vwap) / current_vwap * 100
        bounce_zone = config.vwap_bounce_zone_pct

        # Check recent bar direction (bounce detection)
        recent_trend = 0.0
        if len(close) >= config.vwap_bounce_confirm_bars + 1:
            for i in range(1, config.vwap_bounce_confirm_bars + 1):
                recent_trend += close.iloc[-i] - close.iloc[-i-1]

        confidence = 0.5

        # ── Buy: Price bouncing up from below/near VWAP ──
        if -bounce_zone * 3 <= dist_pct <= bounce_zone and recent_trend > 0:
            confidence = 0.65
            # Higher confidence the closer to VWAP
            proximity_bonus = max(0, (bounce_zone - abs(dist_pct)) / bounce_zone * 0.15)
            confidence += proximity_bonus

            if is_morning and config.vwap_morning_boost:
                confidence += config.vwap_morning_boost_pct

            confidence = min(0.95, confidence)
            return DaySignal(
                "VWAP_BOUNCE", SignalType.BUY, confidence,
                f"VWAP ${current_vwap:.2f} support bounce (dist: {dist_pct:+.2f}%)",
                metadata={"vwap": current_vwap, "dist_pct": dist_pct},
            )

        # ── Sell: Price dropping from above/near VWAP ──
        if -bounce_zone <= dist_pct <= bounce_zone * 3 and recent_trend < 0:
            confidence = 0.65
            proximity_bonus = max(0, (bounce_zone - abs(dist_pct)) / bounce_zone * 0.15)
            confidence += proximity_bonus

            if is_morning and config.vwap_morning_boost:
                confidence += config.vwap_morning_boost_pct

            confidence = min(0.95, confidence)
            return DaySignal(
                "VWAP_BOUNCE", SignalType.SELL, confidence,
                f"VWAP ${current_vwap:.2f} resistance drop (dist: {dist_pct:+.2f}%)",
                metadata={"vwap": current_vwap, "dist_pct": dist_pct},
            )

        return DaySignal(
            "VWAP_BOUNCE", SignalType.HOLD, 0.3,
            f"VWAP ${current_vwap:.2f} | dist: {dist_pct:+.2f}% (no bounce detected)",
            metadata={"vwap": current_vwap, "dist_pct": dist_pct},
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 2: EMA Scalp
# ═══════════════════════════════════════════════════════════════

class EMAScalp:
    """
    EMA Crossover Scalping Strategy

    Core logic:
    - EMA(9) / EMA(21) crossover on 1-min or 5-min bars
    - Cross occurs + price confirms in cross direction
    - Quick entry/exit: target 0.3-0.5%, stop-loss 0.2%
    """

    @staticmethod
    def get_signal(
        close: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        if len(close) < config.ema_slow_period + 3:
            return DaySignal("EMA_SCALP", SignalType.HOLD, 0.0, "Insufficient data")

        ema_fast = DayIndicators.ema(close, config.ema_fast_period)
        ema_slow = DayIndicators.ema(close, config.ema_slow_period)

        curr_fast = float(ema_fast.iloc[-1])
        prev_fast = float(ema_fast.iloc[-2])
        curr_slow = float(ema_slow.iloc[-1])
        prev_slow = float(ema_slow.iloc[-2])
        current_price = float(close.iloc[-1])

        if any(pd.isna([curr_fast, prev_fast, curr_slow, prev_slow])):
            return DaySignal("EMA_SCALP", SignalType.HOLD, 0.0, "Unable to calculate EMA")

        # EMA gap (%)
        ema_gap_pct = abs(curr_fast - curr_slow) / curr_slow * 100 if curr_slow > 0 else 0

        # ── Golden Cross: Fast EMA crosses above slow EMA ──
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            # Confirm price moving in cross direction
            price_above = current_price > curr_fast
            confidence = 0.6
            if price_above:
                confidence = 0.75

            return DaySignal(
                "EMA_SCALP", SignalType.BUY, confidence,
                f"EMA Golden Cross | EMA{config.ema_fast_period}={curr_fast:.2f} > "
                f"EMA{config.ema_slow_period}={curr_slow:.2f} (gap: {ema_gap_pct:.2f}%)",
                metadata={
                    "ema_fast": curr_fast, "ema_slow": curr_slow,
                    "target_pct": config.ema_scalp_target_pct,
                    "stop_pct": config.ema_scalp_stop_pct,
                },
            )

        # ── Death Cross: Fast EMA crosses below slow EMA ──
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            price_below = current_price < curr_fast
            confidence = 0.6
            if price_below:
                confidence = 0.75

            return DaySignal(
                "EMA_SCALP", SignalType.SELL, confidence,
                f"EMA Death Cross | EMA{config.ema_fast_period}={curr_fast:.2f} < "
                f"EMA{config.ema_slow_period}={curr_slow:.2f} (gap: {ema_gap_pct:.2f}%)",
                metadata={
                    "ema_fast": curr_fast, "ema_slow": curr_slow,
                    "target_pct": config.ema_scalp_target_pct,
                    "stop_pct": config.ema_scalp_stop_pct,
                },
            )

        # ── Trend continuation (no cross) ──
        if curr_fast > curr_slow and ema_gap_pct > 0.08:
            # Higher confidence with larger gap
            trend_conf = min(0.7, 0.5 + ema_gap_pct * 0.1)
            return DaySignal(
                "EMA_SCALP", SignalType.BUY, trend_conf,
                f"EMA uptrend holding (gap: {ema_gap_pct:.2f}%)",
                metadata={"ema_fast": curr_fast, "ema_slow": curr_slow},
            )
        elif curr_fast < curr_slow and ema_gap_pct > 0.08:
            trend_conf = min(0.7, 0.5 + ema_gap_pct * 0.1)
            return DaySignal(
                "EMA_SCALP", SignalType.SELL, trend_conf,
                f"EMA downtrend holding (gap: {ema_gap_pct:.2f}%)",
                metadata={"ema_fast": curr_fast, "ema_slow": curr_slow},
            )

        return DaySignal(
            "EMA_SCALP", SignalType.HOLD, 0.3,
            f"EMA neutral | EMA{config.ema_fast_period}={curr_fast:.2f} "
            f"EMA{config.ema_slow_period}={curr_slow:.2f}",
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 3: Volume Spike Breakout
# ═══════════════════════════════════════════════════════════════

class VolumeSpikeBreakout:
    """
    Volume Spike + Price Breakout Strategy

    Core logic:
    - Volume surging to 2x+ above average = institutional/large order inflow signal
    - Simultaneously breaking recent highs (buy) or lows (sell)
    - False breakout filter: confirm N consecutive bars held after breakout
    """

    @staticmethod
    def get_signal(
        close: pd.Series, high: pd.Series, low: pd.Series,
        volume: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        min_bars = max(config.vol_lookback_bars, config.vol_high_lookback) + 3
        if len(close) < min_bars:
            return DaySignal("VOL_BREAKOUT", SignalType.HOLD, 0.0, "Insufficient data")

        # Volume spike check
        avg_vol = float(volume.iloc[-config.vol_lookback_bars - 1:-1].mean())
        recent_vol = float(volume.iloc[-1])
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        current_price = float(close.iloc[-1])

        # Recent N-bar high/low
        lookback_slice = slice(-config.vol_high_lookback - 1, -1)
        recent_high = float(high.iloc[lookback_slice].max())
        recent_low = float(low.iloc[lookback_slice].min())

        # Volume spike confirmation
        is_spike = vol_ratio >= config.vol_spike_ratio

        if not is_spike:
            return DaySignal(
                "VOL_BREAKOUT", SignalType.HOLD, 0.3,
                f"Volume {vol_ratio:.1f}x (threshold: {config.vol_spike_ratio}x not met)",
                metadata={"vol_ratio": vol_ratio},
            )

        # Post-breakout hold confirmation
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

        # ── Upward breakout: New high + volume spike ──
        if current_price > recent_high and confirmed_up:
            confidence = min(0.9, 0.55 + (vol_ratio - config.vol_spike_ratio) * 0.1)
            breakout_pct = (current_price - recent_high) / recent_high * 100
            return DaySignal(
                "VOL_BREAKOUT", SignalType.BUY, confidence,
                f"Upward breakout! High ${recent_high:.2f} -> ${current_price:.2f} "
                f"(+{breakout_pct:.2f}%) | Volume {vol_ratio:.1f}x",
                metadata={"vol_ratio": vol_ratio, "breakout_high": recent_high},
            )

        # ── Downward breakdown: New low + volume spike ──
        if current_price < recent_low and confirmed_down:
            confidence = min(0.9, 0.55 + (vol_ratio - config.vol_spike_ratio) * 0.1)
            breakdown_pct = (current_price - recent_low) / recent_low * 100
            return DaySignal(
                "VOL_BREAKOUT", SignalType.SELL, confidence,
                f"Downward breakdown! Low ${recent_low:.2f} -> ${current_price:.2f} "
                f"({breakdown_pct:.2f}%) | Volume {vol_ratio:.1f}x",
                metadata={"vol_ratio": vol_ratio, "breakdown_low": recent_low},
            )

        # Volume spiked but no breakout confirmed
        return DaySignal(
            "VOL_BREAKOUT", SignalType.HOLD, 0.45,
            f"Volume {vol_ratio:.1f}x spike | Awaiting breakout "
            f"(high: ${recent_high:.2f}, low: ${recent_low:.2f})",
            metadata={"vol_ratio": vol_ratio},
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 4: RSI + MACD Combo
# ═══════════════════════════════════════════════════════════════

class RSIMACDCombo:
    """
    RSI + MACD Combined Strategy

    Core logic:
    - RSI detects overbought/oversold zones
    - MACD histogram direction confirms momentum
    - Signal only when both indicators agree (reduces false signals)
    """

    @staticmethod
    def get_signal(
        close: pd.Series, config: DayStrategyConfig,
    ) -> DaySignal:
        min_bars = max(config.rsi_period, config.macd_slow) + 10
        if len(close) < min_bars:
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "Insufficient data")

        # RSI calculation
        rsi_series = DayIndicators.rsi(close, config.rsi_period)
        current_rsi = float(rsi_series.iloc[-1])

        if pd.isna(current_rsi):
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "Unable to calculate RSI")

        # MACD calculation
        macd_line, signal_line, histogram = DayIndicators.macd(
            close, config.macd_fast, config.macd_slow, config.macd_signal
        )
        current_hist = float(histogram.iloc[-1])
        prev_hist = float(histogram.iloc[-2])
        current_macd = float(macd_line.iloc[-1])
        current_sig = float(signal_line.iloc[-1])

        if any(pd.isna([current_hist, prev_hist])):
            return DaySignal("RSI_MACD", SignalType.HOLD, 0.0, "Unable to calculate MACD")

        # MACD momentum direction
        macd_bullish = current_hist > prev_hist  # Histogram increasing
        macd_bearish = current_hist < prev_hist  # Histogram decreasing
        macd_cross_up = current_macd > current_sig and macd_line.iloc[-2] <= signal_line.iloc[-2]
        macd_cross_down = current_macd < current_sig and macd_line.iloc[-2] >= signal_line.iloc[-2]

        # ── Buy: RSI oversold + MACD rising ──
        if current_rsi <= config.rsi_oversold and (macd_bullish or macd_cross_up):
            confidence = 0.7
            if macd_cross_up:
                confidence = 0.85
            if current_rsi <= 20:
                confidence = min(0.95, confidence + 0.1)

            return DaySignal(
                "RSI_MACD", SignalType.BUY, confidence,
                f"RSI {current_rsi:.1f} oversold + MACD {'crossover up' if macd_cross_up else 'bounce'}",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        # ── Sell: RSI overbought + MACD falling ──
        if current_rsi >= config.rsi_overbought and (macd_bearish or macd_cross_down):
            confidence = 0.7
            if macd_cross_down:
                confidence = 0.85
            if current_rsi >= 80:
                confidence = min(0.95, confidence + 0.1)

            return DaySignal(
                "RSI_MACD", SignalType.SELL, confidence,
                f"RSI {current_rsi:.1f} overbought + MACD {'crossover down' if macd_cross_down else 'declining'}",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        # ── RSI standalone signal ──
        if current_rsi <= config.rsi_oversold:
            return DaySignal(
                "RSI_MACD", SignalType.BUY, 0.6,
                f"RSI {current_rsi:.1f} oversold (MACD unconfirmed)",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )
        if current_rsi >= config.rsi_overbought:
            return DaySignal(
                "RSI_MACD", SignalType.SELL, 0.6,
                f"RSI {current_rsi:.1f} overbought (MACD unconfirmed)",
                metadata={"rsi": current_rsi, "macd_hist": current_hist},
            )

        return DaySignal(
            "RSI_MACD", SignalType.HOLD, 0.3,
            f"RSI {current_rsi:.1f} | MACD hist: {current_hist:+.3f}",
            metadata={"rsi": current_rsi, "macd_hist": current_hist},
        )


# ═══════════════════════════════════════════════════════════════
#  Day Trading Ensemble Engine
# ═══════════════════════════════════════════════════════════════

class DayStrategyEnsemble:
    """
    Consensus-based trading decision from 4 minute-bar strategies

    How it works:
    1. Each strategy returns BUY/SELL/HOLD + confidence
    2. Weighted sum: BUY=+1, SELL=-1, HOLD=0 x confidence x weight
    3. Threshold exceeded + min N strategies agree -> trade
    4. ATR-based automatic stop-loss/take-profit
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
        """Run all strategies -> ensemble decision"""
        cfg = self.config
        signals: list[DaySignal] = []

        # ── Strategy 1: VWAP Bounce ──
        signals.append(VWAPBounce.get_signal(close, high, low, volume, cfg, is_morning))

        # ── Strategy 2: EMA Scalp ──
        signals.append(EMAScalp.get_signal(close, cfg))

        # ── Strategy 3: Volume Breakout ──
        signals.append(VolumeSpikeBreakout.get_signal(close, high, low, volume, cfg))

        # ── Strategy 4: RSI + MACD ──
        signals.append(RSIMACDCombo.get_signal(close, cfg))

        # === Ensemble Score Calculation ===
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

        # Normalize (-1 ~ +1)
        max_possible = sum(weight_map.values())
        if max_possible > 0:
            consensus_score = consensus_score / max_possible

        # === Final Decision ===
        final_signal = SignalType.HOLD
        reason_parts = []

        if (consensus_score >= cfg.ensemble_buy_threshold and
            buy_count >= cfg.min_strategies_agree):
            final_signal = SignalType.BUY
            reason_parts.append(
                f"Consensus BUY ({buy_count} strategies, score: {consensus_score:+.2f})"
            )
        elif (consensus_score <= cfg.ensemble_sell_threshold and
              sell_count >= cfg.min_strategies_agree):
            final_signal = SignalType.SELL
            reason_parts.append(
                f"Consensus SELL ({sell_count} strategies, score: {consensus_score:+.2f})"
            )
        else:
            reason_parts.append(
                f"Consensus not met (BUY:{buy_count} SELL:{sell_count}, "
                f"score: {consensus_score:+.2f}, threshold: +/-{cfg.ensemble_buy_threshold})"
            )

        # === ATR Stop-Loss / Take-Profit ===
        current_price = float(close.iloc[-1])
        atr_series = DayIndicators.atr(high, low, close, cfg.atr_period)
        atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0

        if atr_value > 0:
            stop_loss = round(current_price - atr_value * cfg.atr_stop_multiplier, 2)
            take_profit = round(current_price + atr_value * cfg.atr_profit_multiplier, 2)
            risk_pct = round(atr_value * cfg.atr_stop_multiplier / current_price * 100, 2)
            reward_pct = round(atr_value * cfg.atr_profit_multiplier / current_price * 100, 2)
        else:
            stop_loss = round(current_price * 0.998, 2)  # Default 0.2% stop-loss
            take_profit = round(current_price * 1.004, 2)  # Default 0.4% take-profit
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
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    """Ensemble test with simulated 5-min bar data"""
    np.random.seed(42)

    # 120 bars (10 hours = full trading day) simulated 5-min bar data
    n = 120
    base = 180.0  # NVDA price range

    # Morning rally -> midday consolidation -> afternoon decline pattern
    trend = np.concatenate([
        np.linspace(0, 5, 40),      # Morning rally
        np.linspace(5, 4.5, 40),    # Midday consolidation
        np.linspace(4.5, 2, 40),    # Afternoon decline
    ])
    noise = np.random.randn(n) * 0.8

    close_arr = base + trend + noise
    high_arr = close_arr + np.abs(np.random.randn(n)) * 0.5
    low_arr = close_arr - np.abs(np.random.randn(n)) * 0.5
    vol_arr = np.random.randint(100000, 500000, n).astype(float)
    # Volume spike zones
    vol_arr[35:42] *= 3.0   # Morning breakout
    vol_arr[100:110] *= 2.5  # Afternoon sell-off

    idx = pd.date_range("2026-03-23 09:30", periods=n, freq="5min")
    close = pd.Series(close_arr, index=idx)
    high = pd.Series(high_arr, index=idx)
    low = pd.Series(low_arr, index=idx)
    volume = pd.Series(vol_arr, index=idx)

    print("=" * 70)
    print("  🏎️  Day Trading Strategies Demo")
    print("=" * 70)

    config = DayStrategyConfig()
    ensemble = DayStrategyEnsemble(config)

    # Analyze at multiple time points
    test_points = [
        (40, "Morning breakout zone", True),
        (70, "Midday consolidation zone", False),
        (105, "Afternoon sell-off zone", False),
    ]

    for end_idx, label, is_morning in test_points:
        c = close.iloc[:end_idx]
        h = high.iloc[:end_idx]
        l = low.iloc[:end_idx]
        v = volume.iloc[:end_idx]

        decision = ensemble.analyze("NVDA", c, h, l, v, is_morning)

        print(f"\n  📊 [{label}] {idx[end_idx-1].strftime('%H:%M')}")
        print(f"  {decision}")
        print(f"  Individual strategies:")
        for sig in decision.individual_signals:
            print(
                f"    {sig.strategy_name:15s} | {sig.signal.value} | "
                f"Confidence: {sig.confidence:.0%} | {sig.reason}"
            )

    print("\n" + "=" * 70)


if __name__ == "__main__":
    demo()
