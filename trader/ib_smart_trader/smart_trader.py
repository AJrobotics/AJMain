"""
═══════════════════════════════════════════════════════════════════
  IB Smart Trader v2.0 - Interactive Brokers Auto Trading System

  Strategies (5-Strategy Ensemble):
    1. Moving Average Crossover (MA Crossover) - Short/Long MA Cross
    2. % Change Based Trading - Buy/Sell on configured % Drop/Rise
    3. ATR Dynamic Stop Loss/Take Profit - Volatility-based risk management [NEW]
    4. Adaptive RSI - Trend-context based trade signals [NEW]
    5. Multi-Strategy Ensemble - Trade only on multi-strategy consensus [NEW]

  Modes:
    - AUTO:  Auto trade execution
    - ALERT: Signal alerts only (console + log)

  Connection: TWS (Trader Workstation) via ib_insync
═══════════════════════════════════════════════════════════════════
"""

import logging
import time
import json
import os
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from collections import deque

# ── Third-party imports ──────────────────────────────────────────────
try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║  Required packages are not installed.                    ║
    ║  Please install them with the command below:             ║
    ║                                                          ║
    ║  pip install ib_insync pandas numpy                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    print(f"Missing: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

class TradeMode(Enum):
    AUTO = "auto"       # Auto trading
    ALERT = "alert"     # Signal alerts only


@dataclass
class TradingConfig:
    """Full trading configuration"""

    # ── IB Connection Settings ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # TWS Paper: 7497, TWS Live: 7496
    client_id: int = 1

    # ── Trading Mode ──
    trade_mode: TradeMode = TradeMode.ALERT  # Default: alerts only

    # ── MA Crossover Strategy Settings ──
    ma_short_period: int = 10    # Short-term moving average (days)
    ma_long_period: int = 30     # Long-term moving average (days)

    # ── % Change Strategy Settings ──
    buy_drop_pct: float = -5.0   # Buy signal when dropped by this % or more
    sell_rise_pct: float = 5.0   # Sell signal when risen by this % or more
    pct_lookback_days: int = 5   # Number of days to look back for comparison

    # ── Order Settings ──
    default_quantity: int = 10   # Default order quantity
    max_position_size: int = 100 # Max holding quantity per symbol

    # ── Ensemble Mode (v2.0 new) ──
    use_ensemble: bool = True     # True: 5-strategy ensemble, False: legacy individual strategies

    # ── Monitoring Settings ──
    check_interval_sec: int = 30       # Signal check interval during market hours (seconds)
    check_interval_off_sec: int = 900  # Signal check interval off-hours (seconds, 15 min)
    history_bar_size: str = "1 day"
    history_duration: str = "60 D"

    # ── Logging ──
    log_file: str = "smart_trader.log"

    def save(self, filepath: str = "config.json"):
        """Save config to JSON"""
        data = asdict(self)
        data["trade_mode"] = self.trade_mode.value
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  ✅ Config saved: {filepath}")

    @classmethod
    def load(cls, filepath: str = "config.json") -> "TradingConfig":
        """Load config from JSON"""
        if not os.path.exists(filepath):
            print(f"  ⚠️  Config file not found. Using default settings.")
            return cls()
        with open(filepath, "r") as f:
            data = json.load(f)
        data["trade_mode"] = TradeMode(data.get("trade_mode", "alert"))
        return cls(**data)


# ═══════════════════════════════════════════════════════════════
#  Signal & Log Data Structures
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class TradeSignal:
    """Trade signal"""
    symbol: str
    signal: SignalType
    strategy: str           # "MA_CROSSOVER" or "PCT_CHANGE"
    price: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
    executed: bool = False

    def __str__(self):
        status = "✅ Executed" if self.executed else "⏳ Pending"
        return (
            f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] "
            f"{self.signal.value} {self.symbol} @ ${self.price:.2f} "
            f"| Strategy: {self.strategy} | {self.reason} | {status}"
        )


# ═══════════════════════════════════════════════════════════════
#  Technical Analysis Engine
# ═══════════════════════════════════════════════════════════════

