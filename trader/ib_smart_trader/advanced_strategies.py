"""
═══════════════════════════════════════════════════════════════════
  Advanced Strategies Module - Advanced Strategy Engine

  Additional Strategies:
    3. ATR Dynamic Stop Loss/Take Profit - Volatility-based risk management
    4. Adaptive RSI - Overbought/Oversold signals based on trend context
    5. Multi-Strategy Ensemble - Trading based on multiple strategy consensus

  Integration with existing strategies:
    1. MA Crossover (smart_trader.py)
    2. % Change (smart_trader.py)
═══════════════════════════════════════════════════════════════════
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  Signal Types (compatible with smart_trader.py)
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class StrategySignal:
    """Individual strategy signal"""
    strategy_name: str
    signal: SignalType
    confidence: float      # 0.0 ~ 1.0 confidence
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class EnsembleDecision:
    """Ensemble final decision"""
    symbol: str
    final_signal: SignalType
    consensus_score: float       # -1.0 (strong SELL) ~ +1.0 (strong BUY)
    individual_signals: list     # Each strategy's signal
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
class AdvancedConfig:
    """Advanced strategy configuration"""

    # ── ATR Dynamic Stop Loss ──
    atr_period: int = 14              # ATR calculation period
    atr_stop_multiplier: float = 2.0  # Stop loss = current price - (ATR x multiplier)
    atr_profit_multiplier: float = 3.0  # Take profit = current price + (ATR x multiplier)
    trailing_stop_enabled: bool = True  # Enable trailing stop loss
    trailing_atr_multiplier: float = 1.5  # Trailing multiplier

    # ── Adaptive RSI ──
    rsi_period: int = 14
    # RSI thresholds during uptrend (more aggressive)
    rsi_bull_oversold: float = 40.0     # Uptrend oversold (normal: 30)
    rsi_bull_overbought: float = 80.0   # Uptrend overbought (normal: 70)
    # RSI thresholds during downtrend (more conservative)
    rsi_bear_oversold: float = 20.0     # Downtrend oversold
    rsi_bear_overbought: float = 60.0   # Downtrend overbought
    # Divergence detection
    rsi_divergence_lookback: int = 10   # Divergence lookback period

    # ── Ensemble ──
    ensemble_buy_threshold: float = 0.4   # BUY if score >= this value
    ensemble_sell_threshold: float = -0.4  # SELL if score <= this value
    min_strategies_agree: int = 3          # Minimum N strategies must agree

    # ── Strategy Weights (sum = 1.0) ──
    weight_ma_crossover: float = 0.25
    weight_pct_change: float = 0.15
    weight_adaptive_rsi: float = 0.25
    weight_atr_trend: float = 0.15
    weight_volume_confirm: float = 0.20


# ═══════════════════════════════════════════════════════════════
#  Strategy 3: ATR Dynamic Stop Loss/Take Profit
# ═══════════════════════════════════════════════════════════════

class ATRStopLoss:
    """
    ATR (Average True Range) based dynamic risk management

    Key points:
    - High volatility → Wide stop loss (prevent unnecessary early stop outs)
    - Low volatility → Tight stop loss (cut small losses quickly)
    - Trailing stop: Stop loss follows price upward as profit increases
    """

    @staticmethod
    def calculate_atr(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """
        Calculate True Range and ATR
        TR = max(H-L, |H-prevC|, |L-prevC|)
        ATR = N-day moving average of TR
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
        Calculate stop loss/take profit levels from current price and ATR

        Returns:
            {
                "stop_loss": Stop loss price,
                "take_profit": Take profit price,
                "trailing_stop": Trailing stop distance,
                "risk_reward_ratio": Risk/reward ratio,
            }
        """
        if position_side == "LONG":
            stop_loss = current_price - (atr_value * config.atr_stop_multiplier)
            take_profit = current_price + (atr_value * config.atr_profit_multiplier)
            trailing_distance = atr_value * config.trailing_atr_multiplier
        else:  # SHORT (for future expansion)
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
        ATR Trend Confirmation — Volatility expansion/contraction detection
        - ATR increasing + price rising = Strong bullish momentum (BUY)
        - ATR increasing + price falling = Strong bearish momentum (SELL)
        - ATR decreasing = Energy accumulating (HOLD, waiting for breakout)
        """
        atr = ATRStopLoss.calculate_atr(high, low, close, config.atr_period)

        if len(atr.dropna()) < 5:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="Insufficient data",
            )

        current_atr = atr.iloc[-1]
        prev_atr = atr.iloc[-5:].mean()
        atr_change = (current_atr - prev_atr) / prev_atr * 100 if prev_atr > 0 else 0

        price_change = (close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100

        # ATR expansion + price rising = Strong buy
        if atr_change > 10 and price_change > 2:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.BUY,
                confidence=min(0.9, 0.5 + atr_change / 100),
                reason=f"Volatility expansion +{atr_change:.1f}% + Price +{price_change:.1f}%",
                metadata={"atr": current_atr, "atr_change": atr_change},
            )

        # ATR expansion + price falling = Strong sell
        if atr_change > 10 and price_change < -2:
            return StrategySignal(
                strategy_name="ATR_TREND",
                signal=SignalType.SELL,
                confidence=min(0.9, 0.5 + atr_change / 100),
                reason=f"Volatility expansion +{atr_change:.1f}% + Price {price_change:.1f}%",
                metadata={"atr": current_atr, "atr_change": atr_change},
            )

        return StrategySignal(
            strategy_name="ATR_TREND",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"ATR change: {atr_change:+.1f}%, Price: {price_change:+.1f}%",
            metadata={"atr": current_atr, "atr_change": atr_change},
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 4: Adaptive RSI
# ═══════════════════════════════════════════════════════════════

class AdaptiveRSI:
    """
    Adaptive RSI - Adjusts overbought/oversold thresholds based on trend

    Key points:
    - Uptrend: Buy at RSI 40 (faster entry than the standard 30)
    - Downtrend: Wait until RSI 20 (more conservative)
    - Divergence: Price makes new low but RSI rises → Reversal signal
    """

    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI series"""
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
        Trend detection
        - Price > 50-day MA → BULL
        - Price < 50-day MA → BEAR
        - Price ≈ 50-day MA → NEUTRAL
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
        RSI Divergence Detection

        - Bullish Divergence: Price ↓ new low, RSI ↑ = Reversal imminent
        - Bearish Divergence: Price ↑ new high, RSI ↓ = Decline imminent
        """
        if len(prices) < lookback + 1 or len(rsi) < lookback + 1:
            return None

        recent_prices = prices.iloc[-lookback:]
        recent_rsi = rsi.iloc[-lookback:]

        # Remove NaN
        if recent_rsi.isna().any():
            return None

        price_min_idx = recent_prices.idxmin()
        price_max_idx = recent_prices.idxmax()

        current_price = prices.iloc[-1]
        current_rsi = rsi.iloc[-1]

        # Bullish: Price is low but RSI is higher than previous trough
        if (current_price <= recent_prices.quantile(0.2) and
            current_rsi > recent_rsi.min() + 5):
            return "BULLISH"

        # Bearish: Price is high but RSI is lower than previous peak
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
        Generate Adaptive RSI trading signal

        Logic:
        1. Determine current trend (BULL/BEAR/NEUTRAL)
        2. Apply RSI thresholds matching the trend
        3. Check for divergence (bonus confidence)
        4. Generate final signal
        """
        rsi_series = AdaptiveRSI.calculate_rsi(prices, config.rsi_period)

        if rsi_series.isna().all() or len(rsi_series.dropna()) < 5:
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="Insufficient RSI data",
            )

        current_rsi = float(rsi_series.iloc[-1])
        trend = AdaptiveRSI.detect_trend(prices)
        divergence = AdaptiveRSI.detect_divergence(
            prices, rsi_series, config.rsi_divergence_lookback
        )

        # Select thresholds based on trend
        if trend == "BULL":
            oversold = config.rsi_bull_oversold
            overbought = config.rsi_bull_overbought
            trend_label = "Uptrend"
        elif trend == "BEAR":
            oversold = config.rsi_bear_oversold
            overbought = config.rsi_bear_overbought
            trend_label = "Downtrend"
        else:
            oversold = 30.0
            overbought = 70.0
            trend_label = "Neutral"

        # Base confidence
        confidence = 0.5

        # ── Buy Signal ──
        if current_rsi <= oversold:
            confidence = 0.7
            reason = f"RSI {current_rsi:.1f} ≤ {oversold} ({trend_label} Oversold)"

            # Divergence bonus
            if divergence == "BULLISH":
                confidence = 0.9
                reason += " + 🔥 Bullish Divergence!"

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

        # ── Sell Signal ──
        if current_rsi >= overbought:
            confidence = 0.7
            reason = f"RSI {current_rsi:.1f} ≥ {overbought} ({trend_label} Overbought)"

            if divergence == "BEARISH":
                confidence = 0.9
                reason += " + ⚠️ Bearish Divergence!"

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
        # Bullish zone (50~70) gives weak buy signal
        if trend == "BULL" and 50 < current_rsi < 65:
            return StrategySignal(
                strategy_name="ADAPTIVE_RSI",
                signal=SignalType.BUY,
                confidence=0.4,
                reason=f"RSI {current_rsi:.1f} Bullish zone ({trend_label})",
                metadata={"rsi": current_rsi, "trend": trend},
            )

        return StrategySignal(
            strategy_name="ADAPTIVE_RSI",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"RSI {current_rsi:.1f} Neutral ({trend_label}, Thresholds: {oversold}/{overbought})",
            metadata={"rsi": current_rsi, "trend": trend},
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 5: Volume Confirmation (Ensemble Support Strategy)
# ═══════════════════════════════════════════════════════════════

class VolumeConfirmation:
    """Volume-based signal confirmation"""

    @staticmethod
    def get_signal(
        close: pd.Series,
        volume: pd.Series,
    ) -> StrategySignal:
        """
        Volume + Price direction confirmation
        - Volume surge + price rising = BUY confirmed
        - Volume surge + price falling = SELL confirmed
        - Volume declining = Low confidence
        """
        if len(volume) < 20:
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.HOLD,
                confidence=0.0,
                reason="Insufficient data",
            )

        avg_vol_20 = float(volume.iloc[-20:].mean())
        recent_vol = float(volume.iloc[-3:].mean())
        vol_ratio = recent_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

        price_change_3d = (
            (close.iloc[-1] - close.iloc[-4]) / close.iloc[-4] * 100
            if len(close) >= 4 else 0
        )

        # Volume surge + price rising
        if vol_ratio > 1.5 and price_change_3d > 1:
            conf = min(0.9, 0.5 + (vol_ratio - 1) * 0.3)
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.BUY,
                confidence=conf,
                reason=f"Volume {vol_ratio:.1f}x + Price +{price_change_3d:.1f}%",
                metadata={"vol_ratio": vol_ratio, "price_3d": price_change_3d},
            )

        # Volume surge + price falling
        if vol_ratio > 1.5 and price_change_3d < -1:
            conf = min(0.9, 0.5 + (vol_ratio - 1) * 0.3)
            return StrategySignal(
                strategy_name="VOLUME_CONFIRM",
                signal=SignalType.SELL,
                confidence=conf,
                reason=f"Volume {vol_ratio:.1f}x + Price {price_change_3d:.1f}%",
                metadata={"vol_ratio": vol_ratio, "price_3d": price_change_3d},
            )

        return StrategySignal(
            strategy_name="VOLUME_CONFIRM",
            signal=SignalType.HOLD,
            confidence=0.3,
            reason=f"Volume {vol_ratio:.1f}x, Price {price_change_3d:+.1f}%",
            metadata={"vol_ratio": vol_ratio},
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy Ensemble Engine
# ═══════════════════════════════════════════════════════════════

class StrategyEnsemble:
    """
    Multi-Strategy Ensemble - Final decision based on consensus of multiple strategies

    How it works:
    1. Each of 5 strategies generates a signal (BUY/SELL/HOLD + confidence)
    2. Weighted sum: BUY=+1, SELL=-1, HOLD=0 x confidence x weight
    3. Trade when the summed score exceeds threshold
    4. ATR automatically sets stop loss/take profit

    Advantages:
    - Greatly reduces false signals (whipsaws) from single strategies
    - Only trades when multiple strategies agree simultaneously → Higher win rate
    - Combines strengths of each strategy (trend + momentum + volatility)
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
        Run all strategies → Return ensemble decision

        Parameters:
            symbol: Ticker symbol
            close, high, low, volume: OHLCV data
            ma_signal: Existing MA Crossover signal (passed from smart_trader.py)
            pct_signal: Existing % Change signal (passed from smart_trader.py)
            pct_change: % Change value
        """
        signals: list[StrategySignal] = []
        cfg = self.config

        # ── Strategy 1: MA Crossover (existing, passed externally) ──
        if ma_signal is not None:
            ma_conf = 0.7 if ma_signal != SignalType.HOLD else 0.3
            signals.append(StrategySignal(
                strategy_name="MA_CROSSOVER",
                signal=ma_signal,
                confidence=ma_conf,
                reason="Golden/Death Cross" if ma_signal != SignalType.HOLD else "No Cross",
            ))

        # ── Strategy 2: % Change (existing, passed externally) ──
        if pct_signal is not None:
            pct_conf = min(0.8, 0.3 + abs(pct_change) / 20)
            signals.append(StrategySignal(
                strategy_name="PCT_CHANGE",
                signal=pct_signal,
                confidence=pct_conf,
                reason=f"{pct_change:+.1f}% Change",
            ))

        # ── Strategy 3: ATR Trend ──
        atr_signal = self.atr_engine.check_atr_trend_signal(
            close, high, low, cfg
        )
        signals.append(atr_signal)

        # ── Strategy 4: Adaptive RSI ──
        rsi_signal = self.rsi_engine.get_signal(close, cfg)
        signals.append(rsi_signal)

        # ── Strategy 5: Volume Confirmation ──
        vol_signal = self.volume_engine.get_signal(close, volume)
        signals.append(vol_signal)

        # ═══ Calculate Ensemble Score ═══
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

        # Normalize (-1 ~ +1)
        max_possible = sum(weight_map.values())
        if max_possible > 0:
            consensus_score = consensus_score / max_possible

        # ═══ Final Decision ═══
        final_signal = SignalType.HOLD
        reason_parts = []

        if (consensus_score >= cfg.ensemble_buy_threshold and
            buy_count >= cfg.min_strategies_agree):
            final_signal = SignalType.BUY
            reason_parts.append(
                f"Consensus BUY ({buy_count} strategies agree, "
                f"Score: {consensus_score:+.2f})"
            )
        elif (consensus_score <= cfg.ensemble_sell_threshold and
              sell_count >= cfg.min_strategies_agree):
            final_signal = SignalType.SELL
            reason_parts.append(
                f"Consensus SELL ({sell_count} strategies agree, "
                f"Score: {consensus_score:+.2f})"
            )
        else:
            reason_parts.append(
                f"Consensus not met (BUY:{buy_count} SELL:{sell_count}, "
                f"Score: {consensus_score:+.2f}, "
                f"Threshold: ±{cfg.ensemble_buy_threshold})"
            )

        # ═══ ATR Stop Loss/Take Profit Calculation ═══
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
#  Test / Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    """Demo — Test ensemble with synthetic data"""
    np.random.seed(42)

    # 60-day synthetic price data (uptrend)
    n = 60
    base = 100
    trend = np.linspace(0, 20, n)
    noise = np.random.randn(n) * 2
    close_arr = base + trend + noise
    high_arr = close_arr + np.abs(np.random.randn(n)) * 1.5
    low_arr = close_arr - np.abs(np.random.randn(n)) * 1.5
    vol_arr = np.random.randint(500000, 2000000, n).astype(float)
    # Simulate recent volume surge
    vol_arr[-5:] *= 2.5

    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = pd.Series(close_arr, index=dates)
    high = pd.Series(high_arr, index=dates)
    low = pd.Series(low_arr, index=dates)
    volume = pd.Series(vol_arr, index=dates)

    print("=" * 70)
    print("  🧪 Advanced Strategies Demo")
    print("=" * 70)

    config = AdvancedConfig()
    ensemble = StrategyEnsemble(config)

    # Run ensemble analysis
    decision = ensemble.analyze(
        symbol="DEMO",
        close=close,
        high=high,
        low=low,
        volume=volume,
        ma_signal=SignalType.BUY,   # Assumption: Golden cross occurred
        pct_signal=SignalType.HOLD,
        pct_change=2.3,
    )

    print(f"\n  📊 Ensemble Decision:")
    print(f"  {decision}")
    print(f"\n  Individual Strategy Results:")
    for sig in decision.individual_signals:
        print(
            f"    {sig.strategy_name:18s} | {sig.signal.value} | "
            f"Confidence: {sig.confidence:.0%} | {sig.reason}"
        )

    print(f"\n  🛡️ Risk Management:")
    print(f"    Stop Loss: ${decision.stop_loss_price}")
    print(f"    Take Profit: ${decision.take_profit_price}")
    print(f"    ATR: ${decision.atr_value:.2f}")
    print(f"\n  Final: {decision.final_signal.value} "
          f"(Consensus Score: {decision.consensus_score:+.3f})")
    print("=" * 70)


if __name__ == "__main__":
    demo()
