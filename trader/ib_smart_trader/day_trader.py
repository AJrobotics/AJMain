"""
═══════════════════════════════════════════════════════════════════
  Day Trader v1.0 - IB Day Trading Automated Engine

  Scalping (1-5 min) + Intraday (15 min - 1 hour) Hybrid Strategy
  Targets high-liquidity large caps + pre-market hot stocks

  Module Integration:
    - day_strategies.py — VWAP, EMA, Volume, RSI+MACD ensemble
    - day_risk.py      — Daily risk management, EOD liquidation
    - signal_bridge.py — Market sentiment (optional)

  Execution:
    python run.py --day                # ALERT mode
    python run.py --day --auto         # AUTO mode (automated trading)
    python run.py --day --daemon       # Auto-start daily
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

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
    HAS_IB = True
except ImportError as e:
    print(f"  Required packages need to be installed: pip install ib_insync pandas numpy")
    print(f"  Missing: {e}")
    HAS_IB = False


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

class TradeMode(Enum):
    AUTO = "auto"
    ALERT = "alert"


@dataclass
class DayTraderConfig:
    """Day Trader full configuration"""

    # ── IB Connection ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # Paper: 7497, Live: 7496
    client_id: int = 3           # Separate from existing SmartTrader(1), Screener(2)

    # ── Mode ──
    trade_mode: TradeMode = TradeMode.ALERT

    # ── Capital ──
    capital: float = 75_000.0

    # ── Pre-market Scanner ──
    core_watchlist: list = field(default_factory=lambda: [
        "NVDA", "AAPL", "TSLA", "AMD", "META",
        "AMZN", "GOOGL", "MSFT", "QQQ", "SPY",
    ])
    scanner_gap_threshold_pct: float = 2.0    # Pre-market gap minimum ±2%
    scanner_max_hot_stocks: int = 5           # Max hot stocks to add from pre-market

    # ── Bar Settings ──
    primary_bar_size: str = "5 mins"          # Primary analysis timeframe
    scalp_bar_size: str = "1 min"             # Scalping timeframe
    history_duration: str = "1 D"             # Current day data
    analysis_interval_sec: int = 30           # Analysis interval (seconds)

    # ── Order Settings ──
    use_limit_orders: bool = False            # True: limit order, False: market order
    limit_offset_pct: float = 0.05            # Limit order offset (%)

    # ── Logging ──
    log_file: str = "day_trader.log"


# ═══════════════════════════════════════════════════════════════
#  Pre-market Scanner
# ═══════════════════════════════════════════════════════════════

class PremarketScanner:
    """
    Pre-market liquidity stock scanner

    1. Fixed core list (always included)
    2. Add pre-market gap + volume spike stocks via IB Scanner
    """

    def __init__(self, ib: 'IB', config: DayTraderConfig):
        self.ib = ib
        self.config = config
        self.logger = logging.getLogger("PremarketScanner")

    def scan(self) -> list[str]:
        """Pre-market scan -> return final watchlist"""
        watchlist = list(self.config.core_watchlist)
        self.logger.info(f"📋 Core watchlist: {watchlist}")

        # Add hot stocks via IB Scanner
        hot_stocks = self._scan_premarket_movers()
        for sym in hot_stocks:
            if sym not in watchlist:
                watchlist.append(sym)
                self.logger.info(f"  🔥 Hot stock added: {sym}")

        self.logger.info(f"📋 Final watchlist ({len(watchlist)} stocks): {watchlist}")
        return watchlist

    def _scan_premarket_movers(self) -> list[str]:
        """Scan pre-market gap stocks via IB Scanner API"""
        hot = []
        try:
            # IB Scanner: top pre-market gap stocks
            scan_params = ScannerSubscription(
                instrument="STK",
                locationCode="STK.US.MAJOR",
                scanCode="TOP_PERC_GAIN",
                numberOfRows=20,
                abovePrice=10.0,          # $10 and above
                aboveVolume=100000,        # Volume 100K+
            )
            scan_data = self.ib.reqScannerData(scan_params)

            for item in scan_data[:self.config.scanner_max_hot_stocks]:
                sym = item.contractDetails.contract.symbol
                hot.append(sym)

            self.ib.cancelScannerSubscription(scan_data)
            self.logger.info(f"  🔍 IB Scanner: {len(hot)} hot stocks found")

        except Exception as e:
            self.logger.warning(f"  ⚠️ Pre-market scan failed (using core list only): {e}")

        return hot


# ═══════════════════════════════════════════════════════════════
#  Main Day Trader
# ═══════════════════════════════════════════════════════════════

class DayTrader:
    """IB Day Trading Automated Engine"""

    def __init__(self, config: DayTraderConfig = None):
        self.config = config or DayTraderConfig()
        self.ib = IB() if HAS_IB else None
        self.running = False
        self.watchlist: list = []       # IB Contract list
        self.watchlist_symbols: list[str] = []
        self.positions: dict = {}       # Track only Day Trader's own positions (not all IB positions)
        self.signals_history: list = []

        # Strategy engine
        self.ensemble = None
        self.strategy_config = None
        try:
            from day_strategies import DayStrategyEnsemble, DayStrategyConfig
            self.strategy_config = DayStrategyConfig()
            self.ensemble = DayStrategyEnsemble(self.strategy_config)
        except ImportError:
            print("  ⚠️ day_strategies.py not found")

        # Risk manager
        self.risk_manager = None
        try:
            from day_risk import DayRiskManager, DayRiskConfig
            self.risk_manager = DayRiskManager(
                DayRiskConfig(capital=self.config.capital)
            )
        except ImportError:
            print("  ⚠️ day_risk.py not found")

        # Signal Bridge (optional)
        self.signal_bridge = None
        try:
            from signal_bridge import SignalBridge, SignalBridgeConfig
            self.signal_bridge = SignalBridge(SignalBridgeConfig())
        except ImportError:
            pass  # Optional module

        self._setup_logging()

    def _setup_logging(self):
        self.logger = logging.getLogger("DayTrader")
        self.logger.setLevel(logging.INFO)

        fh = logging.FileHandler(self.config.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S"
        ))

        if not self.logger.handlers:
            self.logger.addHandler(fh)
            self.logger.addHandler(ch)

    # ── IB Connection ───────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to IB Gateway/TWS"""
        if not HAS_IB:
            self.logger.error("❌ ib_insync not installed")
            return False

        self.logger.info("=" * 60)
        self.logger.info("  🏎️ Day Trader v1.0 Starting")
        self.logger.info(f"  Mode: {self.config.trade_mode.value.upper()}")
        self.logger.info(f"  Capital: ${self.config.capital:,.0f}")
        self.logger.info("=" * 60)

        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id,
            )
            accounts = self.ib.managedAccounts()
            self.logger.info(f"✅ IB connection successful | Account: {accounts}")
            return True
        except Exception as e:
            self.logger.error(f"❌ IB connection failed: {e}")
            return False

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            self.logger.info("🔌 IB disconnected")

    # ── Watchlist ────────────────────────────────────────────

    def setup_watchlist(self, symbols: list[str] = None):
        """Set up watchlist (pre-market scan or manual)"""
        if symbols is None:
            # Run pre-market scanner
            scanner = PremarketScanner(self.ib, self.config)
            symbols = scanner.scan()

        self.watchlist = []
        self.watchlist_symbols = []

        for sym in symbols:
            try:
                contract = Stock(sym, "SMART", "USD")
                self.ib.qualifyContracts(contract)
                self.watchlist.append(contract)
                self.watchlist_symbols.append(sym)
            except Exception as e:
                self.logger.warning(f"  ⚠️ {sym} contract validation failed: {e}")

        self.logger.info(f"👀 Watchlist: {self.watchlist_symbols}")

    def load_portfolio(self):
        """Load only Day Trader's own positions (based on risk_manager).

        ⚠️ Important: IB portfolio (ib.portfolio()) returns all positions in the account,
        which mixes with positions from other strategies like Smart Trader.
        Day Trader must only reference risk_manager.positions
        to manage only the positions it directly bought.
        """
        # Read IB full portfolio for reference only (do not store in self.positions)
        self.positions = {}
        if self.risk_manager and self.risk_manager.positions:
            for sym, pos in self.risk_manager.positions.items():
                self.positions[sym] = {
                    "contract": None,
                    "quantity": pos.quantity,
                    "avg_cost": pos.entry_price,
                    "market_value": 0,
                    "unrealized_pnl": 0,
                }

    # ── Data Collection ───────────────────────────────────────────

    def get_intraday_bars(self, contract, bar_size: str = None) -> Optional[pd.DataFrame]:
        """Collect intraday bar data"""
        if bar_size is None:
            bar_size = self.config.primary_bar_size

        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.history_duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if not bars:
                return None

            df = util.df(bars)
            df.set_index("date", inplace=True)
            return df
        except Exception as e:
            self.logger.error(f"❌ {contract.symbol} bar data request failed: {e}")
            return None

    def get_current_price(self, contract) -> Optional[float]:
        """Get current price"""
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(1)
            price = ticker.marketPrice()
            if price and not pd.isna(price):
                return float(price)
            price = ticker.last
            if price and not pd.isna(price):
                return float(price)
            return None
        except Exception:
            return None

    # ── Analysis & Trading ──────────────────────────────────────

    def analyze_stock(self, contract) -> Optional[dict]:
        """Analyze single stock intraday bars"""
        symbol = contract.symbol
        df = self.get_intraday_bars(contract)

        if df is None or len(df) < 30:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Morning session check
        is_morning = False
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            is_morning = now_et.hour < 11 or (now_et.hour == 10 and now_et.minute <= 30)
        except Exception:
            pass

        # Ensemble analysis
        if self.ensemble is None:
            return None

        decision = self.ensemble.analyze(symbol, close, high, low, volume, is_morning)

        # Signal Bridge check (optional)
        bridge_blocked = False
        if self.signal_bridge and decision.final_signal.name == "BUY":
            blocked, reason = self.signal_bridge.should_block_buy()
            if blocked:
                self.logger.info(f"  📡 {reason}")
                bridge_blocked = True

        return {
            "symbol": symbol,
            "contract": contract,
            "decision": decision,
            "bridge_blocked": bridge_blocked,
            "current_price": float(close.iloc[-1]),
        }

    def process_signal(self, analysis: dict):
        """Analysis result -> execute order"""
        decision = analysis["decision"]
        symbol = analysis["symbol"]
        price = analysis["current_price"]

        # Logging
        buy_count = sum(1 for s in decision.individual_signals if s.signal.name == "BUY")
        sell_count = sum(1 for s in decision.individual_signals if s.signal.name == "SELL")

        self.logger.info(
            f"  🎯 {symbol} | {decision.final_signal.value} | "
            f"Consensus: {decision.consensus_score:+.3f} | "
            f"BUY:{buy_count} SELL:{sell_count}"
        )
        for sig in decision.individual_signals:
            self.logger.info(
                f"      {sig.strategy_name:15s} → {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) {sig.reason}"
            )

        if decision.final_signal.name == "HOLD":
            return

        if analysis.get("bridge_blocked"):
            return

        # Risk check
        if self.risk_manager:
            risk = self.risk_manager.check_risk(symbol, price)

            # Must liquidate all positions
            if risk.must_close_all:
                self.logger.warning(f"  🔴 {risk.level.value} — Full liquidation order")
                self._liquidate_all()
                return

            # Close individual stock
            for sym in risk.must_close_symbols:
                self.logger.warning(f"  🔴 {sym} forced liquidation")
                self._close_position(sym)

            # Cannot open new position
            if not risk.can_open_new and decision.final_signal.name == "BUY":
                self.logger.info(f"  🟡 Cannot open new position: {', '.join(risk.reasons)}")
                return

        # Execute order
        if decision.final_signal.name == "BUY":
            self._execute_buy(analysis)
        elif decision.final_signal.name == "SELL":
            self._execute_sell(analysis)

    def _execute_buy(self, analysis: dict):
        """Execute buy order"""
        symbol = analysis["symbol"]
        price = analysis["current_price"]
        decision = analysis["decision"]

        # Skip if already holding
        if symbol in (self.risk_manager.positions if self.risk_manager else {}):
            self.logger.info(f"  ⚪ {symbol} already held — skipping buy")
            return

        # Position sizing
        shares = 10  # Default
        if self.risk_manager:
            stop_distance = abs(price - decision.stop_loss_price) if decision.stop_loss_price > 0 else 0
            sizing = self.risk_manager.calculate_position_size(
                symbol, price, stop_distance=stop_distance,
            )
            shares = sizing["shares"]
            self.logger.info(
                f"  📏 Sizing: {shares} shares × ${price:.2f} = "
                f"${sizing['dollar_amount']:,.2f} | {sizing['method']}"
            )

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  🔔 [ALERT] BUY {symbol} x{shares} @ ${price:.2f} | "
                f"SL=${decision.stop_loss_price:.2f} TP=${decision.take_profit_price:.2f}"
            )
            return

        # AUTO mode: actual order
        try:
            contract = analysis["contract"]
            if self.config.use_limit_orders:
                limit_price = round(price * (1 + self.config.limit_offset_pct / 100), 2)
                order = LimitOrder("BUY", shares, limit_price)
            else:
                order = MarketOrder("BUY", shares)

            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  ✅ BUY order sent! {symbol} x{shares} | "
                f"OrderID: {trade.order.orderId} | Status: {trade.orderStatus.status}"
            )

            # Record position in risk manager
            if self.risk_manager:
                self.risk_manager.open_position(
                    symbol, "LONG", price, shares,
                    stop_loss=decision.stop_loss_price,
                    take_profit=decision.take_profit_price,
                )

        except Exception as e:
            self.logger.error(f"  ❌ BUY order failed: {e}")

    def _execute_sell(self, analysis: dict):
        """Execute sell order — only sell positions bought by Day Trader"""
        symbol = analysis["symbol"]
        price = analysis["current_price"]

        # ⚠️ Key: only sell Day Trader's own positions recorded in risk_manager
        # Do not reference IB full portfolio (self.positions)
        if not self.risk_manager or symbol not in self.risk_manager.positions:
            return  # Skip unconditionally if Day Trader never bought it

        current_qty = self.risk_manager.positions[symbol].quantity
        if current_qty <= 0:
            return

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  🔔 [ALERT] SELL {symbol} x{current_qty} @ ${price:.2f}"
            )
            return

        try:
            contract = analysis["contract"]
            order = MarketOrder("SELL", int(current_qty))
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  ✅ SELL order sent! {symbol} x{current_qty} | "
                f"OrderID: {trade.order.orderId}"
            )

            if self.risk_manager:
                self.risk_manager.close_position(symbol, price)

        except Exception as e:
            self.logger.error(f"  ❌ SELL order failed: {e}")

    def _close_position(self, symbol: str):
        """Close position for a specific stock — Day Trader's own positions only"""
        if not HAS_IB:
            return

        # Skip if Day Trader never bought it
        if not self.risk_manager or symbol not in self.risk_manager.positions:
            self.logger.info(f"  ⚪ {symbol} not a Day Trader position — skipping liquidation")
            return

        qty = self.risk_manager.positions[symbol].quantity
        if qty <= 0:
            return

        price = None
        for contract in self.watchlist:
            if contract.symbol == symbol:
                price = self.get_current_price(contract)
                if price and self.config.trade_mode == TradeMode.AUTO:
                    if qty > 0:
                        order = MarketOrder("SELL", int(qty))
                        self.ib.placeOrder(contract, order)
                        self.ib.sleep(1)
                if price and self.risk_manager:
                    self.risk_manager.close_position(symbol, price)
                break

    def _liquidate_all(self):
        """Force liquidate all positions"""
        self.logger.warning("  🔴🔴🔴 Force liquidating all positions!")

        if not self.risk_manager:
            return

        symbols = list(self.risk_manager.positions.keys())
        for symbol in symbols:
            self._close_position(symbol)
            self.logger.info(f"    Liquidated: {symbol}")

        self.logger.warning("  🔴🔴🔴 All positions liquidated")

    # ── Main Loop ─────────────────────────────────────────────

    def run(self, symbols: list[str] = None):
        """Main day trading loop"""
        if not self.ib or not self.ib.isConnected():
            if not self.connect():
                return

        # Set up watchlist
        self.setup_watchlist(symbols)
        if not self.watchlist:
            self.logger.error("❌ Watchlist is empty!")
            return

        # Load portfolio
        self.load_portfolio()

        # Daily reset for risk manager
        if self.risk_manager:
            self.risk_manager.reset_daily()

        self.running = True
        self.logger.info(
            f"\n🚀 Day trading started! "
            f"Watchlist: {self.watchlist_symbols} | "
            f"Analysis interval: {self.config.analysis_interval_sec}s"
        )

        try:
            while self.running:
                # Check market status
                if not self._is_market_open():
                    self.logger.info("  💤 Market closed — waiting...")
                    self.ib.sleep(60)
                    continue

                # Risk check (EOD etc.)
                if self.risk_manager:
                    risk = self.risk_manager.check_risk()
                    if risk.must_close_all:
                        self._liquidate_all()
                        self.logger.info("  ⏰ EOD liquidation complete — exiting")
                        break

                # Analyze all stocks
                self._scan_cycle()

                # Dashboard
                self.print_dashboard()

                # Wait
                self.ib.sleep(self.config.analysis_interval_sec)

        except KeyboardInterrupt:
            self.logger.info("\n⛔ User interrupted")
        finally:
            # Handle remaining positions
            if self.risk_manager and self.risk_manager.positions:
                self.logger.warning(f"  ⚠️ {len(self.risk_manager.positions)} remaining positions")
                if self.config.trade_mode == TradeMode.AUTO:
                    self._liquidate_all()

            self.running = False
            self.disconnect()

    def _scan_cycle(self):
        """Full watchlist single analysis cycle"""
        self.logger.info(f"\n{'─' * 60}")
        self.logger.info(
            f"  🔄 Scan cycle | {datetime.now():%H:%M:%S} | "
            f"Watchlist: {len(self.watchlist_symbols)} stocks"
        )

        # Update current prices
        if self.risk_manager and self.risk_manager.positions:
            price_map = {}
            for contract in self.watchlist:
                sym = contract.symbol
                if sym in self.risk_manager.positions:
                    price = self.get_current_price(contract)
                    if price:
                        price_map[sym] = price
            self.risk_manager.update_prices(price_map)

        # Analyze all stocks
        for contract in self.watchlist:
            try:
                analysis = self.analyze_stock(contract)
                if analysis:
                    self.process_signal(analysis)
            except Exception as e:
                self.logger.error(f"  ❌ {contract.symbol} analysis error: {e}")

    def _is_market_open(self) -> bool:
        """Check if US stock market is open"""
        try:
            from signal_bridge import is_market_open
            return is_market_open()
        except ImportError:
            pass

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.weekday() >= 5:
                return False
            market_open = now_et.replace(hour=9, minute=30, second=0)
            market_close = now_et.replace(hour=16, minute=0, second=0)
            return market_open <= now_et <= market_close
        except Exception:
            return True  # Run if unable to determine

    # ── Dashboard ──────────────────────────────────────────────

    def print_dashboard(self):
        """Day trading dashboard"""
        now = datetime.now()

        print("\n")
        print("╔" + "═" * 68 + "╗")
        print(f"║  🏎️ Day Trader Dashboard          {now:%Y-%m-%d %H:%M:%S}  ║")
        mode_str = '🤖 AUTO' if self.config.trade_mode == TradeMode.AUTO else '🔔 ALERT'
        print(f"║  Mode: {mode_str:50s}  ║")
        print("╠" + "═" * 68 + "╣")

        # Risk status
        if self.risk_manager:
            status = self.risk_manager.get_status()
            icon = "🟢" if status["daily_pnl"] >= 0 else "🔴"
            line = (
                f"║  {icon} PnL: ${status['daily_pnl']:+,.2f} "
                f"({status['daily_pnl_pct']:+.2f}%) | "
                f"Positions: {status['position_count']}/{status['max_positions']} | "
                f"Trades: {status['trade_count']}"
            )
            print(f"{line:<69s}║")

            # Position details
            if self.risk_manager.positions:
                for sym, pos in self.risk_manager.positions.items():
                    pnl_icon = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
                    line = (
                        f"║    {pnl_icon} {sym:6s} x{pos.quantity:4d} "
                        f"@ ${pos.entry_price:.2f} → ${pos.current_price:.2f} "
                        f"| ${pos.unrealized_pnl:+,.2f}"
                    )
                    print(f"{line:<69s}║")

        print("╚" + "═" * 68 + "╝")

    # ── Daemon Mode ─────────────────────────────────────────────

    def run_daemon(self):
        """Auto-start daemon that runs every morning"""
        self.logger.info("  🕐 Daemon mode — auto-run at market open daily")

        while True:
            try:
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                now_et = datetime.now()

            # Start preparation at 9:25 ET on weekdays
            if now_et.weekday() < 5 and now_et.hour == 9 and now_et.minute >= 25:
                self.logger.info(f"  🌅 Market open preparation ({now_et:%Y-%m-%d %H:%M})")
                self.run()
                self.logger.info("  🌙 Market closed — waiting until tomorrow")

            time.sleep(60)


# ═══════════════════════════════════════════════════════════════
#  Demo (logic test without IB)
# ═══════════════════════════════════════════════════════════════

def demo():
    """Offline demo"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🏎️ Day Trader v1.0 Demo (Offline)                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    # Strategy demo
    try:
        from day_strategies import DayStrategyEnsemble, DayStrategyConfig
        from day_strategies import demo as strategies_demo
        strategies_demo()
    except ImportError as e:
        print(f"  ⚠️ Strategy module load failed: {e}")

    print()

    # Risk demo
    try:
        from day_risk import demo as risk_demo
        risk_demo()
    except ImportError as e:
        print(f"  ⚠️ Risk module load failed: {e}")

    print("\n  ✅ Demo complete!")
    print("  Live execution: python run.py --day [--auto]")


if __name__ == "__main__":
    demo()