class TechnicalAnalyzer:
    """Technical indicator calculations"""

    @staticmethod
    def moving_average(prices: pd.Series, period: int) -> pd.Series:
        """Calculate Simple Moving Average (SMA)"""
        return prices.rolling(window=period).mean()

    @staticmethod
    def check_ma_crossover(
        prices: pd.Series,
        short_period: int,
        long_period: int
    ) -> Optional[SignalType]:
        """
        Check MA Crossover
        - Golden Cross (short > long): BUY
        - Dead Cross (short < long): SELL
        """
        if len(prices) < long_period + 2:
            return None

        ma_short = TechnicalAnalyzer.moving_average(prices, short_period)
        ma_long = TechnicalAnalyzer.moving_average(prices, long_period)

        # Compare current and previous values
        curr_short = ma_short.iloc[-1]
        prev_short = ma_short.iloc[-2]
        curr_long = ma_long.iloc[-1]
        prev_long = ma_long.iloc[-2]

        # NaN check
        if any(pd.isna([curr_short, prev_short, curr_long, prev_long])):
            return None

        # Golden Cross: short crosses above long from below
        if prev_short <= prev_long and curr_short > curr_long:
            return SignalType.BUY

        # Dead Cross: short crosses below long from above
        if prev_short >= prev_long and curr_short < curr_long:
            return SignalType.SELL

        return SignalType.HOLD

    @staticmethod
    def check_pct_change(
        prices: pd.Series,
        buy_threshold: float,
        sell_threshold: float,
        lookback: int
    ) -> tuple[Optional[SignalType], float]:
        """
        Check % change
        - Dropped by buy_threshold% or more vs lookback days ago: BUY
        - Risen by sell_threshold% or more vs lookback days ago: SELL
        """
        if len(prices) < lookback + 1:
            return None, 0.0

        current_price = prices.iloc[-1]
        past_price = prices.iloc[-(lookback + 1)]

        if past_price == 0:
            return None, 0.0

        pct_change = ((current_price - past_price) / past_price) * 100

        if pct_change <= buy_threshold:
            return SignalType.BUY, pct_change
        elif pct_change >= sell_threshold:
            return SignalType.SELL, pct_change

        return SignalType.HOLD, pct_change

    @staticmethod
    def get_ma_values(
        prices: pd.Series,
        short_period: int,
        long_period: int
    ) -> dict:
        """Return current MA values (for dashboard)"""
        ma_short = TechnicalAnalyzer.moving_average(prices, short_period)
        ma_long = TechnicalAnalyzer.moving_average(prices, long_period)
        return {
            "ma_short": round(ma_short.iloc[-1], 2) if not pd.isna(ma_short.iloc[-1]) else None,
            "ma_long": round(ma_long.iloc[-1], 2) if not pd.isna(ma_long.iloc[-1]) else None,
            "spread": round(ma_short.iloc[-1] - ma_long.iloc[-1], 2)
                      if not any(pd.isna([ma_short.iloc[-1], ma_long.iloc[-1]])) else None,
        }


# ═══════════════════════════════════════════════════════════════
#  Main Trading Bot
# ═══════════════════════════════════════════════════════════════

