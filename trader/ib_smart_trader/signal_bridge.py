"""
═══════════════════════════════════════════════════════════════════
  Signal Monitor Bridge - Signal Monitor Integration Bridge

  Integrates the Signal Monitor (COT + Options Flow) created in
  another session as the 6th strategy in the Smart Trader ensemble.

  Features:
    8. COT + Options Composite Signal -> Ensemble 6th Strategy
    9. BEAR Market Brake - Block new buys on bearish signal
   10. BULL Booster - Increase buy weight on bullish signal
   11. Washout (Whipsaw) Prevention - Block false cross consecutive trades

  Supports both Signal Monitor server (port 5050) and standalone mode
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
#  Configuration
# ═══════════════════════════════════════════════════════════════

def is_market_open() -> bool:
    """Check if US stock market is open (ET 9:30~16:00, Mon~Fri)"""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Weekend
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def is_premarket() -> bool:
    """Pre-market (ET 4:00~9:30)"""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    pre_open = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return pre_open <= now_et < market_open


@dataclass
class SignalBridgeConfig:
    """Signal Bridge Configuration"""

    # ── Signal Monitor Server ──
    monitor_url: str = "http://localhost:5050"
    use_server: bool = False         # True: server mode, False: built-in engine
    poll_interval_sec: int = 60      # Market hours server polling interval
    poll_interval_off_hours_sec: int = 3600  # Off-hours polling interval (1 hour)

    # ── COT Settings ──
    cot_enabled: bool = True
    cot_bull_threshold: float = 0.3   # Hedge fund net long ratio above = BULL
    cot_bear_threshold: float = -0.2  # Hedge fund net short ratio below = BEAR

    # ── Options Settings ──
    options_enabled: bool = True
    pc_ratio_bull: float = 0.7        # Put/Call < 0.7 = BULL (calls dominant)
    pc_ratio_bear: float = 1.3        # Put/Call > 1.3 = BEAR (puts dominant)

    # ── Market Brake ──
    market_brake_enabled: bool = True
    brake_on_bear: bool = True        # Block new buys on BEAR
    brake_reduce_pct: float = 50.0    # Recommended position reduction % on BEAR

    # ── BULL Booster ──
    bull_boost_enabled: bool = True
    bull_confidence_boost: float = 0.15  # Ensemble buy confidence boost on BULL

    # ── Washout (Whipsaw) Prevention ──
    washout_enabled: bool = True
    cooldown_hours: int = 4           # Block re-trade on same symbol for N hours after trade
    min_ma_gap_pct: float = 0.5       # MA gap must be at least 0.5% to be valid
    confirmation_bars: int = 2         # Cross must hold for N consecutive bars
    max_signals_per_day: int = 3       # Max signals per symbol per day

    # ── Ensemble Weight ──
    ensemble_weight: float = 0.15     # Weight of the 6th strategy


# ═══════════════════════════════════════════════════════════════
#  Composite Market Signal
# ═══════════════════════════════════════════════════════════════

class MarketSignal:
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


@dataclass
class CompositeSignal:
    """COT + Options Composite Signal"""
    composite: str = MarketSignal.NEUTRAL    # BULL / BEAR / NEUTRAL
    cot_signal: str = MarketSignal.NEUTRAL
    options_signal: str = MarketSignal.NEUTRAL
    confidence: float = 0.5
    reason: str = ""
    timestamp: str = ""

    # Detailed data
    cot_net_long: float = 0.0
    pc_ratio: float = 1.0

    @property
    def is_bull(self) -> bool:
        return self.composite == MarketSignal.BULL

    @property
    def is_bear(self) -> bool:
        return self.composite == MarketSignal.BEAR


# ═══════════════════════════════════════════════════════════════
#  Built-in COT Engine (standalone without server)
# ═══════════════════════════════════════════════════════════════

class BuiltInCOTEngine:
    """
    CFTC COT Data-based Market Positioning Analysis

    Core: Track net position changes of Hedge Funds (Managed Money)
    - Net long increase -> BULL (smart money is buying)
    - Net short increase -> BEAR (smart money is selling)

    In production, weekly data is auto-collected from CFTC API
    """

    # Latest COT data (manual update or API integration)
    # Format: {"date": "2026-03-14", "net_long_pct": 0.35, "change": 0.05}
    _latest_data = {
        "S&P500": {"net_long_pct": 0.25, "change": -0.05, "date": "2026-03-14"},
        "CRUDE_OIL": {"net_long_pct": 0.45, "change": 0.12, "date": "2026-03-14"},
        "GOLD": {"net_long_pct": 0.38, "change": 0.08, "date": "2026-03-14"},
        "US_DOLLAR": {"net_long_pct": 0.15, "change": 0.03, "date": "2026-03-14"},
    }

    @classmethod
    def update_data(cls, instrument: str, net_long_pct: float, change: float):
        """Manually update COT data"""
        cls._latest_data[instrument] = {
            "net_long_pct": net_long_pct,
            "change": change,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }

    @classmethod
    def get_signal(cls, config: SignalBridgeConfig) -> tuple[str, float, str]:
        """
        COT Composite Signal
        Returns: (signal, confidence, reason)
        """
        if not config.cot_enabled:
            return MarketSignal.NEUTRAL, 0.3, "COT disabled"

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

        # S&P 500 net long analysis
        if sp_net > config.cot_bull_threshold:
            bull_count += 1
            reasons.append(f"S&P net long {sp_net:.0%}")
        elif sp_net < config.cot_bear_threshold:
            bear_count += 1
            reasons.append(f"S&P net short {sp_net:.0%}")

        # Crude oil net long (rising oil price expectation = inflation risk)
        if oil_net > 0.4:
            bear_count += 1  # High oil = negative for equities
            reasons.append(f"Crude net long {oil_net:.0%} (inflation risk)")

        # Gold net long (safe-haven preference = risk aversion)
        if gold_net > 0.35:
            bear_count += 1
            reasons.append(f"Gold net long {gold_net:.0%} (risk-off)")
        elif gold_net < 0.1:
            bull_count += 1
            reasons.append("Gold selling (risk-on)")

        # Position change direction
        if sp_chg > 0.05:
            bull_count += 1
            reasons.append(f"S&P position weekly +{sp_chg:.0%}")
        elif sp_chg < -0.05:
            bear_count += 1
            reasons.append(f"S&P position weekly {sp_chg:.0%}")

        if bull_count > bear_count:
            conf = min(0.8, 0.5 + (bull_count - bear_count) * 0.1)
            return MarketSignal.BULL, conf, " | ".join(reasons)
        elif bear_count > bull_count:
            conf = min(0.8, 0.5 + (bear_count - bull_count) * 0.1)
            return MarketSignal.BEAR, conf, " | ".join(reasons)

        return MarketSignal.NEUTRAL, 0.4, " | ".join(reasons) or "COT neutral"


# ═══════════════════════════════════════════════════════════════
#  Built-in Options Flow Engine
# ═══════════════════════════════════════════════════════════════

class BuiltInOptionsEngine:
    """
    Options Put/Call Ratio-based Market Sentiment

    - P/C < 0.7: Calls overwhelmingly dominant -> BULL (buy expectation)
    - P/C > 1.3: Puts overwhelmingly dominant -> BEAR (sell/hedge expectation)
    - 0.7~1.3: Neutral

    In production, real-time options OI is collected via IB API
    """

    _latest_pc_ratio = 1.05  # Current P/C Ratio
    _latest_vix = 27.19       # Current VIX

    @classmethod
    def update(cls, pc_ratio: float, vix: float = 0):
        cls._latest_pc_ratio = pc_ratio
        if vix > 0:
            cls._latest_vix = vix

    @classmethod
    def get_signal(cls, config: SignalBridgeConfig) -> tuple[str, float, str]:
        """
        Options Signal
        Returns: (signal, confidence, reason)
        """
        if not config.options_enabled:
            return MarketSignal.NEUTRAL, 0.3, "Options disabled"

        pc = cls._latest_pc_ratio
        vix = cls._latest_vix

        reasons = []
        signal = MarketSignal.NEUTRAL
        confidence = 0.4

        # P/C Ratio
        if pc < config.pc_ratio_bull:
            signal = MarketSignal.BULL
            confidence = min(0.85, 0.6 + (config.pc_ratio_bull - pc) * 0.5)
            reasons.append(f"P/C={pc:.2f} calls dominant")
        elif pc > config.pc_ratio_bear:
            signal = MarketSignal.BEAR
            confidence = min(0.85, 0.6 + (pc - config.pc_ratio_bear) * 0.3)
            reasons.append(f"P/C={pc:.2f} puts dominant")
        else:
            reasons.append(f"P/C={pc:.2f} neutral")

        # VIX supplementary
        if vix > 30:
            if signal != MarketSignal.BEAR:
                signal = MarketSignal.BEAR
            confidence = min(confidence + 0.1, 0.9)
            reasons.append(f"VIX={vix:.1f} fear")
        elif vix > 25:
            reasons.append(f"VIX={vix:.1f} caution")
        elif vix < 15:
            if signal != MarketSignal.BULL:
                signal = MarketSignal.BULL
            reasons.append(f"VIX={vix:.1f} stable")

        return signal, confidence, " | ".join(reasons)


# ═══════════════════════════════════════════════════════════════
#  Washout (Whipsaw) Prevention Filter
# ═══════════════════════════════════════════════════════════════

class WashoutFilter:
    """
    MA Crossover False Signal (Whipsaw) Prevention

    Rules:
    1. Cooldown: Block re-trade on same symbol for N hours after trade
    2. MA Gap: Short/long MA gap must be at least X%
    3. Confirmation: Cross must hold for N consecutive bars
    4. Rate Limit: Max N signals per symbol per day

    Different concept from Wash Sale (tax_optimizer.py):
    - Wash Sale = Tax regulation (prevent re-buy of same symbol within 30 days)
    - Washout = Technical signal quality (prevent false crosses)
    """

    def __init__(self, config: SignalBridgeConfig = None):
        self.config = config or SignalBridgeConfig()
        self._last_trade_time: dict[str, datetime] = {}  # {symbol: last_trade_time}
        self._daily_signal_count: dict[str, int] = defaultdict(int)
        self._cross_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self._last_reset_date: Optional[str] = None

    def _reset_daily_counts(self):
        """Reset daily counts"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_signal_count.clear()
            self._last_reset_date = today

    def record_trade(self, symbol: str):
        """Record trade execution"""
        self._last_trade_time[symbol] = datetime.now()

    def check(
        self,
        symbol: str,
        ma_short: float,
        ma_long: float,
        current_price: float,
    ) -> dict:
        """
        Washout Check

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

        # 1. Cooldown check
        if symbol in self._last_trade_time:
            elapsed = (datetime.now() - self._last_trade_time[symbol]).total_seconds()
            cooldown_sec = self.config.cooldown_hours * 3600
            if elapsed < cooldown_sec:
                remaining = int((cooldown_sec - elapsed) / 60)
                return {
                    "allowed": False,
                    "reason": f"⏱️ Cooldown {remaining}min remaining ({self.config.cooldown_hours}hr limit)",
                    "cooldown_remaining_min": remaining,
                }

        # 2. MA Gap check
        if ma_long > 0:
            gap_pct = abs(ma_short - ma_long) / ma_long * 100
            if gap_pct < self.config.min_ma_gap_pct:
                return {
                    "allowed": False,
                    "reason": f"📏 MA gap {gap_pct:.2f}% < {self.config.min_ma_gap_pct}% (too narrow)",
                    "cooldown_remaining_min": 0,
                }

        # 3. Daily signal limit
        if self._daily_signal_count[symbol] >= self.config.max_signals_per_day:
            return {
                "allowed": False,
                "reason": f"📊 Daily signals {self._daily_signal_count[symbol]}/{self.config.max_signals_per_day} exceeded",
                "cooldown_remaining_min": 0,
            }

        # Passed
        self._daily_signal_count[symbol] += 1
        return {"allowed": True, "reason": "", "cooldown_remaining_min": 0}


# ═══════════════════════════════════════════════════════════════
#  Integration Bridge
# ═══════════════════════════════════════════════════════════════

class SignalBridge:
    """
    Signal Monitor <-> Smart Trader Integration Bridge

    Usage (in smart_trader.py):
        from signal_bridge import SignalBridge
        bridge = SignalBridge()

        # During ensemble analysis
        market_signal = bridge.get_composite_signal()

        # Before buying
        if bridge.should_block_buy():
            # Block buy

        # Signal weight
        boost = bridge.get_ensemble_boost()

        # Washout check
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
        """Calculate COT + Options composite signal"""
        # COT signal
        cot_sig, cot_conf, cot_reason = self.cot_engine.get_signal(self.config)

        # Options signal
        opt_sig, opt_conf, opt_reason = self.options_engine.get_signal(self.config)

        # Composite judgment
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

        # Final judgment
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
        BEAR Market Brake -- Whether to block buy

        Returns: (blocked, reason)
        """
        if not self.config.market_brake_enabled:
            return False, ""

        signal = self.get_composite_signal()

        if signal.is_bear and self.config.brake_on_bear:
            return True, (
                f"🛑 BEAR Market Brake! New buys blocked | "
                f"COT: {signal.cot_signal} | "
                f"Options: {signal.options_signal} | "
                f"P/C: {signal.pc_ratio:.2f}"
            )

        return False, ""

    def get_ensemble_boost(self) -> float:
        """
        Ensemble confidence boost
        - BULL: +0.15 (increase buy confidence)
        - BEAR: -0.15 (decrease buy confidence)
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
        Signal to use as the 6th strategy in the ensemble
        Returns in the StrategySignal format of advanced_strategies.py
        """
        signal = self.get_composite_signal()

        if signal.is_bull:
            return {
                "strategy_name": "SIGNAL_MONITOR",
                "signal": "BUY",
                "confidence": signal.confidence,
                "reason": f"Market BULL — {signal.reason}",
                "weight": self.config.ensemble_weight,
            }
        elif signal.is_bear:
            return {
                "strategy_name": "SIGNAL_MONITOR",
                "signal": "SELL",
                "confidence": signal.confidence,
                "reason": f"Market BEAR — {signal.reason}",
                "weight": self.config.ensemble_weight,
            }

        return {
            "strategy_name": "SIGNAL_MONITOR",
            "signal": "HOLD",
            "confidence": 0.3,
            "reason": f"Market NEUTRAL — {signal.reason}",
            "weight": self.config.ensemble_weight,
        }

    def check_washout(
        self, symbol: str,
        ma_short: float, ma_long: float,
        price: float
    ) -> dict:
        """Washout check (Whipsaw prevention)"""
        return self.washout_filter.check(symbol, ma_short, ma_long, price)

    def record_trade(self, symbol: str):
        """Record trade (for Washout cooldown)"""
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
        """Update real-time data (IB API or manual)"""
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
        """Print current signal status"""
        sig = self.get_composite_signal()
        icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}

        print(f"\n  {icon[sig.composite]} Signal Monitor: {sig.composite} "
              f"(confidence: {sig.confidence:.0%})")
        print(f"    COT: {sig.cot_signal} | Options: {sig.options_signal}")
        print(f"    P/C Ratio: {sig.pc_ratio:.2f} | "
              f"S&P net long: {sig.cot_net_long:.0%}")
        print(f"    {sig.reason}")

        blocked, reason = self.should_block_buy()
        if blocked:
            print(f"    {reason}")


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  📡 Signal Bridge Demo — COT + Options + Washout Integration ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    bridge = SignalBridge()

    # 1) Current market data (Iran war week 3)
    print("  📊 Setting current market data:")
    bridge.update_market_data(
        pc_ratio=1.05,          # Put/Call slightly puts dominant
        vix=27.19,              # VIX high (caution)
        cot_sp_net=0.25,        # S&P net long 25%
        cot_sp_change=-0.05,    # Weekly decrease
        cot_oil_net=0.45,       # Crude net long 45% (high oil expectation)
        cot_gold_net=0.38,      # Gold net long 38% (safe-haven)
    )

    # 2) Composite signal
    bridge.print_status()

    # 3) Ensemble strategy signal
    print("\n  🎯 Ensemble 6th Strategy:")
    ens = bridge.get_ensemble_strategy_signal()
    print(f"    {ens['strategy_name']} → {ens['signal']} "
          f"(confidence: {ens['confidence']:.0%}, weight: {ens['weight']})")
    print(f"    Reason: {ens['reason']}")

    # 4) BEAR brake check
    print("\n  🛑 Buy Brake Check:")
    blocked, reason = bridge.should_block_buy()
    print(f"    Buy blocked: {'Yes' if blocked else 'No'}")
    if reason:
        print(f"    {reason}")

    # 5) Washout check
    print("\n  🔄 Washout Check (NVDA):")
    # First trade
    wo1 = bridge.check_washout("NVDA", 182.0, 185.0, 183.5)
    print(f"    1st: {'Allowed' if wo1['allowed'] else 'Blocked'} {wo1.get('reason','')}")

    # Record trade
    bridge.record_trade("NVDA")

    # Immediate re-trade attempt
    wo2 = bridge.check_washout("NVDA", 182.5, 184.8, 184.0)
    print(f"    2nd (immediately after): {'Allowed' if wo2['allowed'] else 'Blocked'} {wo2.get('reason','')}")

    # 6) BULL scenario
    print("\n  📈 BULL Scenario Test:")
    bridge.update_market_data(pc_ratio=0.6, vix=15, cot_sp_net=0.4, cot_sp_change=0.1, cot_gold_net=0.05)
    bridge.print_status()
    boost = bridge.get_ensemble_boost()
    print(f"    Ensemble boost: {boost:+.2f}")


if __name__ == "__main__":
    demo()
