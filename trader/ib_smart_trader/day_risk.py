"""
═══════════════════════════════════════════════════════════════════
  Day Trading Risk Manager - Day Trading Risk Management System

  Features:
    1. Daily loss limit — staged brakes
    2. Per-stock loss limit — automatic position close
    3. Concurrent position limit — prevent over-diversification
    4. Position sizing — ATR-based dynamic adjustment
    5. EOD forced liquidation — close all positions before 15:50 ET
    6. PDT rule tracking — Pattern Day Trader trade count management
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from collections import defaultdict

logger = logging.getLogger("DayRiskManager")


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class DayRiskConfig:
    """Day trading risk configuration"""

    # ── Capital settings ──
    capital: float = 75_000.0               # Day trading dedicated capital

    # ── Daily loss limits ──
    daily_loss_soft_limit: float = -1_500.0   # Stage 1: Stop new entries ($)
    daily_loss_hard_limit: float = -2_000.0   # Stage 2: Force close all positions ($)
    daily_profit_target: float = 3_000.0      # Daily profit target ($) — informational

    # ── Per-stock loss limits ──
    per_stock_loss_limit: float = -500.0      # Max loss per stock ($)
    per_stock_loss_pct: float = -1.5          # Max loss per stock (%)

    # ── Position limits ──
    max_positions: int = 5                    # Max concurrent positions
    max_position_pct: float = 20.0            # Max capital allocation per stock (%)
    max_position_dollar: float = 15_000.0     # Max investment per stock ($)

    # ── Position sizing ──
    risk_per_trade_pct: float = 1.0           # Risk capital per trade (%)
    use_atr_sizing: bool = True               # ATR-based dynamic sizing

    # ── EOD forced liquidation ──
    eod_liquidation_enabled: bool = True
    eod_liquidation_time: str = "15:50"       # Forced liquidation time in ET

    # ── PDT rules ──
    pdt_tracking_enabled: bool = True
    pdt_min_equity: float = 25_000.0          # PDT minimum equity ($)
    pdt_max_day_trades_5d: int = 3            # Max day trades in 5 business days (below PDT threshold)

    # ── Cooldown ──
    cooldown_after_loss_min: int = 10         # N-minute cooldown after stop loss
    max_trades_per_hour: int = 10             # Max trades per hour


# ═══════════════════════════════════════════════════════════════
#  Risk state
# ═══════════════════════════════════════════════════════════════

class RiskLevel(Enum):
    NORMAL = "✅ Normal"
    CAUTION = "⚠️ Caution"
    SOFT_BRAKE = "🟡 New entries stopped"
    HARD_BRAKE = "🔴 Close all positions"
    EOD_LIQUIDATION = "⏰ EOD liquidation"


@dataclass
class DayPosition:
    """Day trading position"""
    symbol: str
    side: str               # "LONG" or "SHORT"
    entry_price: float
    quantity: int
    entry_time: datetime
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.entry_price

    def update_pnl(self, current_price: float):
        self.current_price = current_price
        if self.side == "LONG":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity


@dataclass
class RiskCheckResult:
    """Risk check result"""
    level: RiskLevel = RiskLevel.NORMAL
    can_open_new: bool = True
    must_close_all: bool = False
    must_close_symbols: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    suggested_size: int = 0         # Suggested quantity
    suggested_dollar: float = 0.0   # Suggested investment amount


# ═══════════════════════════════════════════════════════════════
#  Day Risk Manager
# ═══════════════════════════════════════════════════════════════

class DayRiskManager:
    """Day trading risk management"""

    def __init__(self, config: DayRiskConfig = None):
        self.config = config or DayRiskConfig()
        self.positions: dict[str, DayPosition] = {}
        self.daily_pnl: float = 0.0
        self.realized_pnl: float = 0.0
        self.trade_count: int = 0
        self.trade_timestamps: list[datetime] = []
        self.loss_cooldowns: dict[str, datetime] = {}  # {symbol: cooldown_until}
        self._day_trades_5d: list[str] = []  # Day trade dates in last 5 days

    def reset_daily(self):
        """Daily reset (called at market open each day)"""
        self.positions.clear()
        self.daily_pnl = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.trade_timestamps.clear()
        self.loss_cooldowns.clear()
        logger.info("📋 Daily risk counters reset")

    # ── Position management ──────────────────────────────────────────

    def open_position(self, symbol: str, side: str, price: float,
                      quantity: int, stop_loss: float = 0, take_profit: float = 0):
        """Record position open"""
        self.positions[symbol] = DayPosition(
            symbol=symbol, side=side, entry_price=price,
            quantity=quantity, entry_time=datetime.now(),
            stop_loss=stop_loss, take_profit=take_profit,
        )
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())
        logger.info(
            f"📥 Position opened: {side} {symbol} x{quantity} @ ${price:.2f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Record position close -> return realized PnL"""
        if symbol not in self.positions:
            return 0.0

        pos = self.positions[symbol]
        pos.update_pnl(exit_price)
        pnl = pos.unrealized_pnl

        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())

        # Record day trade (PDT)
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self._day_trades_5d:
            self._day_trades_5d.append(today)
        # Keep only last 5 days
        if len(self._day_trades_5d) > 5:
            self._day_trades_5d = self._day_trades_5d[-5:]

        # Stop loss cooldown
        if pnl < 0:
            cooldown_until = datetime.now() + timedelta(
                minutes=self.config.cooldown_after_loss_min
            )
            self.loss_cooldowns[symbol] = cooldown_until

        del self.positions[symbol]

        icon = "🟢" if pnl >= 0 else "🔴"
        logger.info(
            f"📤 Position closed: {symbol} @ ${exit_price:.2f} | "
            f"{icon} PnL: ${pnl:+,.2f} | Daily cumulative: ${self.daily_pnl:+,.2f}"
        )
        return pnl

    def update_prices(self, price_map: dict[str, float]):
        """Update current prices -> recalculate unrealized PnL"""
        total_unrealized = 0.0
        for symbol, pos in self.positions.items():
            if symbol in price_map:
                pos.update_pnl(price_map[symbol])
            total_unrealized += pos.unrealized_pnl
        self.daily_pnl = self.realized_pnl + total_unrealized

    # ── Risk checks ──────────────────────────────────────────

    def check_risk(self, symbol: str = "", current_price: float = 0.0) -> RiskCheckResult:
        """Comprehensive risk check"""
        result = RiskCheckResult()

        # 1. Daily loss limit
        self._check_daily_limits(result)

        # 2. Per-stock loss limit
        self._check_per_stock_limits(result)

        # 3. Concurrent position limit
        self._check_position_count(result)

        # 4. EOD liquidation time
        self._check_eod_time(result)

        # 5. Cooldown
        if symbol:
            self._check_cooldown(symbol, result)

        # 6. Trade frequency
        self._check_trade_frequency(result)

        # 7. PDT rules
        self._check_pdt(result)

        # Determine final risk level
        if result.must_close_all:
            result.level = RiskLevel.HARD_BRAKE
            result.can_open_new = False
        elif not result.can_open_new and any("EOD" in r for r in result.reasons):
            result.level = RiskLevel.EOD_LIQUIDATION
        elif not result.can_open_new:
            result.level = RiskLevel.SOFT_BRAKE
        elif result.reasons:
            result.level = RiskLevel.CAUTION

        return result

    def _check_daily_limits(self, result: RiskCheckResult):
        """Check daily loss limits"""
        cfg = self.config

        if self.daily_pnl <= cfg.daily_loss_hard_limit:
            result.can_open_new = False
            result.must_close_all = True
            result.reasons.append(
                f"🔴 Daily loss ${self.daily_pnl:+,.2f} <= "
                f"Hard limit ${cfg.daily_loss_hard_limit:,.2f} -> Close all positions"
            )
        elif self.daily_pnl <= cfg.daily_loss_soft_limit:
            result.can_open_new = False
            result.reasons.append(
                f"🟡 Daily loss ${self.daily_pnl:+,.2f} <= "
                f"Soft limit ${cfg.daily_loss_soft_limit:,.2f} -> New entries stopped"
            )

    def _check_per_stock_limits(self, result: RiskCheckResult):
        """Check per-stock loss limits"""
        cfg = self.config

        for symbol, pos in self.positions.items():
            # Absolute amount check
            if pos.unrealized_pnl <= cfg.per_stock_loss_limit:
                result.must_close_symbols.append(symbol)
                result.reasons.append(
                    f"🔴 {symbol} loss ${pos.unrealized_pnl:+,.2f} <= "
                    f"limit ${cfg.per_stock_loss_limit:,.2f}"
                )
                continue

            # Percentage check
            if pos.entry_price > 0:
                pnl_pct = pos.unrealized_pnl / (pos.entry_price * pos.quantity) * 100
                if pnl_pct <= cfg.per_stock_loss_pct:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🔴 {symbol} loss {pnl_pct:.1f}% <= limit {cfg.per_stock_loss_pct}%"
                    )

            # Stop loss price reached check
            if pos.stop_loss > 0 and pos.current_price > 0:
                if pos.side == "LONG" and pos.current_price <= pos.stop_loss:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🛑 {symbol} stop loss reached "
                        f"${pos.current_price:.2f} <= SL ${pos.stop_loss:.2f}"
                    )

            # Take profit price reached check
            if pos.take_profit > 0 and pos.current_price > 0:
                if pos.side == "LONG" and pos.current_price >= pos.take_profit:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🎯 {symbol} take profit reached "
                        f"${pos.current_price:.2f} >= TP ${pos.take_profit:.2f}"
                    )

    def _check_position_count(self, result: RiskCheckResult):
        """Check concurrent position count"""
        if len(self.positions) >= self.config.max_positions:
            result.can_open_new = False
            result.reasons.append(
                f"📊 Concurrent positions {len(self.positions)}/{self.config.max_positions} at max"
            )

    def _check_eod_time(self, result: RiskCheckResult):
        """Check forced liquidation time before market close"""
        if not self.config.eod_liquidation_enabled:
            return

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = datetime.now()

        h, m = map(int, self.config.eod_liquidation_time.split(":"))
        liquidation_time = now_et.replace(hour=h, minute=m, second=0, microsecond=0)

        if now_et >= liquidation_time and self.positions:
            result.can_open_new = False
            result.must_close_all = True
            result.reasons.append(
                f"⏰ EOD forced liquidation time ({self.config.eod_liquidation_time} ET) reached"
            )
        elif now_et >= liquidation_time - timedelta(minutes=15):
            result.can_open_new = False
            result.reasons.append(
                f"⏰ 15 min before EOD liquidation — new entries stopped"
            )

    def _check_cooldown(self, symbol: str, result: RiskCheckResult):
        """Check post-stop-loss cooldown"""
        if symbol in self.loss_cooldowns:
            until = self.loss_cooldowns[symbol]
            if datetime.now() < until:
                remaining = int((until - datetime.now()).total_seconds() / 60)
                result.can_open_new = False
                result.reasons.append(
                    f"⏱️ {symbol} cooldown {remaining} min remaining "
                    f"({self.config.cooldown_after_loss_min} min wait after stop loss)"
                )
            else:
                del self.loss_cooldowns[symbol]

    def _check_trade_frequency(self, result: RiskCheckResult):
        """Check trade frequency"""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_trades = sum(1 for t in self.trade_timestamps if t > one_hour_ago)

        if recent_trades >= self.config.max_trades_per_hour:
            result.can_open_new = False
            result.reasons.append(
                f"⚡ Hourly trades {recent_trades}/{self.config.max_trades_per_hour} exceeded"
            )

    def _check_pdt(self, result: RiskCheckResult):
        """Check PDT rules"""
        if not self.config.pdt_tracking_enabled:
            return

        # Warn if capital is below PDT threshold
        if self.config.capital < self.config.pdt_min_equity:
            day_trade_count = len(self._day_trades_5d)
            if day_trade_count >= self.config.pdt_max_day_trades_5d:
                result.can_open_new = False
                result.reasons.append(
                    f"⚠️ PDT rule! Day trades in 5 days: "
                    f"{day_trade_count}/{self.config.pdt_max_day_trades_5d} "
                    f"(capital ${self.config.capital:,.0f} < ${self.config.pdt_min_equity:,.0f})"
                )

    # ── Position sizing ────────────────────────────────────────

    def calculate_position_size(
        self, symbol: str, price: float, atr: float = 0.0,
        stop_distance: float = 0.0,
    ) -> dict:
        """
        Calculate position size

        Returns:
            {
                "shares": quantity,
                "dollar_amount": investment amount,
                "risk_amount": risk amount,
                "method": sizing method,
            }
        """
        cfg = self.config

        # Max investment (lesser of N% of capital or fixed limit)
        max_dollar = min(
            cfg.capital * cfg.max_position_pct / 100,
            cfg.max_position_dollar,
        )

        # ATR-based sizing
        if cfg.use_atr_sizing and atr > 0 and stop_distance > 0:
            risk_amount = cfg.capital * cfg.risk_per_trade_pct / 100
            shares = int(risk_amount / stop_distance)
            dollar_amount = shares * price
            method = f"ATR (risk ${risk_amount:.0f} / SL distance ${stop_distance:.2f})"
        else:
            # Fixed ratio sizing
            dollar_amount = max_dollar
            shares = int(dollar_amount / price) if price > 0 else 0
            method = f"Fixed ({cfg.max_position_pct}% of capital)"

        # Apply max investment limit
        if shares * price > max_dollar:
            shares = int(max_dollar / price)
            dollar_amount = shares * price

        # Minimum 1 share
        shares = max(1, shares)
        dollar_amount = shares * price

        return {
            "shares": shares,
            "dollar_amount": round(dollar_amount, 2),
            "risk_amount": round(stop_distance * shares, 2) if stop_distance > 0 else 0,
            "method": method,
        }

    # ── Status output ────────────────────────────────────────────

    def get_status(self) -> dict:
        """Current risk status summary"""
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "position_count": len(self.positions),
            "max_positions": self.config.max_positions,
            "trade_count": self.trade_count,
            "capital": self.config.capital,
            "daily_pnl_pct": round(self.daily_pnl / self.config.capital * 100, 2),
        }

    def print_dashboard(self):
        """Print risk dashboard"""
        status = self.get_status()
        risk = self.check_risk()

        icon = "🟢" if status["daily_pnl"] >= 0 else "🔴"

        print(f"\n  {risk.level.value}")
        print(f"  {icon} Daily PnL: ${status['daily_pnl']:+,.2f} ({status['daily_pnl_pct']:+.2f}%)")
        print(f"    Realized: ${status['realized_pnl']:+,.2f} | Unrealized: ${status['unrealized_pnl']:+,.2f}")
        print(f"    Positions: {status['position_count']}/{status['max_positions']} | Trades: {status['trade_count']}")

        if self.positions:
            print(f"    ── Position details ──")
            for sym, pos in self.positions.items():
                pnl_icon = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
                print(
                    f"      {pnl_icon} {sym:6s} {pos.side:5s} "
                    f"x{pos.quantity:4d} @ ${pos.entry_price:.2f} "
                    f"-> ${pos.current_price:.2f} | "
                    f"PnL: ${pos.unrealized_pnl:+,.2f}"
                )

        if risk.reasons:
            print(f"    ── Warnings ──")
            for reason in risk.reasons:
                print(f"      {reason}")


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🛡️ Day Risk Manager Demo                                ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    rm = DayRiskManager(DayRiskConfig(capital=75_000))

    # 1) Open positions
    rm.open_position("NVDA", "LONG", 180.00, 50, stop_loss=178.50, take_profit=183.00)
    rm.open_position("AAPL", "LONG", 215.00, 30, stop_loss=213.50, take_profit=218.00)
    rm.open_position("TSLA", "LONG", 250.00, 20, stop_loss=247.00, take_profit=256.00)

    # 2) Price update
    rm.update_prices({"NVDA": 181.50, "AAPL": 214.00, "TSLA": 248.00})
    rm.print_dashboard()

    # 3) Position sizing example
    print("\n  📏 Position sizing (AMD, $160, ATR=$2.5):")
    sizing = rm.calculate_position_size("AMD", 160.0, atr=2.5, stop_distance=3.75)
    print(f"    Shares: {sizing['shares']} | Investment: ${sizing['dollar_amount']:,.2f}")
    print(f"    Risk amount: ${sizing['risk_amount']:,.2f} | Method: {sizing['method']}")

    # 4) Loss scenario
    print("\n  📉 TSLA stop loss scenario:")
    rm.update_prices({"NVDA": 181.50, "AAPL": 214.00, "TSLA": 246.00})
    risk = rm.check_risk("TSLA", 246.00)
    print(f"    Risk: {risk.level.value}")
    for r in risk.reasons:
        print(f"    {r}")

    # 5) Close
    pnl = rm.close_position("TSLA", 246.00)
    print(f"    TSLA liquidation PnL: ${pnl:+,.2f}")
    rm.print_dashboard()


if __name__ == "__main__":
    demo()