class SmartTrader:
    """IB Smart Trader main class"""

    def __init__(self, config: TradingConfig = None):
        self.config = config or TradingConfig()
        self.ib = IB()
        self.analyzer = TechnicalAnalyzer()
        self.signals_history: list[TradeSignal] = []
        self.positions: dict = {}
        self.watchlist: list[Stock] = []
        self.running = False

        # v2.0: Initialize ensemble engine
        self.ensemble = None
        if self.config.use_ensemble:
            try:
                from advanced_strategies import (
                    StrategyEnsemble, AdvancedConfig, EnsembleDecision,
                    SignalType as AdvSignalType,
                )
                self.ensemble = StrategyEnsemble(AdvancedConfig())
                self._adv_signal_type = AdvSignalType
            except ImportError:
                print("  ⚠️  advanced_strategies.py not found. Using legacy strategies only.")
                self.ensemble = None

        # Active stop loss/take profit tracking (per symbol)
        self.active_stops: dict = {}  # {symbol: {"sl": price, "tp": price, "trail_high": price}}

        # v2.1: Initialize risk shield system
        self.risk_shield = None
        try:
            from risk_shield import RiskShield, RiskShieldConfig, RiskAction
            self.risk_shield = RiskShield(RiskShieldConfig())
            self._risk_action = RiskAction
        except ImportError:
            print("  ⚠️  risk_shield.py not found. Running without risk shield.")

        # v2.2: Initialize tax optimization system
        self.tax_optimizer = None
        try:
            from tax_optimizer import TaxOptimizer, TaxConfig
            self.tax_optimizer = TaxOptimizer(TaxConfig())
        except ImportError:
            print("  ⚠️  tax_optimizer.py not found. Running without tax optimization.")

        # v2.3: Initialize signal monitor bridge
        self.signal_bridge = None
        try:
            from signal_bridge import SignalBridge, SignalBridgeConfig
            self.signal_bridge = SignalBridge(SignalBridgeConfig())
        except ImportError:
            print("  ⚠️  signal_bridge.py not found. Running without signal monitor.")

        # Logging setup
        self._setup_logging()

    # ── Logging ──────────────────────────────────────────────────

    def _setup_logging(self):
        """Set up logging"""
        self.logger = logging.getLogger("SmartTrader")
        self.logger.setLevel(logging.INFO)

        # File handler
        fh = logging.FileHandler(
            self.config.log_file, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))

        # Console handler
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S"
        ))

        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    # ── IB Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to IB TWS"""
        self.logger.info("=" * 60)
        self.logger.info("  IB Smart Trader Starting")
        self.logger.info(f"  Mode: {self.config.trade_mode.value.upper()}")
        self.logger.info("=" * 60)

        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id
            )
            self.logger.info(
                f"✅ TWS connection successful "
                f"({self.config.ib_host}:{self.config.ib_port})"
            )

            # Print account info
            accounts = self.ib.managedAccounts()
            self.logger.info(f"📋 Account: {accounts}")
            return True

        except Exception as e:
            self.logger.error(f"❌ TWS connection failed: {e}")
            self.logger.error(
                "   → Make sure TWS is running.\n"
                "   → In TWS > Edit > Global Config > API > Settings,\n"
                "     check 'Enable ActiveX and Socket Clients'\n"
                f"   → Socket port: {self.config.ib_port}"
            )
            return False

    def disconnect(self):
        """Disconnect from IB"""
        if self.ib.isConnected():
            self.ib.disconnect()
            self.logger.info("🔌 TWS disconnected")

    # ── Portfolio & Watchlist ────────────────────────────────

    def load_portfolio(self) -> dict:
        """Load current held positions"""
        self.logger.info("📊 Loading portfolio...")
        portfolio = self.ib.portfolio()

        self.positions = {}
        for item in portfolio:
            symbol = item.contract.symbol
            self.positions[symbol] = {
                "contract": item.contract,
                "quantity": item.position,
                "avg_cost": item.averageCost,
                "market_value": item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "realized_pnl": item.realizedPNL,
            }
            self.logger.info(
                f"  📌 {symbol}: {item.position} shares "
                f"| Avg cost: ${item.averageCost:.2f} "
                f"| Unrealized P&L: ${item.unrealizedPNL:+,.2f}"
            )

        if not self.positions:
            self.logger.info("  (No holdings)")

        return self.positions

    def set_watchlist(self, symbols: list[str], exchange: str = "SMART", currency: str = "USD"):
        """
        Set symbols to monitor

        Example: trader.set_watchlist(["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"])
        """
        self.watchlist = []
        self.logger.info(f"👀 Setting watchlist: {symbols}")

        for sym in symbols:
            contract = Stock(sym, exchange, currency)
            self.ib.qualifyContracts(contract)
            self.watchlist.append(contract)
            self.logger.info(f"  ✅ {sym} added")

        return self.watchlist

    # ── Market Data ───────────────────────────────────────────

    def get_historical_prices(self, contract: Contract) -> Optional[pd.DataFrame]:
        """Fetch historical price data"""
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.history_duration,
                barSizeSetting=self.config.history_bar_size,
                whatToShow="ADJUSTED_LAST",
                useRTH=True,        # Regular trading hours only
                formatDate=1,
            )

            if not bars:
                self.logger.warning(
                    f"⚠️  {contract.symbol}: No historical data available"
                )
                return None

            df = util.df(bars)
            df.set_index("date", inplace=True)
            return df

        except Exception as e:
            self.logger.error(
                f"❌ {contract.symbol} historical data request failed: {e}"
            )
            return None

    def get_current_price(self, contract: Contract) -> Optional[float]:
        """Fetch current price"""
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)  # Wait for data reception

            price = ticker.marketPrice()
            if price and not pd.isna(price):
                return float(price)

            # Use last trade price
            price = ticker.last
            if price and not pd.isna(price):
                return float(price)

            return None
        except Exception as e:
            self.logger.error(f"❌ {contract.symbol} price request failed: {e}")
            return None

    # ── Signal Analysis ─────────────────────────────────────────────

    def analyze_stock(self, contract: Contract) -> list[TradeSignal]:
        """Analyze a single symbol -> return trade signal list (v2.0 ensemble integrated)"""
        symbol = contract.symbol
        signals = []

        # Historical data
        df = self.get_historical_prices(contract)
        if df is None or len(df) < self.config.ma_long_period + 2:
            return signals

        close_prices = df["close"]
        current_price = close_prices.iloc[-1]

        # ── Legacy Strategy 1: MA Crossover ──
        ma_signal = self.analyzer.check_ma_crossover(
            close_prices,
            self.config.ma_short_period,
            self.config.ma_long_period
        )

        # ── Legacy Strategy 2: % Change ──
        pct_signal, pct_change = self.analyzer.check_pct_change(
            close_prices,
            self.config.buy_drop_pct,
            self.config.sell_rise_pct,
            self.config.pct_lookback_days,
        )

        # ═══ v2.0: Ensemble Mode ═══
        if self.ensemble is not None and self.config.use_ensemble:
            return self._analyze_ensemble(
                symbol, df, close_prices, current_price,
                ma_signal, pct_signal, pct_change,
            )

        # ═══ Legacy Mode: Individual Strategies ═══
        if ma_signal and ma_signal != SignalType.HOLD:
            ma_info = self.analyzer.get_ma_values(
                close_prices,
                self.config.ma_short_period,
                self.config.ma_long_period
            )
            cross_type = "Golden Cross ↑" if ma_signal == SignalType.BUY else "Dead Cross ↓"
            reason = (
                f"{cross_type} | "
                f"MA{self.config.ma_short_period}={ma_info['ma_short']} "
                f"MA{self.config.ma_long_period}={ma_info['ma_long']} "
                f"(Spread: {ma_info['spread']:+.2f})"
            )
            signals.append(TradeSignal(
                symbol=symbol, signal=ma_signal,
                strategy="MA_CROSSOVER", price=current_price, reason=reason,
            ))

        if pct_signal and pct_signal != SignalType.HOLD:
            direction = "Down 📉" if pct_change < 0 else "Up 📈"
            reason = (
                f"{self.config.pct_lookback_days}-day {pct_change:+.2f}% {direction} | "
                f"Current: ${current_price:.2f}"
            )
            signals.append(TradeSignal(
                symbol=symbol, signal=pct_signal,
                strategy="PCT_CHANGE", price=current_price, reason=reason,
            ))

        if not signals:
            self._log_hold(symbol, close_prices, current_price, pct_change)

        return signals

    def _analyze_ensemble(
        self, symbol, df, close_prices, current_price,
        ma_signal, pct_signal, pct_change,
    ) -> list[TradeSignal]:
        """v2.0 Ensemble analysis - 5-strategy consensus based"""
        signals = []

        # OHLCV needed for ensemble
        high = df["high"] if "high" in df.columns else close_prices
        low = df["low"] if "low" in df.columns else close_prices
        volume = df["volume"] if "volume" in df.columns else pd.Series(
            [1000000] * len(close_prices), index=close_prices.index
        )

        # Convert MA/PCT signals to ensemble SignalType
        adv_ma = None
        adv_pct = None
        AST = self._adv_signal_type

        if ma_signal == SignalType.BUY:
            adv_ma = AST.BUY
        elif ma_signal == SignalType.SELL:
            adv_ma = AST.SELL
        elif ma_signal == SignalType.HOLD:
            adv_ma = AST.HOLD

        if pct_signal == SignalType.BUY:
            adv_pct = AST.BUY
        elif pct_signal == SignalType.SELL:
            adv_pct = AST.SELL
        elif pct_signal == SignalType.HOLD:
            adv_pct = AST.HOLD

        # Run ensemble!
        decision = self.ensemble.analyze(
            symbol=symbol,
            close=close_prices,
            high=high,
            low=low,
            volume=volume,
            ma_signal=adv_ma,
            pct_signal=adv_pct,
            pct_change=pct_change,
        )

        # Log ensemble results
        buy_count = sum(1 for s in decision.individual_signals if s.signal == AST.BUY)
        sell_count = sum(1 for s in decision.individual_signals if s.signal == AST.SELL)
        hold_count = sum(1 for s in decision.individual_signals if s.signal == AST.HOLD)

        # ── v2.3: Signal Bridge - 6th strategy + boost ──
        bridge_signal_str = ""
        if self.signal_bridge is not None:
            # 6th strategy signal
            bridge_sig = self.signal_bridge.get_ensemble_strategy_signal()
            bridge_signal_str = bridge_sig["signal"]

            # Apply ensemble boost
            boost = self.signal_bridge.get_ensemble_boost()
            if boost != 0:
                decision.consensus_score += boost
                self.logger.info(
                    f"  📡 Signal Monitor: {bridge_sig['signal']} "
                    f"(Confidence: {bridge_sig['confidence']:.0%}) | "
                    f"Boost: {boost:+.2f} → Consensus: {decision.consensus_score:+.3f}"
                )
            else:
                self.logger.info(
                    f"  📡 Signal Monitor: {bridge_sig['signal']} (NEUTRAL)"
                )

            if bridge_sig["signal"] == "BUY":
                buy_count += 1
            elif bridge_sig["signal"] == "SELL":
                sell_count += 1
            else:
                hold_count += 1

        self.logger.info(
            f"  🎯 {symbol} Ensemble | "
            f"Consensus: {decision.consensus_score:+.3f} | "
            f"BUY:{buy_count} SELL:{sell_count} HOLD:{hold_count}"
        )

        for sig in decision.individual_signals:
            self.logger.info(
                f"      {sig.strategy_name:18s} → {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) {sig.reason}"
            )
        if bridge_signal_str:
            self.logger.info(
                f"      {'SIGNAL_MONITOR':18s} → {bridge_signal_str:4s} (6th strategy)"
            )

        # Convert ensemble decision to TradeSignal
        if decision.final_signal == AST.BUY:
            final_sig = SignalType.BUY
        elif decision.final_signal == AST.SELL:
            final_sig = SignalType.SELL
        else:
            final_sig = SignalType.HOLD

        if final_sig != SignalType.HOLD:
            # ── v2.1: Risk Shield check (before BUY only) ──
            if final_sig == SignalType.BUY and self.risk_shield is not None:
                current_holdings = list(self.positions.keys())
                risk_result = self.risk_shield.full_check(symbol, current_holdings)

                if risk_result.action == self._risk_action.BLOCK:
                    self.logger.info(
                        f"  🛡️ RISK SHIELD BLOCKED! {symbol} buy rejected"
                    )
                    for reason in risk_result.reasons:
                        self.logger.info(f"      → {reason}")

                    # Convert BUY to HOLD
                    final_sig = SignalType.HOLD
                    self.logger.info(
                        f"  ⚪ {symbol} HOLD (risk shield) | "
                        f"Beta: {risk_result.beta:.2f} | "
                        f"Misses: {risk_result.earnings_miss_count}/4"
                    )
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals

                elif risk_result.action == self._risk_action.REDUCE:
                    self.logger.info(
                        f"  ⚠️ RISK SHIELD WARNING: {symbol} position reduction recommended"
                    )
                    for reason in risk_result.reasons:
                        self.logger.info(f"      → {reason}")

                # Log beta-adjusted investment
                if risk_result.beta != 1.0:
                    self.logger.info(
                        f"  📏 Beta adjustment: {symbol} Beta={risk_result.beta:.2f} → "
                        f"Investment ${risk_result.adjusted_investment:,.0f} "
                        f"(vs default {risk_result.adjusted_investment/10000:.0%})"
                    )

            # ── v2.3: Signal Bridge - BEAR brake (before BUY) ──
            if final_sig == SignalType.BUY and self.signal_bridge is not None:
                blocked, brake_reason = self.signal_bridge.should_block_buy()
                if blocked:
                    self.logger.info(f"  {brake_reason}")
                    final_sig = SignalType.HOLD
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals

            # ── v2.3: Signal Bridge - Washout prevention ──
            if final_sig != SignalType.HOLD and self.signal_bridge is not None:
                ma_vals = self.analyzer.get_ma_values(
                    close_prices,
                    self.config.ma_short_period,
                    self.config.ma_long_period
                )
                ma_short = ma_vals.get("ma_short", 0) if ma_vals else 0
                ma_long = ma_vals.get("ma_long", 0) if ma_vals else 0

                washout = self.signal_bridge.check_washout(
                    symbol, ma_short, ma_long, current_price
                )
                if not washout["allowed"]:
                    self.logger.info(
                        f"  🔄 Washout blocked! {symbol} | {washout['reason']}"
                    )
                    final_sig = SignalType.HOLD
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals

            # Save stop loss/take profit
            self.active_stops[symbol] = {
                "sl": decision.stop_loss_price,
                "tp": decision.take_profit_price,
                "trail_high": current_price,
                "atr": decision.atr_value,
            }

            signals.append(TradeSignal(
                symbol=symbol,
                signal=final_sig,
                strategy="ENSEMBLE",
                price=current_price,
                reason=decision.reason,
            ))

            self.logger.info(
                f"  🛡️ Risk: SL=${decision.stop_loss_price:.2f} | "
                f"TP=${decision.take_profit_price:.2f} | "
                f"ATR=${decision.atr_value:.2f}"
            )
        else:
            self.logger.info(
                f"  ⚪ {symbol} HOLD (ensemble consensus not met) | "
                f"${current_price:.2f} | {decision.reason}"
            )

        # Check stop loss/take profit for existing positions
        self._check_stop_levels(symbol, current_price, signals)

        return signals

    def _check_stop_levels(self, symbol: str, current_price: float, signals: list):
        """Check if active positions hit stop loss/take profit levels"""
        if symbol not in self.active_stops:
            return

        stops = self.active_stops[symbol]
        sl = stops.get("sl", 0)
        tp = stops.get("tp", 0)
        trail_high = stops.get("trail_high", current_price)

        # Update trailing stop
        if current_price > trail_high:
            stops["trail_high"] = current_price
            # Trail the SL upward as well
            atr = stops.get("atr", 0)
            if atr > 0:
                new_sl = current_price - atr * 1.5
                if new_sl > sl:
                    stops["sl"] = new_sl
                    self.logger.info(
                        f"  📈 {symbol} Trailing SL updated: "
                        f"${sl:.2f} → ${new_sl:.2f}"
                    )

        # Stop loss hit
        if sl > 0 and current_price <= sl:
            signals.append(TradeSignal(
                symbol=symbol,
                signal=SignalType.SELL,
                strategy="ATR_STOP_LOSS",
                price=current_price,
                reason=f"🛑 Stop loss hit! ${current_price:.2f} ≤ SL ${sl:.2f}",
            ))
            del self.active_stops[symbol]

        # Take profit hit
        elif tp > 0 and current_price >= tp:
            signals.append(TradeSignal(
                symbol=symbol,
                signal=SignalType.SELL,
                strategy="ATR_TAKE_PROFIT",
                price=current_price,
                reason=f"🎯 Take profit hit! ${current_price:.2f} ≥ TP ${tp:.2f}",
            ))
            del self.active_stops[symbol]

    def _log_hold(self, symbol, close_prices, current_price, pct_change):
        """Log HOLD status"""
        ma_info = self.analyzer.get_ma_values(
            close_prices,
            self.config.ma_short_period,
            self.config.ma_long_period
        )
        self.logger.info(
            f"  ⚪ {symbol} HOLD | "
            f"${current_price:.2f} | "
            f"MA Spread: {ma_info.get('spread', 'N/A')} | "
            f"{self.config.pct_lookback_days}-day change: {pct_change:+.2f}%"
        )

    # ── Order Execution ─────────────────────────────────────────────

    def execute_signal(self, signal: TradeSignal) -> bool:
        """
        Execute order based on signal
        - AUTO mode: Send actual order
        - ALERT mode: Log only
        """
        self.logger.info(f"  📡 Signal detected: {signal}")
        self.signals_history.append(signal)

        # ALERT mode -> do not execute order
        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  ℹ️  [ALERT mode] Order not executed. "
                f"To enable auto trading, set trade_mode='auto'."
            )
            return False

        # AUTO mode -> execute actual order
        try:
            contract = Stock(signal.symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # Check position size
            current_qty = self.positions.get(signal.symbol, {}).get("quantity", 0)

            if signal.signal == SignalType.BUY:
                # v2.2: Wash Sale check
                if self.tax_optimizer is not None:
                    wash = self.tax_optimizer.check_buy_allowed(signal.symbol)
                    if wash.get("blocked"):
                        self.logger.warning(
                            f"  🚫 Wash Sale blocked! {signal.symbol} buy not allowed | "
                            f"{wash.get('reason', '')}"
                        )
                        return False
                    if wash.get("warning"):
                        self.logger.info(f"  ⚠️ {wash.get('reason', '')}")

                # Check max position size exceeded
                if current_qty + self.config.default_quantity > self.config.max_position_size:
                    self.logger.warning(
                        f"  ⚠️  {signal.symbol}: Max position size "
                        f"({self.config.max_position_size}) exceeded! Order skipped"
                    )
                    return False

                order = MarketOrder("BUY", self.config.default_quantity)

            elif signal.signal == SignalType.SELL:
                # Skip if no holdings
                if current_qty <= 0:
                    self.logger.warning(
                        f"  ⚠️  {signal.symbol}: No holdings! Sell skipped"
                    )
                    return False

                sell_qty = min(self.config.default_quantity, int(current_qty))
                order = MarketOrder("SELL", sell_qty)
            else:
                return False

            # Submit order
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  ✅ Order submitted! {signal.signal.value} "
                f"{signal.symbol} x{order.totalQuantity} "
                f"| Order ID: {trade.order.orderId} "
                f"| Status: {trade.orderStatus.status}"
            )

            # v2.2: Tax record
            if self.tax_optimizer is not None:
                if signal.signal == SignalType.BUY:
                    self.tax_optimizer.on_buy(
                        signal.symbol, signal.price, int(order.totalQuantity)
                    )
                elif signal.signal == SignalType.SELL:
                    tax_result = self.tax_optimizer.on_sell(
                        signal.symbol, signal.price, int(order.totalQuantity)
                    )
                    if tax_result.get("wash_sale_started"):
                        self.logger.info(
                            f"  ⏰ {signal.symbol} Wash Sale 30-day countdown started"
                        )

            # v2.3: Washout cooldown record
            if self.signal_bridge is not None:
                self.signal_bridge.record_trade(signal.symbol)

            signal.executed = True
            return True

        except Exception as e:
            self.logger.error(f"  ❌ Order execution failed: {e}")
            return False

    # ── Dashboard ──────────────────────────────────────────────

    def print_dashboard(self):
        """Print current status dashboard"""
        now = datetime.now()

        print("\n")
        print("╔" + "═" * 68 + "╗")
        print(f"║  📊 IB Smart Trader Dashboard       {now:%Y-%m-%d %H:%M:%S}  ║")
        print(f"║  Mode: {'🤖 AUTO (auto trading)' if self.config.trade_mode == TradeMode.AUTO else '🔔 ALERT (alerts only)':42s}  ║")
        print("╠" + "═" * 68 + "╣")

        # Strategy settings
        print(f"║  📈 MA Crossover: MA{self.config.ma_short_period} / MA{self.config.ma_long_period}" + " " * 35 + "║")
        print(f"║  📉 % Change: Buy ≤ {self.config.buy_drop_pct}% | Sell ≥ +{self.config.sell_rise_pct}% ({self.config.pct_lookback_days}d)" + " " * 11 + "║")
        print("╠" + "═" * 68 + "╣")

        # Held positions
        print("║  💼 Holdings:" + " " * 54 + "║")
        if self.positions:
            for sym, pos in self.positions.items():
                pnl = pos.get("unrealized_pnl", 0)
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                line = (
                    f"║    {pnl_icon} {sym:6s} | "
                    f"{int(pos['quantity']):4d} shares | "
                    f"Avg: ${pos['avg_cost']:8.2f} | "
                    f"P&L: ${pnl:+10,.2f}"
                )
                print(f"{line:<69s}║")
        else:
            print("║    (None)" + " " * 58 + "║")

        print("╠" + "═" * 68 + "╣")

        # v2.3: Signal Monitor status
        if self.signal_bridge is not None:
            sig = self.signal_bridge.get_composite_signal()
            icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
            brake_str = ""
            blocked, _ = self.signal_bridge.should_block_buy()
            if blocked:
                brake_str = " | 🛑 Buy halted"
            line = (
                f"║  📡 Signal Monitor: {icon.get(sig.composite, '⚪')} {sig.composite} "
                f"({sig.confidence:.0%}) | P/C: {sig.pc_ratio:.2f}{brake_str}"
            )
            print(f"{line:<69s}║")
            line2 = (
                f"║    COT: {sig.cot_signal} | Options: {sig.options_signal} | "
                f"Boost: {self.signal_bridge.get_ensemble_boost():+.2f}"
            )
            print(f"{line2:<69s}║")

        print("╠" + "═" * 68 + "╣")

        # Recent signals
        print("║  📡 Recent signals (max 10):" + " " * 39 + "║")
        recent = self.signals_history[-10:] if self.signals_history else []
        if recent:
            for sig in recent:
                status = "✅" if sig.executed else "⏳"
                line = (
                    f"║    {status} {sig.signal.value} {sig.symbol:6s} "
                    f"${sig.price:8.2f} | {sig.strategy:12s} "
                    f"| {sig.timestamp:%H:%M}"
                )
                print(f"{line:<69s}║")
        else:
            print("║    (No signals)" + " " * 52 + "║")

        print("╚" + "═" * 68 + "╝")

    # ── Main Loop ─────────────────────────────────────────────

    def run(self, symbols: list[str] = None):
        """
        Run main monitoring loop

        Usage:
            trader = SmartTrader(config)
            trader.connect()
            trader.run(["AAPL", "MSFT", "GOOGL", "TSLA"])
        """
        if not self.ib.isConnected():
            if not self.connect():
                return

        # Set watchlist
        if symbols:
            self.set_watchlist(symbols)

        if not self.watchlist:
            self.logger.error("❌ Watchlist is empty!")
            return

        # Load portfolio
        self.load_portfolio()

        self.running = True
        self.logger.info(
            f"\n🚀 Monitoring started! "
            f"({len(self.watchlist)} symbols, "
            f"{self.config.check_interval_sec}s interval)\n"
            f"   Ctrl+C to stop\n"
        )

        cycle = 0
        try:
            while self.running:
                cycle += 1
                self.logger.info(f"\n{'─' * 50}")
                self.logger.info(f"🔄 Cycle #{cycle} started [{datetime.now():%H:%M:%S}]")
                self.logger.info(f"{'─' * 50}")

                # Refresh positions
                self.load_portfolio()

                # Analyze each symbol
                all_signals = []
                for contract in self.watchlist:
                    self.logger.info(f"\n  🔍 Analyzing: {contract.symbol}")
                    signals = self.analyze_stock(contract)
                    all_signals.extend(signals)

                    # Execute signals
                    for signal in signals:
                        self.execute_signal(signal)

                    # API rate limit prevention
                    self.ib.sleep(1)

                # Print dashboard
                self.print_dashboard()

                # Wait until next cycle (auto-adjust for market/off-hours)
                from signal_bridge import is_market_open
                if is_market_open():
                    interval = self.config.check_interval_sec
                    label = "Market hours"
                else:
                    interval = self.config.check_interval_off_sec
                    label = "Off-hours"
                self.logger.info(
                    f"\n⏰ Next check: in {interval}s ({label})..."
                )
                self.ib.sleep(interval)

        except KeyboardInterrupt:
            self.logger.info("\n\n🛑 Stopped by user")
        except Exception as e:
            self.logger.error(f"\n❌ Error occurred: {e}", exc_info=True)
        finally:
            self.stop()

    def stop(self):
        """Stop bot & cleanup"""
        self.running = False
        self.print_dashboard()
        self.disconnect()

        # Save signal history
        if self.signals_history:
            history_file = f"signals_{datetime.now():%Y%m%d_%H%M%S}.json"
            history_data = [
                {
                    "symbol": s.symbol,
                    "signal": s.signal.name,
                    "strategy": s.strategy,
                    "price": s.price,
                    "reason": s.reason,
                    "timestamp": s.timestamp.isoformat(),
                    "executed": s.executed,
                }
                for s in self.signals_history
            ]
            with open(history_file, "w") as f:
                json.dump(history_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"📁 Signal history saved: {history_file}")

        self.logger.info("👋 Smart Trader terminated")


