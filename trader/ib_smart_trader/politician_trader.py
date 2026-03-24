"""
═══════════════════════════════════════════════════════════════════
  Politician Trader v1.0 - Congressional Trade Following Engine

  Follows US congressional stock trade disclosures + political events
  Swing (disclosure-based, days~weeks) + Day (event-based, intraday)

  Modules:
    - politician_data.py       — Disclosure/event data fetching
    - politician_strategies.py — 4-strategy ensemble
    - politician_risk.py       — Risk management

  Usage:
    python run.py --politician              # ALERT mode
    python run.py --politician --auto       # AUTO mode (live trading)
    python run.py --politician --daemon     # Daily auto-start
    python run.py --politician --demo       # Offline demo
═══════════════════════════════════════════════════════════════════
"""

import logging
import time
import json
import os
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
    HAS_IB = True
except ImportError as e:
    print(f"  Required packages: pip install ib_insync pandas numpy")
    print(f"  Missing: {e}")
    HAS_IB = False


# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

class TradeMode(Enum):
    AUTO = "auto"
    ALERT = "alert"


@dataclass
class PoliticianTraderConfig:
    """Politician Trader configuration"""

    # ── IB Connection ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # Paper: 7497, Live: 7496
    client_id: int = 4           # Smart=1, Screener=2, Day=3, Politician=4

    # ── Mode ──
    trade_mode: TradeMode = TradeMode.ALERT

    # ── Capital ──
    capital: float = 50_000.0

    # ── API Keys ──
    quiver_api_key: str = ""
    capitol_trades_api_key: str = ""

    # ── Scan Intervals ──
    scan_interval_min: int = 30          # Disclosure scan interval (min)
    event_scan_interval_min: int = 5     # Event scan interval (min)

    # ── Logging ──
    log_file: str = "politician_trader.log"


# ═══════════════════════════════════════════════════════════════
#  Main Trader
# ═══════════════════════════════════════════════════════════════

