"""
═══════════════════════════════════════════════════════════════════
  Politician Risk Manager - Congressman-Following Strategy Risk Management

  Features:
    1. Daily loss limits — tiered brakes
    2. Sector concentration limits — prevent over 40% in one sector
    3. Per-politician exposure limits — max 3 positions following one politician
    4. Position sizing — swing vs day differentiation
    5. Day mode EOD liquidation
    6. Smart/Day Trader overlap check
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("PoliticianRisk")


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class PoliticianRiskConfig:
    """Risk configuration"""

    # ── Capital ──
    capital: float = 50_000.0

    # ── Daily loss limits ──
    daily_loss_soft_limit: float = -1_000.0
    daily_loss_hard_limit: float = -1_500.0
    daily_profit_target: float = 2_500.0

    # ── Position limits ──
    max_positions: int = 8
    max_position_pct: float = 15.0
    max_position_dollar: float = 10_000.0

    # ── Sector concentration ──
    max_sector_pct: float = 40.0

    # ── Per-politician exposure ──
    max_same_politician_positions: int = 3

    # ── Day mode ──
    day_max_positions: int = 3
    day_max_position_pct: float = 8.0
    day_max_position_dollar: float = 5_000.0
    day_eod_liquidation_time: str = "15:50"

    # ── Sizing ──
    risk_per_trade_pct: float = 1.5

    # ── Cooldown ──
    cooldown_after_loss_min: int = 15
    max_trades_per_hour: int = 6


# ═══════════════════════════════════════════════════════════════
#  Risk State
# ═══════════════════════════════════════════════════════════════

class RiskLevel(Enum):
    NORMAL = "Normal"
    CAUTION = "Caution"
    SOFT_BRAKE = "Soft Brake - New Entry Halted"
    HARD_BRAKE = "Hard Brake - Liquidate All"
    EOD_LIQUIDATION = "EOD Liquidation"


@dataclass
class PoliticianPosition:
    """Position"""
    symbol: str
    side: str               # "LONG" or "SHORT"
    entry_price: float
    quantity: int
    entry_time: datetime
    trade_mode: str = "swing"   # "swing" or "day"
    politician: str = ""        # Followed politician
    sector: str = ""            # Sector
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    hold_period_days: int = 0

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
    suggested_size: int = 0
    suggested_dollar: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  Risk Manager
# ═══════════════════════════════════════════════════════════════

class PoliticianRiskManager:
    """Congressman-following strategy risk management"""

    def __init__(self, config: PoliticianRiskConfig = None):
        self.config = config or PoliticianRiskConfig()
        self.positions: dict[str, PoliticianPosition] = {}
        self.daily_pnl: float = 0.0
        self.realized_pnl: float = 0.0
        self.trade_count: int = 0
        self.trade_timestamps: list[datetime] = []
        self.loss_cooldowns: dict[str, datetime] = {}

    def reset_daily(self):
        """Daily reset"""
        # Reset only day mode positions (swing positions are kept)
        day_positions = [s for s, p in self.positions.items() if p.trade_mode == "day"]
        for sym in day_positions:
            del self.positions[sym]
        self.daily_pnl = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.trade_timestamps.clear()
        self.loss_cooldowns.clear()
        logger.info("Daily risk counters reset (swing positions retained)")

    # ── Position Management ──────────────────────────────────────────

    def open_position(self, symbol: str, side: str, price: float,
                      quantity: int, trade_mode: str = "swing",
                      politician: str = "", sector: str = "",
                      stop_loss: float = 0, take_profit: float = 0,
                      hold_period_days: int = 0):
        """Record position open"""
        self.positions[symbol] = PoliticianPosition(
            symbol=symbol, side=side, entry_price=price,
            quantity=quantity, entry_time=datetime.now(),
            trade_mode=trade_mode, politician=politician, sector=sector,
            stop_loss=stop_loss, take_profit=take_profit,
            hold_period_days=hold_period_days,
        )
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())
        logger.info(
            f"Position opened: {side} {symbol} x{quantity} @ ${price:.2f} | "
            f"Mode: {trade_mode} | Politician: {politician} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Close position -> return realized PnL"""
        if symbol not in self.positions:
            return 0.0

        pos = self.positions[symbol]
        pos.update_pnl(exit_price)
        pnl = pos.unrealized_pnl

        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())

        if pnl < 0:
            cooldown_until = datetime.now() + timedelta(
                minutes=self.config.cooldown_after_loss_min
            )
            self.loss_cooldowns[symbol] = cooldown_until

        del self.positions[symbol]

        icon = "+" if pnl >= 0 else "-"
        logger.info(
            f"Position closed: {symbol} @ ${exit_price:.2f} | "
            f"{icon} PnL: ${pnl:+,.2f} | Daily cumulative: ${self.daily_pnl:+,.2f}"
        )
        return pnl

    def update_prices(self, price_map: dict[str, float]):
        """Update current prices"""
        total_unrealized = 0.0
        for symbol, pos in self.positions.items():
            if symbol in price_map:
                pos.update_pnl(price_map[symbol])
            total_unrealized += pos.unrealized_pnl
        self.daily_pnl = self.realized_pnl + total_unrealized

    # ── Risk Checks ──────────────────────────────────────────

    def check_risk(self, symbol: str = "", trade_mode: str = "swing",
                   politician: str = "", sector: str = "") -> RiskCheckResult:
        """Comprehensive risk check"""
        result = RiskCheckResult()

        self._check_daily_limits(result)
        self._check_position_count(result, trade_mode)
        self._check_sector_concentration(result, sector)
        self._check_politician_exposure(result, politician)
        self._check_stop_take(result)
        self._check_eod_time(result)
        self._check_cooldown(symbol, result)
        self._check_trade_frequency(result)

        # Determine final level
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
        cfg = self.config
        if self.daily_pnl <= cfg.daily_loss_hard_limit:
            result.can_open_new = False
            result.must_close_all = True
            result.reasons.append(
                f"Daily loss ${self.daily_pnl:+,.2f} <= "
                f"Hard limit ${cfg.daily_loss_hard_limit:,.2f}"
            )
        elif self.daily_pnl <= cfg.daily_loss_soft_limit:
            result.can_open_new = False
            result.reasons.append(
                f"Daily loss ${self.daily_pnl:+,.2f} <= "
                f"Soft limit ${cfg.daily_loss_soft_limit:,.2f}"
            )

    def _check_position_count(self, result: RiskCheckResult, trade_mode: str):
        cfg = self.config
        total = len(self.positions)
        day_count = sum(1 for p in self.positions.values() if p.trade_mode == "day")

        if total >= cfg.max_positions:
            result.can_open_new = False
            result.reasons.append(f"Positions {total}/{cfg.max_positions} at max")

        if trade_mode == "day" and day_count >= cfg.day_max_positions:
            result.can_open_new = False
            result.reasons.append(f"Day positions {day_count}/{cfg.day_max_positions} at max")

    def _check_sector_concentration(self, result: RiskCheckResult, sector: str):
        if not sector:
            return
        cfg = self.config

        sector_value = sum(
            p.market_value for p in self.positions.values()
            if p.sector == sector
        )
        total_value = sum(p.market_value for p in self.positions.values())

        if total_value > 0:
            sector_pct = sector_value / cfg.capital * 100
            if sector_pct >= cfg.max_sector_pct:
                result.can_open_new = False
                result.reasons.append(
                    f"Sector concentration: {sector} {sector_pct:.1f}% >= "
                    f"{cfg.max_sector_pct}% limit"
                )

    def _check_politician_exposure(self, result: RiskCheckResult, politician: str):
        if not politician:
            return
        cfg = self.config

        pol_count = sum(
            1 for p in self.positions.values()
            if p.politician == politician
        )
        if pol_count >= cfg.max_same_politician_positions:
            result.can_open_new = False
            result.reasons.append(
                f"{politician} positions {pol_count}/{cfg.max_same_politician_positions} at max"
            )

    def _check_stop_take(self, result: RiskCheckResult):
        """Stop loss / take profit check"""
        for symbol, pos in self.positions.items():
            if pos.current_price <= 0:
                continue

            if pos.stop_loss > 0:
                if pos.side == "LONG" and pos.current_price <= pos.stop_loss:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"{symbol} stop loss hit ${pos.current_price:.2f} <= SL ${pos.stop_loss:.2f}"
                    )
                elif pos.side == "SHORT" and pos.current_price >= pos.stop_loss:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"{symbol} stop loss hit ${pos.current_price:.2f} >= SL ${pos.stop_loss:.2f}"
                    )

            if pos.take_profit > 0:
                if pos.side == "LONG" and pos.current_price >= pos.take_profit:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"{symbol} take profit hit ${pos.current_price:.2f} >= TP ${pos.take_profit:.2f}"
                    )
                elif pos.side == "SHORT" and pos.current_price <= pos.take_profit:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"{symbol} take profit hit ${pos.current_price:.2f} <= TP ${pos.take_profit:.2f}"
                    )

    def _check_eod_time(self, result: RiskCheckResult):
        """Day mode EOD liquidation check"""
        day_positions = [s for s, p in self.positions.items() if p.trade_mode == "day"]
        if not day_positions:
            return

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = datetime.now()

        h, m = map(int, self.config.day_eod_liquidation_time.split(":"))
        liq_time = now_et.replace(hour=h, minute=m, second=0, microsecond=0)

        if now_et >= liq_time:
            for sym in day_positions:
                if sym not in result.must_close_symbols:
                    result.must_close_symbols.append(sym)
            result.reasons.append(
                f"EOD liquidation time — liquidating {len(day_positions)} day mode position(s)"
            )
        elif now_et >= liq_time - timedelta(minutes=15):
            result.can_open_new = False
            result.reasons.append("15 min before EOD liquidation — day mode new entry halted")

    def _check_cooldown(self, symbol: str, result: RiskCheckResult):
        if not symbol:
            return
        if symbol in self.loss_cooldowns:
            until = self.loss_cooldowns[symbol]
            if datetime.now() < until:
                remaining = int((until - datetime.now()).total_seconds() / 60)
                result.can_open_new = False
                result.reasons.append(f"{symbol} cooldown {remaining} min remaining")
            else:
                del self.loss_cooldowns[symbol]

    def _check_trade_frequency(self, result: RiskCheckResult):
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent = sum(1 for t in self.trade_timestamps if t > one_hour_ago)
        if recent >= self.config.max_trades_per_hour:
            result.can_open_new = False
            result.reasons.append(
                f"Hourly trades {recent}/{self.config.max_trades_per_hour} exceeded"
            )

    # ── Position Sizing ────────────────────────────────────────

    def calculate_position_size(
        self, symbol: str, price: float,
        trade_mode: str = "swing",
        stop_distance: float = 0.0,
    ) -> dict:
        """Calculate position size"""
        cfg = self.config

        if trade_mode == "day":
            max_dollar = min(
                cfg.capital * cfg.day_max_position_pct / 100,
                cfg.day_max_position_dollar,
            )
        else:
            max_dollar = min(
                cfg.capital * cfg.max_position_pct / 100,
                cfg.max_position_dollar,
            )

        # Risk-based sizing
        if stop_distance > 0:
            risk_amount = cfg.capital * cfg.risk_per_trade_pct / 100
            shares = int(risk_amount / stop_distance)
            dollar_amount = shares * price
            method = f"Risk-based (risk ${risk_amount:.0f} / SL distance ${stop_distance:.2f})"
        else:
            dollar_amount = max_dollar
            shares = int(dollar_amount / price) if price > 0 else 0
            method = f"Fixed ({trade_mode} mode)"

        # Max cap
        if shares * price > max_dollar:
            shares = int(max_dollar / price)
            dollar_amount = shares * price

        shares = max(1, shares)
        dollar_amount = shares * price

        return {
            "shares": shares,
            "dollar_amount": round(dollar_amount, 2),
            "risk_amount": round(stop_distance * shares, 2) if stop_distance > 0 else 0,
            "method": method,
        }

    # ── Status ─────────────────────────────────────────────────

    def get_status(self) -> dict:
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        swing_count = sum(1 for p in self.positions.values() if p.trade_mode == "swing")
        day_count = sum(1 for p in self.positions.values() if p.trade_mode == "day")
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "position_count": len(self.positions),
            "swing_count": swing_count,
            "day_count": day_count,
            "max_positions": self.config.max_positions,
            "trade_count": self.trade_count,
            "capital": self.config.capital,
            "daily_pnl_pct": round(self.daily_pnl / self.config.capital * 100, 2) if self.config.capital > 0 else 0,
        }

    def print_dashboard(self):
        status = self.get_status()
        risk = self.check_risk()

        icon = "+" if status["daily_pnl"] >= 0 else "-"

        print(f"\n  {risk.level.value}")
        print(f"  {icon} Daily PnL: ${status['daily_pnl']:+,.2f} ({status['daily_pnl_pct']:+.2f}%)")
        print(f"    Realized: ${status['realized_pnl']:+,.2f} | Unrealized: ${status['unrealized_pnl']:+,.2f}")
        print(f"    Positions: {status['position_count']}/{status['max_positions']} "
              f"(Swing: {status['swing_count']}, Day: {status['day_count']}) | "
              f"Trades: {status['trade_count']}")

        if self.positions:
            print(f"    -- Position Details --")
            for sym, pos in self.positions.items():
                pnl_icon = "+" if pos.unrealized_pnl >= 0 else "-"
                print(
                    f"      {pnl_icon} {sym:6s} {pos.side:5s} "
                    f"x{pos.quantity:4d} @ ${pos.entry_price:.2f} "
                    f"-> ${pos.current_price:.2f} | "
                    f"PnL: ${pos.unrealized_pnl:+,.2f} | "
                    f"{pos.trade_mode} | {pos.politician}"
                )

        if risk.reasons:
            print(f"    -- Warnings --")
            for reason in risk.reasons:
                print(f"      {reason}")


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  Politician Risk Manager Demo                            ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    rm = PoliticianRiskManager(PoliticianRiskConfig(capital=50_000))

    # 1) Open positions
    rm.open_position("NVDA", "LONG", 185.00, 30, trade_mode="swing",
                     politician="Nancy Pelosi", sector="Technology",
                     stop_loss=175.75, take_profit=207.20)
    rm.open_position("LMT", "LONG", 520.00, 10, trade_mode="swing",
                     politician="Tommy Tuberville", sector="Aerospace & Defense",
                     stop_loss=494.00, take_profit=582.40)
    rm.open_position("RTX", "LONG", 125.00, 25, trade_mode="swing",
                     politician="Mark Kelly", sector="Aerospace & Defense",
                     stop_loss=118.75, take_profit=140.00)

    # 2) Update prices
    rm.update_prices({"NVDA": 188.50, "LMT": 515.00, "RTX": 127.00})
    rm.print_dashboard()

    # 3) Sector concentration check
    print("\n  Sector concentration check (adding another Aerospace entry):")
    risk = rm.check_risk("BA", trade_mode="swing", sector="Aerospace & Defense")
    print(f"    Risk: {risk.level.value} | Can enter: {risk.can_open_new}")
    for r in risk.reasons:
        print(f"    {r}")

    # 4) Position sizing
    print("\n  Position sizing:")
    for mode in ["swing", "day"]:
        sizing = rm.calculate_position_size("XOM", 110.0, trade_mode=mode, stop_distance=5.5)
        print(f"    [{mode}] {sizing['shares']} shares x $110 = ${sizing['dollar_amount']:,.2f} | {sizing['method']}")

    # 5) Stop loss scenario
    print("\n  LMT stop loss scenario:")
    rm.update_prices({"NVDA": 188.50, "LMT": 493.00, "RTX": 127.00})
    risk = rm.check_risk()
    for r in risk.reasons:
        print(f"    {r}")

    rm.print_dashboard()


if __name__ == "__main__":
    demo()