# ═══════════════════════════════════════════════════════════════
#  Execution
# ═══════════════════════════════════════════════════════════════

def main():
    """Main execution function"""

    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║           🤖 IB Smart Trader v1.0                       ║
    ║                                                          ║
    ║  Interactive Brokers Auto Trading System                 ║
    ║  Strategy: MA Crossover + % Change Based                 ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    # ── Load or create config ──
    config = TradingConfig(
        # IB connection
        ib_host="127.0.0.1",
        ib_port=7497,            # Paper Trading (Live: 7496)
        client_id=1,

        # Mode selection - recommend testing with ALERT first!
        trade_mode=TradeMode.ALERT,

        # MA Crossover settings
        ma_short_period=10,      # 10-day moving average
        ma_long_period=30,       # 30-day moving average

        # % Change settings
        buy_drop_pct=-5.0,       # Buy on 5% drop
        sell_rise_pct=5.0,       # Sell on 5% rise
        pct_lookback_days=5,     # Compare vs 5 days ago

        # Order settings
        default_quantity=10,     # Default 10 shares
        max_position_size=100,   # Max 100 shares

        # Monitoring
        check_interval_sec=60,   # Check every 60 seconds
    )

    # Save config
    config.save("config.json")

    # ── Symbols to monitor ──
    watchlist = [
        "AAPL",    # Apple
        "MSFT",    # Microsoft
        "GOOGL",   # Alphabet
        "TSLA",    # Tesla
        "AMZN",    # Amazon
        "NVDA",    # NVIDIA
        "META",    # Meta
    ]

    # ── Run trader ──
    trader = SmartTrader(config)

    if trader.connect():
        trader.run(watchlist)
    else:
        print("\n  TWS connection failed. Please refer to the instructions above.")


if __name__ == "__main__":
    main()
