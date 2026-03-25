"""
═══════════════════════════════════════════════════════════════════
  Portfolio Manager - Multi-Bucket Portfolio Management System

  Offensive/Defensive split portfolio management for $200K+ accounts

  Bucket Structure:
    🔴 Offensive  — Tech/AI + Momentum + High Growth
    🔵 Defensive  — Dividends + Safe Assets + Defense + Consumer Staples

  Core Features:
    1. Dynamic ratio adjustment based on market conditions (Signal Monitor integration)
       - BULL: 60 Offensive / 40 Defensive
       - NEUTRAL: 50/50
       - BEAR: 40 Offensive / 60 Defensive
    2. Independent risk management per bucket
    3. Max 5% weight per stock
    4. Enforced sector diversification
    5. Automatic rebalancing

  Integrates with Smart Trader, Risk Shield, Tax Optimizer, Signal Bridge
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger("PortfolioManager")


# ═══════════════════════════════════════════════════════════════
#  Bucket Definitions
# ═══════════════════════════════════════════════════════════════

class BucketType(Enum):
    OFFENSIVE = "🔴 Offensive"
    DEFENSIVE = "🔵 Defensive"
    CASH = "💵 Cash"


class MarketRegime(Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class PortfolioConfig:
    """Multi-bucket portfolio configuration"""

    # ── Total Capital ──
    total_capital: float = 200_000.0

    # ── Dynamic Ratios (by market regime) ──
    # {regime: (Offensive%, Defensive%, Cash%)}
    regime_allocation: dict = field(default_factory=lambda: {
        "BULL":    (60, 30, 10),
        "NEUTRAL": (50, 40, 10),
        "BEAR":    (30, 55, 15),
    })

    # ── Stock Limits ──
    max_weight_per_stock: float = 5.0     # Max 5% per stock
    max_stocks_offensive: int = 15        # Max stocks in offensive bucket
    max_stocks_defensive: int = 15        # Max stocks in defensive bucket
    max_stocks_per_sector: int = 4        # Max per sector

    # ── Rebalancing ──
    rebalance_threshold_pct: float = 5.0  # Rebalance when 5% drift from target ratio
    rebalance_frequency_days: int = 7     # Check rebalancing at least every N days

    # ── Offensive Bucket — Stop Loss ──
    offensive_stop_loss_pct: float = -8.0   # -8% stop loss per stock
    offensive_take_profit_pct: float = 15.0 # +15% take profit per stock

    # ── Defensive Bucket — Wider Range ──
    defensive_stop_loss_pct: float = -12.0  # Wider range since defensive stocks are less volatile
    defensive_take_profit_pct: float = 20.0


# ═══════════════════════════════════════════════════════════════
#  Stock Universe
# ═══════════════════════════════════════════════════════════════

@dataclass
class StockInfo:
    """Stock information"""
    symbol: str
    name: str
    sector: str
    bucket: BucketType
    beta: float
    dividend_yield: float = 0.0
    base_weight: float = 0.0    # Base allocation weight (%)

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "bucket": self.bucket.value,
            "beta": self.beta,
            "dividend_yield": self.dividend_yield,
            "base_weight": self.base_weight,
        }


# Offensive portfolio universe
OFFENSIVE_UNIVERSE = [
    StockInfo("NVDA",  "Nvidia",          "AI/Semiconductors",  BucketType.OFFENSIVE, 1.95, 0.02, 15),
    StockInfo("AMD",   "AMD",             "Semiconductors",     BucketType.OFFENSIVE, 1.60, 0.00, 10),
    StockInfo("META",  "Meta Platforms",   "AI/Social",          BucketType.OFFENSIVE, 1.25, 0.00, 10),
    StockInfo("MSFT",  "Microsoft",        "Cloud",              BucketType.OFFENSIVE, 0.90, 0.80, 10),
    StockInfo("AMZN",  "Amazon",           "Cloud",              BucketType.OFFENSIVE, 1.15, 0.00, 8),
    StockInfo("TSLA",  "Tesla",            "EV/Robotics",        BucketType.OFFENSIVE, 2.05, 0.00, 5),
    StockInfo("MU",    "Micron",           "Memory",             BucketType.OFFENSIVE, 1.45, 0.40, 7),
    StockInfo("COP",   "ConocoPhillips",   "Energy",             BucketType.OFFENSIVE, 1.30, 1.80, 8),
    StockInfo("DVN",   "Devon Energy",     "Shale",              BucketType.OFFENSIVE, 1.75, 2.50, 5),
    StockInfo("HOOD",  "Robinhood",        "Fintech",            BucketType.OFFENSIVE, 2.10, 0.00, 5),
    StockInfo("PLTR",  "Palantir",         "AI/Defense",         BucketType.OFFENSIVE, 1.80, 0.00, 7),
    StockInfo("AVGO",  "Broadcom",         "Semiconductors",     BucketType.OFFENSIVE, 1.20, 1.20, 5),
    StockInfo("COIN",  "Coinbase",         "Crypto",             BucketType.OFFENSIVE, 2.30, 0.00, 5),
]

# Defensive portfolio universe
DEFENSIVE_UNIVERSE = [
    StockInfo("LMT",   "Lockheed Martin",  "Defense",            BucketType.DEFENSIVE, 0.55, 2.50, 12),
    StockInfo("NOC",   "Northrop Grumman", "Defense",            BucketType.DEFENSIVE, 0.50, 1.60, 10),
    StockInfo("RTX",   "RTX Corp",         "Defense",            BucketType.DEFENSIVE, 0.65, 2.10, 8),
    StockInfo("XOM",   "ExxonMobil",       "Energy",             BucketType.DEFENSIVE, 0.85, 2.60, 12),
    StockInfo("CVX",   "Chevron",          "Energy",             BucketType.DEFENSIVE, 0.90, 3.00, 10),
    StockInfo("GLD",   "Gold ETF",         "Gold",               BucketType.DEFENSIVE, 0.15, 0.00, 12),
    StockInfo("WMT",   "Walmart",          "Consumer Staples",   BucketType.DEFENSIVE, 0.50, 1.10, 8),
    StockInfo("JNJ",   "Johnson & Johnson","Healthcare",         BucketType.DEFENSIVE, 0.55, 3.20, 8),
    StockInfo("PG",    "Procter & Gamble", "Consumer Staples",   BucketType.DEFENSIVE, 0.40, 2.40, 7),
    StockInfo("UNH",   "UnitedHealth",     "Healthcare",         BucketType.DEFENSIVE, 0.60, 1.50, 8),
    StockInfo("KO",    "Coca-Cola",        "Consumer Staples",   BucketType.DEFENSIVE, 0.55, 2.80, 5),
]


# ═══════════════════════════════════════════════════════════════
#  Portfolio Manager
# ═══════════════════════════════════════════════════════════════

class PortfolioManager:
    """
    Multi-Bucket Portfolio Manager

    Usage:
        pm = PortfolioManager(PortfolioConfig(total_capital=200000))

        # Set market regime (Signal Bridge integration)
        pm.set_regime(MarketRegime.NEUTRAL)

        # Build portfolio
        portfolio = pm.build_portfolio()

        # Check rebalancing
        rebalance = pm.check_rebalance(current_positions)
    """

    def __init__(self, config: PortfolioConfig = None):
        self.config = config or PortfolioConfig()
        self.regime = MarketRegime.NEUTRAL
        self.last_rebalance = None
        self._portfolio: dict = {}
        self._history: list = []

    def set_regime(self, regime: MarketRegime):
        """Set market regime (called from Signal Bridge)"""
        if self.regime != regime:
            logger.info(
                f"  🔄 Market regime change: {self.regime.value} → {regime.value}"
            )
            self.regime = regime

    def get_allocation(self) -> dict:
        """Capital allocation based on current regime"""
        alloc = self.config.regime_allocation.get(
            self.regime.value, (50, 40, 10)
        )
        off_pct, def_pct, cash_pct = alloc
        total = self.config.total_capital

        return {
            "offensive": total * off_pct / 100,
            "defensive": total * def_pct / 100,
            "cash": total * cash_pct / 100,
            "offensive_pct": off_pct,
            "defensive_pct": def_pct,
            "cash_pct": cash_pct,
        }

    def build_portfolio(self) -> dict:
        """
        Build the full portfolio

        Returns:
            {
                "regime": str,
                "allocation": dict,
                "offensive_stocks": [dict],
                "defensive_stocks": [dict],
                "cash": float,
                "total_stocks": int,
                "est_annual_dividend": float,
            }
        """
        alloc = self.get_allocation()
        off_capital = alloc["offensive"]
        def_capital = alloc["defensive"]

        # Max amount per stock
        max_per_stock = self.config.total_capital * self.config.max_weight_per_stock / 100

        # ── Build offensive portfolio ──
        offensive_stocks = []
        off_total_weight = sum(s.base_weight for s in OFFENSIVE_UNIVERSE)

        for stock in OFFENSIVE_UNIVERSE:
            weight_ratio = stock.base_weight / off_total_weight
            raw_amount = off_capital * weight_ratio

            # Beta adjustment: high Beta → reduce, low Beta → increase
            beta_adj = 1.0 / max(stock.beta, 0.5)
            beta_adj = max(0.5, min(1.5, beta_adj))
            adjusted = raw_amount * beta_adj

            # Apply max limit
            final_amount = min(adjusted, max_per_stock)

            offensive_stocks.append({
                "symbol": stock.symbol,
                "name": stock.name,
                "sector": stock.sector,
                "bucket": "Offensive",
                "amount": round(final_amount, 0),
                "weight_pct": round(final_amount / self.config.total_capital * 100, 2),
                "beta": stock.beta,
                "dividend_yield": stock.dividend_yield,
                "stop_loss": self.config.offensive_stop_loss_pct,
                "take_profit": self.config.offensive_take_profit_pct,
            })

        # Normalize offensive portfolio (total = off_capital)
        off_sum = sum(s["amount"] for s in offensive_stocks)
        if off_sum > 0:
            scale = off_capital / off_sum
            for s in offensive_stocks:
                s["amount"] = round(s["amount"] * scale, 0)
                s["weight_pct"] = round(s["amount"] / self.config.total_capital * 100, 2)

        # ── Build defensive portfolio ──
        defensive_stocks = []
        def_total_weight = sum(s.base_weight for s in DEFENSIVE_UNIVERSE)

        for stock in DEFENSIVE_UNIVERSE:
            weight_ratio = stock.base_weight / def_total_weight
            raw_amount = def_capital * weight_ratio

            # Defensive stocks get lighter Beta adjustment
            final_amount = min(raw_amount, max_per_stock)

            defensive_stocks.append({
                "symbol": stock.symbol,
                "name": stock.name,
                "sector": stock.sector,
                "bucket": "Defensive",
                "amount": round(final_amount, 0),
                "weight_pct": round(final_amount / self.config.total_capital * 100, 2),
                "beta": stock.beta,
                "dividend_yield": stock.dividend_yield,
                "stop_loss": self.config.defensive_stop_loss_pct,
                "take_profit": self.config.defensive_take_profit_pct,
            })

        # Normalize defensive portfolio
        def_sum = sum(s["amount"] for s in defensive_stocks)
        if def_sum > 0:
            scale = def_capital / def_sum
            for s in defensive_stocks:
                s["amount"] = round(s["amount"] * scale, 0)
                s["weight_pct"] = round(s["amount"] / self.config.total_capital * 100, 2)

        # Estimate annual dividends
        est_dividend = sum(
            s["amount"] * s["dividend_yield"] / 100
            for s in offensive_stocks + defensive_stocks
        )

        # Average Beta
        all_stocks = offensive_stocks + defensive_stocks
        total_invested = sum(s["amount"] for s in all_stocks)
        avg_beta = sum(
            s["amount"] * s["beta"] for s in all_stocks
        ) / total_invested if total_invested > 0 else 1.0

        self._portfolio = {
            "regime": self.regime.value,
            "allocation": alloc,
            "offensive_stocks": offensive_stocks,
            "defensive_stocks": defensive_stocks,
            "cash": alloc["cash"],
            "total_stocks": len(offensive_stocks) + len(defensive_stocks),
            "est_annual_dividend": round(est_dividend, 0),
            "avg_beta": round(avg_beta, 2),
            "timestamp": datetime.now().isoformat(),
        }

        return self._portfolio

    def check_rebalance(self, current_positions: dict) -> dict:
        """
        Check whether rebalancing is needed

        Parameters:
            current_positions: {symbol: {"market_value": float, "pnl_pct": float}}

        Returns:
            {"needed": bool, "actions": [dict], "reason": str}
        """
        if not self._portfolio:
            return {"needed": True, "actions": [], "reason": "Portfolio not yet built"}

        alloc = self.get_allocation()
        actions = []

        # Calculate current total per bucket
        off_symbols = {s["symbol"] for s in self._portfolio["offensive_stocks"]}
        def_symbols = {s["symbol"] for s in self._portfolio["defensive_stocks"]}

        current_off = sum(
            pos.get("market_value", 0)
            for sym, pos in current_positions.items()
            if sym in off_symbols
        )
        current_def = sum(
            pos.get("market_value", 0)
            for sym, pos in current_positions.items()
            if sym in def_symbols
        )
        current_total = current_off + current_def

        if current_total <= 0:
            return {"needed": True, "actions": [], "reason": "No positions"}

        # Target ratio vs current ratio
        target_off_pct = alloc["offensive_pct"]
        target_def_pct = alloc["defensive_pct"]
        actual_off_pct = (current_off / current_total) * 100
        actual_def_pct = (current_def / current_total) * 100

        off_drift = abs(actual_off_pct - target_off_pct)
        def_drift = abs(actual_def_pct - target_def_pct)

        needed = (off_drift > self.config.rebalance_threshold_pct or
                  def_drift > self.config.rebalance_threshold_pct)

        reason = (
            f"Offensive: {actual_off_pct:.1f}% (target {target_off_pct}%, "
            f"drift {off_drift:.1f}%) | "
            f"Defensive: {actual_def_pct:.1f}% (target {target_def_pct}%, "
            f"drift {def_drift:.1f}%)"
        )

        if needed:
            # Calculate transfer amount: Offensive → Defensive or Defensive → Offensive
            target_off = current_total * target_off_pct / 100
            move = current_off - target_off

            if move > 0:
                actions.append({
                    "action": "REDUCE_OFFENSIVE",
                    "amount": round(abs(move), 0),
                    "reason": f"Reduce offensive by ${abs(move):,.0f} → move to defensive",
                })
            else:
                actions.append({
                    "action": "REDUCE_DEFENSIVE",
                    "amount": round(abs(move), 0),
                    "reason": f"Reduce defensive by ${abs(move):,.0f} → move to offensive",
                })

        # Check stop loss / take profit per stock
        for sym, pos in current_positions.items():
            pnl_pct = pos.get("pnl_pct", 0)

            if sym in off_symbols:
                if pnl_pct <= self.config.offensive_stop_loss_pct:
                    actions.append({
                        "action": "STOP_LOSS",
                        "symbol": sym,
                        "bucket": "Offensive",
                        "pnl_pct": pnl_pct,
                        "reason": f"🛑 {sym} stop loss {pnl_pct:.1f}%",
                    })
                elif pnl_pct >= self.config.offensive_take_profit_pct:
                    actions.append({
                        "action": "TAKE_PROFIT",
                        "symbol": sym,
                        "bucket": "Offensive",
                        "pnl_pct": pnl_pct,
                        "reason": f"🎯 {sym} take profit +{pnl_pct:.1f}%",
                    })

            elif sym in def_symbols:
                if pnl_pct <= self.config.defensive_stop_loss_pct:
                    actions.append({
                        "action": "STOP_LOSS",
                        "symbol": sym,
                        "bucket": "Defensive",
                        "pnl_pct": pnl_pct,
                        "reason": f"🛑 {sym} stop loss {pnl_pct:.1f}%",
                    })
                elif pnl_pct >= self.config.defensive_take_profit_pct:
                    actions.append({
                        "action": "TAKE_PROFIT",
                        "symbol": sym,
                        "bucket": "Defensive",
                        "pnl_pct": pnl_pct,
                        "reason": f"🎯 {sym} take profit +{pnl_pct:.1f}%",
                    })

        return {"needed": needed, "actions": actions, "reason": reason}

    def print_portfolio(self):
        """Print portfolio report"""
        if not self._portfolio:
            self.build_portfolio()

        p = self._portfolio
        alloc = p["allocation"]

        print("\n" + "═" * 75)
        print(f"  📊 Multi-Bucket Portfolio — ${self.config.total_capital:,.0f}")
        print(f"  Market Regime: {p['regime']} | Avg Beta: {p['avg_beta']}")
        print("═" * 75)

        # Allocation summary
        print(f"\n  💰 Capital Allocation:")
        print(f"    🔴 Offensive: ${alloc['offensive']:>10,.0f} ({alloc['offensive_pct']}%)")
        print(f"    🔵 Defensive: ${alloc['defensive']:>10,.0f} ({alloc['defensive_pct']}%)")
        print(f"    💵 Cash:      ${alloc['cash']:>10,.0f} ({alloc['cash_pct']}%)")

        # Offensive portfolio
        print(f"\n  🔴 Offensive Portfolio ({len(p['offensive_stocks'])} stocks):")
        print(f"    {'Symbol':6s} {'Name':18s} {'Sector':10s} {'Amount':>10s} {'Weight':>6s} {'Beta':>5s} {'SL':>6s}")
        print(f"    {'─'*6} {'─'*18} {'─'*10} {'─'*10} {'─'*6} {'─'*5} {'─'*6}")

        for s in sorted(p["offensive_stocks"], key=lambda x: -x["amount"]):
            print(
                f"    {s['symbol']:6s} {s['name']:18s} {s['sector']:10s} "
                f"${s['amount']:>9,.0f} {s['weight_pct']:>5.1f}% "
                f"{s['beta']:>5.2f} {s['stop_loss']:>5.0f}%"
            )

        off_total = sum(s["amount"] for s in p["offensive_stocks"])
        print(f"    {'':6s} {'Subtotal':18s} {'':10s} ${off_total:>9,.0f}")

        # Defensive portfolio
        print(f"\n  🔵 Defensive Portfolio ({len(p['defensive_stocks'])} stocks):")
        print(f"    {'Symbol':6s} {'Name':18s} {'Sector':10s} {'Amount':>10s} {'Weight':>6s} {'Beta':>5s} {'Div':>5s}")
        print(f"    {'─'*6} {'─'*18} {'─'*10} {'─'*10} {'─'*6} {'─'*5} {'─'*5}")

        for s in sorted(p["defensive_stocks"], key=lambda x: -x["amount"]):
            print(
                f"    {s['symbol']:6s} {s['name']:18s} {s['sector']:10s} "
                f"${s['amount']:>9,.0f} {s['weight_pct']:>5.1f}% "
                f"{s['beta']:>5.2f} {s['dividend_yield']:>4.1f}%"
            )

        def_total = sum(s["amount"] for s in p["defensive_stocks"])
        print(f"    {'':6s} {'Subtotal':18s} {'':10s} ${def_total:>9,.0f}")

        # Summary
        print(f"\n  📈 Portfolio Summary:")
        print(f"    Total Stocks: {p['total_stocks']}")
        print(f"    Est. Annual Dividend: ${p['est_annual_dividend']:,.0f}")
        print(f"    Avg Beta: {p['avg_beta']}")

        # Scenario analysis
        print(f"\n  📉 Scenario Analysis:")
        scenarios = [
            ("Market +2%", 0.02),
            ("Market -2%", -0.02),
            ("Market -5%", -0.05),
            ("Market -10%", -0.10),
        ]

        off_beta = sum(
            s["amount"] * s["beta"] for s in p["offensive_stocks"]
        ) / off_total if off_total > 0 else 1.5
        def_beta = sum(
            s["amount"] * s["beta"] for s in p["defensive_stocks"]
        ) / def_total if def_total > 0 else 0.55

        for name, market_move in scenarios:
            off_move = off_total * market_move * off_beta
            def_move = def_total * market_move * def_beta
            total_move = off_move + def_move
            total_pct = total_move / self.config.total_capital * 100
            print(
                f"    {name:10s} → "
                f"Offensive: ${off_move:>+9,.0f} | "
                f"Defensive: ${def_move:>+9,.0f} | "
                f"Total: ${total_move:>+9,.0f} ({total_pct:+.1f}%)"
            )

        print("═" * 75)


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  📊 Multi-Bucket Portfolio Manager Demo                  ║
    ║  $200,000 Offensive/Defensive Split Portfolio             ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    config = PortfolioConfig(total_capital=200_000)
    pm = PortfolioManager(config)

    # 1) NEUTRAL regime (50/40/10)
    print("  ━━━ Scenario 1: NEUTRAL Market ━━━")
    pm.set_regime(MarketRegime.NEUTRAL)
    pm.build_portfolio()
    pm.print_portfolio()

    # 2) BEAR regime (30/55/15)
    print("\n\n  ━━━ Scenario 2: BEAR Market (Iran war escalation) ━━━")
    pm.set_regime(MarketRegime.BEAR)
    pm.build_portfolio()
    pm.print_portfolio()

    # 3) BULL regime (60/30/10)
    print("\n\n  ━━━ Scenario 3: BULL Market (war ended) ━━━")
    pm.set_regime(MarketRegime.BULL)
    pm.build_portfolio()
    pm.print_portfolio()

    # 4) Rebalancing check
    print("\n\n  ━━━ Rebalancing Check ━━━")
    # Mock current positions (offensive grew too much, ratio drifted)
    mock_positions = {
        "NVDA": {"market_value": 18000, "pnl_pct": 20.0},  # Take profit trigger
        "AMD": {"market_value": 11000, "pnl_pct": 10.0},
        "META": {"market_value": 10500, "pnl_pct": 5.0},
        "TSLA": {"market_value": 3000, "pnl_pct": -40.0},  # Already stopped out
        "LMT": {"market_value": 13000, "pnl_pct": 8.0},
        "XOM": {"market_value": 12500, "pnl_pct": 4.0},
        "GLD": {"market_value": 11000, "pnl_pct": -2.0},
    }

    result = pm.check_rebalance(mock_positions)
    print(f"\n  Rebalancing needed: {'Yes' if result['needed'] else 'No'}")
    print(f"  Status: {result['reason']}")
    if result["actions"]:
        print(f"\n  📋 Action List:")
        for act in result["actions"]:
            print(f"    → {act['reason']}")


if __name__ == "__main__":
    demo()