class PoliticianTrader:
    """Congressional trade following engine"""

    def __init__(self, config: PoliticianTraderConfig = None):
        self.config = config or PoliticianTraderConfig()
        self.ib = IB() if HAS_IB else None
        self.running = False
        self.contracts: dict[str, object] = {}

        # Data fetcher
        self.data_fetcher = None
        try:
            from politician_data import PoliticianDataFetcher, PoliticianDataConfig
            self.data_fetcher = PoliticianDataFetcher(PoliticianDataConfig(
                quiver_api_key=self.config.quiver_api_key,
                capitol_trades_api_key=self.config.capitol_trades_api_key,
            ))
        except ImportError:
            print("  Warning: politician_data.py not found")

        # Strategy engine
        self.ensemble = None
        self.strategy_config = None
        try:
            from politician_strategies import PoliticianStrategyEnsemble, PoliticianStrategyConfig
            self.strategy_config = PoliticianStrategyConfig()
            self.ensemble = PoliticianStrategyEnsemble(self.strategy_config)
        except ImportError:
            print("  Warning: politician_strategies.py not found")

        # Risk manager
        self.risk_manager = None
        try:
            from politician_risk import PoliticianRiskManager, PoliticianRiskConfig
            self.risk_manager = PoliticianRiskManager(
                PoliticianRiskConfig(capital=self.config.capital)
            )
        except ImportError:
            print("  Warning: politician_risk.py not found")

        # Politician profiles (refreshed periodically)
        self.profiles: dict = {}

        # Last scan times
        self._last_disclosure_scan: Optional[datetime] = None
        self._last_event_scan: Optional[datetime] = None

        self._setup_logging()

    def _setup_logging(self):
        self.logger = logging.getLogger("PoliticianTrader")
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

    # ── IB Connection ────────────────────────────────────────

    def connect(self) -> bool:
        if not HAS_IB:
            self.logger.error("ib_insync not installed")
            return False

        self.logger.info("=" * 60)
        self.logger.info("  Politician Trader v1.0 Starting")
        self.logger.info(f"  Mode: {self.config.trade_mode.value.upper()}")
        self.logger.info(f"  Capital: ${self.config.capital:,.0f}")
        self.logger.info(f"  Disclosure scan: every {self.config.scan_interval_min}min")
        self.logger.info("=" * 60)

        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id,
            )
            accounts = self.ib.managedAccounts()
            self.logger.info(f"IB Connected | Account: {accounts}")
            return True
        except Exception as e:
            self.logger.error(f"IB Connection failed: {e}")
            return False

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            self.logger.info("IB Disconnected")

    # ── Data Loading ─────────────────────────────────────────

    def _load_profiles(self):
        if self.data_fetcher:
            self.profiles = self.data_fetcher.build_politician_profiles()
            self.logger.info(f"Loaded {len(self.profiles)} politician profiles")

    def _qualify_contract(self, symbol: str) -> Optional[object]:
        if symbol in self.contracts:
            return self.contracts[symbol]

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            self.contracts[symbol] = contract
            return contract
        except Exception as e:
            self.logger.warning(f"  {symbol} contract qualification failed: {e}")
            return None

    def get_current_price(self, contract) -> Optional[float]:
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

    # ── Scan & Analyze ───────────────────────────────────────

    def _scan_disclosures(self):
        if not self.data_fetcher or not self.ensemble:
            return

        now = datetime.now()
        if (self._last_disclosure_scan and
                (now - self._last_disclosure_scan).total_seconds() < self.config.scan_interval_min * 60):
            return
        self._last_disclosure_scan = now

        self.logger.info(f"\n{'─' * 60}")
        self.logger.info(f"  Disclosure Scan | {now:%H:%M:%S}")

        disclosures = self.data_fetcher.fetch_recent_disclosures()
        actionable = self.data_fetcher.filter_actionable_disclosures(disclosures)

        if not actionable:
            self.logger.info("  No actionable disclosures — waiting")
            return

        symbols = list(set(d.symbol for d in actionable))
        self.logger.info(f"  Target symbols: {symbols}")

        events = self.data_fetcher.fetch_political_events()

        for symbol in symbols:
            try:
                contract = self._qualify_contract(symbol)
                if not contract:
                    continue

                price = self.get_current_price(contract)
                if not price:
                    self.logger.warning(f"  {symbol} price fetch failed")
                    continue

                decision = self.ensemble.analyze(
                    symbol, actionable, self.profiles, events, price
                )

                self._process_signal(decision, contract, price)

            except Exception as e:
                self.logger.error(f"  {symbol} analysis error: {e}")

    def _scan_events(self):
        if not self.data_fetcher or not self.ensemble:
            return

        now = datetime.now()
        if (self._last_event_scan and
                (now - self._last_event_scan).total_seconds() < self.config.event_scan_interval_min * 60):
            return
        self._last_event_scan = now

        events = self.data_fetcher.fetch_political_events()
        if not events:
            return

        self.logger.info(f"  Political events detected: {len(events)}")

        from politician_data import SECTOR_SYMBOLS
        symbols_to_check = set()
        for event in events:
            if event.impact_score < 0.5:
                continue
            for sector in event.affected_sectors:
                for sym in SECTOR_SYMBOLS.get(sector, [])[:3]:
                    symbols_to_check.add(sym)

        for symbol in symbols_to_check:
            try:
                contract = self._qualify_contract(symbol)
                if not contract:
                    continue
                price = self.get_current_price(contract)
                if not price:
                    continue

                decision = self.ensemble.analyze(
                    symbol, [], self.profiles, events, price
                )

                if decision.trade_mode == "day":
                    self._process_signal(decision, contract, price)

            except Exception as e:
                self.logger.error(f"  {symbol} event analysis error: {e}")

    # ── Signal Processing ────────────────────────────────────

    def _process_signal(self, decision, contract, price: float):
        symbol = decision.symbol

        buy_count = sum(1 for s in decision.individual_signals if s.signal.name == "BUY")
        sell_count = sum(1 for s in decision.individual_signals if s.signal.name == "SELL")

        self.logger.info(
            f"  {symbol} | {decision.final_signal.value} | "
            f"Consensus: {decision.consensus_score:+.3f} | "
            f"BUY:{buy_count} SELL:{sell_count} | "
            f"Mode: {decision.trade_mode}"
        )
        for sig in decision.individual_signals:
            self.logger.info(
                f"      {sig.strategy_name:22s} -> {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) {sig.reason[:60]}"
            )

        if decision.final_signal.name == "HOLD":
            return

        # Risk check
        if self.risk_manager:
            politician = decision.source_disclosure.get("politician", "")
            sector = ""
            relevant = [d for d in (self.data_fetcher.fetch_recent_disclosures() if self.data_fetcher else [])
                       if d.symbol == symbol]
            if relevant:
                sector = relevant[0].sector

            risk = self.risk_manager.check_risk(
                symbol, trade_mode=decision.trade_mode,
                politician=politician, sector=sector,
            )

            if risk.must_close_all:
                self.logger.warning(f"  {risk.level.value} — Liquidating all positions")
                self._liquidate_all()
                return

            for sym in risk.must_close_symbols:
                self.logger.warning(f"  Force closing {sym}")
                self._close_position(sym)

            if not risk.can_open_new and decision.final_signal.name == "BUY":
                self.logger.info(f"  New entry blocked: {', '.join(risk.reasons)}")
                return

        if decision.final_signal.name == "BUY":
            self._execute_buy(decision, contract, price)
        elif decision.final_signal.name == "SELL":
            self._execute_sell(decision, contract, price)

    def _execute_buy(self, decision, contract, price: float):
        symbol = decision.symbol

        if symbol in (self.risk_manager.positions if self.risk_manager else {}):
            self.logger.info(f"  {symbol} already held — skip")
            return

        shares = 10
        if self.risk_manager:
            stop_distance = abs(price - decision.stop_loss_price) if decision.stop_loss_price > 0 else 0
            sizing = self.risk_manager.calculate_position_size(
                symbol, price, trade_mode=decision.trade_mode,
                stop_distance=stop_distance,
            )
            shares = sizing["shares"]
            self.logger.info(
                f"  Sizing: {shares} shares x ${price:.2f} = "
                f"${sizing['dollar_amount']:,.2f} | {sizing['method']}"
            )

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  [ALERT] BUY {symbol} x{shares} @ ${price:.2f} | "
                f"SL=${decision.stop_loss_price:.2f} TP=${decision.take_profit_price:.2f} | "
                f"Mode: {decision.trade_mode}"
            )
            return

        # AUTO mode
        try:
            order = MarketOrder("BUY", shares)
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  BUY order sent! {symbol} x{shares} | "
                f"OrderID: {trade.order.orderId} | Status: {trade.orderStatus.status}"
            )

            if self.risk_manager:
                politician = decision.source_disclosure.get("politician", "")
                sector = ""
                if self.data_fetcher:
                    relevant = [d for d in self.data_fetcher.fetch_recent_disclosures()
                               if d.symbol == symbol]
                    if relevant:
                        sector = relevant[0].sector

                self.risk_manager.open_position(
                    symbol, "LONG", price, shares,
                    trade_mode=decision.trade_mode,
                    politician=politician, sector=sector,
                    stop_loss=decision.stop_loss_price,
                    take_profit=decision.take_profit_price,
                    hold_period_days=decision.hold_period_days,
                )

        except Exception as e:
            self.logger.error(f"  BUY order failed: {e}")

    def _execute_sell(self, decision, contract, price: float):
        symbol = decision.symbol

        if not self.risk_manager or symbol not in self.risk_manager.positions:
            return

        current_qty = self.risk_manager.positions[symbol].quantity
        if current_qty <= 0:
            return

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  [ALERT] SELL {symbol} x{current_qty} @ ${price:.2f}"
            )
            return

        try:
            order = MarketOrder("SELL", int(current_qty))
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  SELL order sent! {symbol} x{current_qty} | "
                f"OrderID: {trade.order.orderId}"
            )

            if self.risk_manager:
                self.risk_manager.close_position(symbol, price)

        except Exception as e:
            self.logger.error(f"  SELL order failed: {e}")

    def _close_position(self, symbol: str):
        if not self.risk_manager or symbol not in self.risk_manager.positions:
            return

        qty = self.risk_manager.positions[symbol].quantity
        if qty <= 0:
            return

        contract = self.contracts.get(symbol)
        if not contract:
            return

        price = self.get_current_price(contract)
        if price and self.config.trade_mode == TradeMode.AUTO:
            order = MarketOrder("SELL", int(qty))
            self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

        if price and self.risk_manager:
            self.risk_manager.close_position(symbol, price)

    def _liquidate_all(self):
        self.logger.warning("  LIQUIDATING ALL POSITIONS!")
        if not self.risk_manager:
            return
        symbols = list(self.risk_manager.positions.keys())
        for symbol in symbols:
            self._close_position(symbol)
        self.logger.warning("  Liquidation complete")

    def _liquidate_day_positions(self):
        if not self.risk_manager:
            return
        day_symbols = [
            s for s, p in self.risk_manager.positions.items()
            if p.trade_mode == "day"
        ]
        if day_symbols:
            self.logger.info(f"  EOD: Liquidating day-mode positions: {day_symbols}")
            for symbol in day_symbols:
                self._close_position(symbol)

    # ── Main Loop ────────────────────────────────────────────

    def run(self):
        if not self.ib or not self.ib.isConnected():
            if not self.connect():
                return

        self._load_profiles()

        if self.risk_manager:
            self.risk_manager.reset_daily()

        self.running = True
        self.logger.info(
            f"\nPolitician Trader Started! "
            f"Disclosure scan: {self.config.scan_interval_min}min | "
            f"Event scan: {self.config.event_scan_interval_min}min"
        )

        try:
            while self.running:
                if not self._is_market_open():
                    self.logger.info("  Market closed — waiting...")
                    self.ib.sleep(60)
                    continue

                if self.risk_manager:
                    risk = self.risk_manager.check_risk()
                    for sym in risk.must_close_symbols:
                        self._close_position(sym)

                self._scan_disclosures()
                self._scan_events()
                self._update_positions()
                self.print_dashboard()

                self.ib.sleep(self.config.event_scan_interval_min * 60)

        except KeyboardInterrupt:
            self.logger.info("\nUser interrupted")
        finally:
            if self.risk_manager and self.risk_manager.positions:
                day_positions = [s for s, p in self.risk_manager.positions.items()
                               if p.trade_mode == "day"]
                if day_positions and self.config.trade_mode == TradeMode.AUTO:
                    self._liquidate_day_positions()

            self.running = False
            self.disconnect()

    def run_daemon(self):
        self.logger.info("  Daemon mode — auto-start at market open daily")

        while True:
            try:
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                now_et = datetime.now()

            if now_et.weekday() < 5 and now_et.hour == 9 and now_et.minute >= 25:
                self.logger.info(f"  Market opening ({now_et:%Y-%m-%d %H:%M})")
                self.run()
                self.logger.info("  Market closed — waiting until tomorrow")

            time.sleep(60)

    def _update_positions(self):
        if not self.risk_manager or not self.risk_manager.positions:
            return

        price_map = {}
        for symbol in self.risk_manager.positions:
            contract = self.contracts.get(symbol)
            if contract:
                price = self.get_current_price(contract)
                if price:
                    price_map[symbol] = price

        if price_map:
            self.risk_manager.update_prices(price_map)

    def _is_market_open(self) -> bool:
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.weekday() >= 5:
                return False
            market_open = now_et.replace(hour=9, minute=30, second=0)
            market_close = now_et.replace(hour=16, minute=0, second=0)
            return market_open <= now_et <= market_close
        except Exception:
            return True

    # ── Dashboard ────────────────────────────────────────────

    def print_dashboard(self):
        now = datetime.now()

        print("\n")
        print("+" + "=" * 68 + "+")
        print(f"|  Politician Trader Dashboard   {now:%Y-%m-%d %H:%M:%S}  |")
        mode_str = 'AUTO' if self.config.trade_mode == TradeMode.AUTO else 'ALERT'
        print(f"|  Mode: {mode_str:50s}  |")
        print("+" + "=" * 68 + "+")

        if self.risk_manager:
            status = self.risk_manager.get_status()
            icon = "+" if status["daily_pnl"] >= 0 else "-"
            line = (
                f"|  [{icon}] PnL: ${status['daily_pnl']:+,.2f} "
                f"({status['daily_pnl_pct']:+.2f}%) | "
                f"Swing: {status['swing_count']} Day: {status['day_count']} | "
                f"Trades: {status['trade_count']}"
            )
            print(f"{line:<69s}|")

            if self.risk_manager.positions:
                for sym, pos in self.risk_manager.positions.items():
                    pnl_icon = "+" if pos.unrealized_pnl >= 0 else "-"
                    line = (
                        f"|    [{pnl_icon}] {sym:6s} x{pos.quantity:4d} "
                        f"@ ${pos.entry_price:.2f} -> ${pos.current_price:.2f} "
                        f"| ${pos.unrealized_pnl:+,.2f} [{pos.trade_mode}]"
                    )
                    print(f"{line:<69s}|")

        if self.profiles:
            top3 = sorted(self.profiles.values(),
                         key=lambda p: p.reliability_score, reverse=True)[:3]
            names = ", ".join(f"{p.name}({p.reliability_score:.2f})" for p in top3)
            line = f"|  Top politicians: {names}"
            print(f"{line:<69s}|")

        print("+" + "=" * 68 + "+")


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    +========================================================+
    |  Politician Trader v1.0 Demo (Offline)                  |
    +========================================================+
    """)

    try:
        from politician_data import demo as data_demo
        data_demo()
    except ImportError as e:
        print(f"  Warning: Data module load failed: {e}")

    print()

    try:
        from politician_strategies import demo as strategies_demo
        strategies_demo()
    except ImportError as e:
        print(f"  Warning: Strategy module load failed: {e}")

    print()

    try:
        from politician_risk import demo as risk_demo
        risk_demo()
    except ImportError as e:
        print(f"  Warning: Risk module load failed: {e}")

    print("\n  Demo complete!")
    print("  Live run: python run.py --politician [--auto]")


if __name__ == "__main__":
    demo()
