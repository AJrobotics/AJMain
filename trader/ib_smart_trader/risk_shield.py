"""
═══════════════════════════════════════════════════════════════════
  Risk Shield Module - Risk Defense System

  3 Defense Strategies Learned from the AVAV Incident:
    1. Earnings Calendar Filter - Block buys on announcement day + Reduce positions
    2. Beta-Based Position Sizing - Reduce investment for high-volatility stocks
    3. Earnings Miss Pattern Filter - Penalize/exclude chronic miss stocks

  + Additional Defenses:
    4. Sector Concentration Limit - Set max stocks per sector
    5. Daily Loss Limit - Limit total portfolio loss

  Integrated with Smart Trader and Auto Screener
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
    HAS_IB = True
except ImportError:
    HAS_IB = False
    IB = None


logger = logging.getLogger("RiskShield")


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskShieldConfig:
    """Risk defense configuration"""

    # ── 1. Earnings Calendar ──
    earnings_block_days_before: int = 1     # Block buys N days before announcement
    earnings_block_days_after: int = 1      # Block buys N days after announcement
    earnings_reduce_position_pct: float = 50.0  # Position reduction percentage before announcement (%)
    earnings_enabled: bool = True

    # ── 2. Beta-Based Position Sizing ──
    beta_enabled: bool = True
    base_investment: float = 10000.0        # Base investment per stock
    beta_neutral: float = 1.0               # Reference Beta (market average)
    beta_scale_factor: float = 0.5          # Scaling intensity (0=ignore, 1=fully proportional)
    beta_max_multiplier: float = 1.5        # Max investment multiplier (low-Beta stocks)
    beta_min_multiplier: float = 0.3        # Min investment multiplier (high-Beta stocks)

    # ── 3. Earnings Miss Pattern ──
    miss_pattern_enabled: bool = True
    miss_lookback_quarters: int = 4         # Check last N quarters
    miss_threshold: int = 2                 # Warning at N or more misses
    miss_penalty_score: float = 20.0        # Screener score penalty
    miss_block_threshold: int = 3           # Fully block buys at N or more misses

    # ── 4. Sector Concentration Limit ──
    sector_limit_enabled: bool = True
    max_stocks_per_sector: int = 3          # Max stocks per sector

    # ── 5. Daily Loss Limit ──
    daily_loss_limit_enabled: bool = True
    daily_loss_limit_pct: float = -3.0      # Stop all trading at portfolio -3%


# ═══════════════════════════════════════════════════════════════
#  Risk Check Result
# ═══════════════════════════════════════════════════════════════

class RiskAction(Enum):
    ALLOW = "✅ Allow"
    REDUCE = "⚠️ Reduce"
    BLOCK = "🚫 Block"


@dataclass
class RiskCheckResult:
    """Risk check result"""
    symbol: str
    action: RiskAction = RiskAction.ALLOW
    reasons: list = field(default_factory=list)
    adjusted_investment: float = 0.0   # Investment after Beta adjustment
    position_reduce_pct: float = 0.0   # Position reduction percentage
    score_penalty: float = 0.0         # Screener score penalty

    # Detailed info
    has_earnings_soon: bool = False
    earnings_date: str = ""
    beta: float = 1.0
    earnings_miss_count: int = 0
    sector: str = ""
    sector_count: int = 0

    def __str__(self):
        flags = " | ".join(self.reasons) if self.reasons else "No risk"
        return (
            f"{self.action.value} {self.symbol} | "
            f"Investment: ${self.adjusted_investment:,.0f} | "
            f"Beta: {self.beta:.2f} | "
            f"Misses: {self.earnings_miss_count}/{4}Q | "
            f"{flags}"
        )


# ═══════════════════════════════════════════════════════════════
#  1. Earnings Calendar Filter
# ═══════════════════════════════════════════════════════════════

class EarningsCalendarFilter:
    """
    Restrict trading around earnings announcements

    AVAV lesson: Holding/buying on the earnings announcement day
    can result in a full -14% after-hours crash.
    → Reduce positions before announcement, block buys on announcement day
    """

    def __init__(self, ib=None):
        self.ib = ib
        self._cache = {}  # {symbol: earnings_date}

    def get_next_earnings_date(self, symbol: str) -> Optional[datetime]:
        """
        Query next earnings date via IB API
        Uses cache to minimize API calls
        """
        if symbol in self._cache:
            cached = self._cache[symbol]
            # Reuse cache if the date is in the future
            if cached and cached > datetime.now():
                return cached

        if self.ib is None or not self.ib.isConnected():
            return None

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # Query earnings schedule from IB's fundamentalData
            # Or use nextEarningsDate from reqContractDetails
            details = self.ib.reqContractDetails(contract)
            if details:
                # If provided by IB
                for d in details:
                    # contractDetails may contain earningsDate
                    pass

            # Fallback: Use Wall Street Horizon or own data
            # Return None if unable to retrieve from IB
            self._cache[symbol] = None
            return None

        except Exception as e:
            logger.warning(f"  ⚠️ {symbol} earnings schedule query failed: {e}")
            return None

    def set_earnings_date(self, symbol: str, date: datetime):
        """Manually set earnings date (when API is unavailable)"""
        self._cache[symbol] = date

    def load_earnings_calendar(self, calendar: dict):
        """
        Bulk load earnings calendar

        Format: {"AVAV": "2026-03-10", "MU": "2026-03-18", ...}
        """
        for symbol, date_str in calendar.items():
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                self._cache[symbol] = dt
            except ValueError:
                pass

        logger.info(f"  📅 Earnings calendar loaded: {len(calendar)} stocks")

    def check(
        self,
        symbol: str,
        config: RiskShieldConfig,
    ) -> dict:
        """
        Check proximity to earnings announcement

        Returns:
            {
                "has_earnings_soon": bool,
                "earnings_date": str,
                "action": "BLOCK" | "REDUCE" | "ALLOW",
                "reduce_pct": float,
            }
        """
        if not config.earnings_enabled:
            return {"has_earnings_soon": False, "action": "ALLOW", "reduce_pct": 0}

        earnings_date = self.get_next_earnings_date(symbol)

        # Check directly from cache
        if earnings_date is None and symbol in self._cache:
            earnings_date = self._cache.get(symbol)

        if earnings_date is None:
            return {"has_earnings_soon": False, "action": "ALLOW", "reduce_pct": 0}

        now = datetime.now()
        days_until = (earnings_date - now).days

        # Announcement day or just before: block buys
        if -config.earnings_block_days_after <= days_until <= config.earnings_block_days_before:
            return {
                "has_earnings_soon": True,
                "earnings_date": earnings_date.strftime("%Y-%m-%d"),
                "action": "BLOCK",
                "reduce_pct": config.earnings_reduce_position_pct,
                "days_until": days_until,
            }

        # 2~3 days before announcement: recommend position reduction
        if config.earnings_block_days_before < days_until <= config.earnings_block_days_before + 2:
            return {
                "has_earnings_soon": True,
                "earnings_date": earnings_date.strftime("%Y-%m-%d"),
                "action": "REDUCE",
                "reduce_pct": config.earnings_reduce_position_pct * 0.5,
                "days_until": days_until,
            }

        return {
            "has_earnings_soon": False,
            "earnings_date": earnings_date.strftime("%Y-%m-%d") if earnings_date else "",
            "action": "ALLOW",
            "reduce_pct": 0,
        }


# ═══════════════════════════════════════════════════════════════
#  2. Beta-Based Position Sizing
# ═══════════════════════════════════════════════════════════════

class BetaPositionSizer:
    """
    Dynamic investment adjustment based on Beta

    AVAV lesson: Investing the same amount in a stock with Beta 2.21
    means a -14% drop hits the portfolio 2x harder than a Beta 1.0 stock.
    → Reduce investment for high-Beta stocks to equalize risk

    Formula:
      Adjusted investment = Base investment x (Reference Beta / Stock Beta)^Scale Factor
      → Beta 2.0 stock: $10,000 x (1.0/2.0)^0.5 = $7,071
      → Beta 0.5 stock: $10,000 x (1.0/0.5)^0.5 = $14,142 (cap applied)
    """

    # Known Beta values for major stocks (predefined + dynamically updatable)
    KNOWN_BETAS = {
        # Tech / AI
        "NVDA": 1.95, "AAPL": 1.18, "MSFT": 1.05, "GOOGL": 1.10,
        "AMZN": 1.15, "META": 1.35, "TSLA": 2.05, "TSM": 1.30,
        "AMD": 1.72, "INTC": 1.08, "MU": 1.45, "PLTR": 2.10,
        "APP": 2.50, "AVGO": 1.40, "ORCL": 1.12, "CRM": 1.25,

        # Energy
        "XOM": 0.85, "CVX": 0.90, "COP": 1.05, "OXY": 1.60,
        "DVN": 1.75, "EOG": 1.20, "SLB": 1.35, "BP": 0.80,
        "MPC": 1.15, "VLO": 1.30, "PSX": 1.10,

        # Defense
        "LMT": 0.55, "NOC": 0.50, "RTX": 0.75, "GD": 0.60,
        "BA": 1.45, "LHX": 0.70, "AVAV": 2.21, "KTOS": 1.80,

        # Tankers
        "FRO": 1.90, "DHT": 1.70, "INSW": 1.65, "STNG": 1.85,

        # Nuclear/Utilities
        "CEG": 1.30, "VST": 1.25, "GEV": 1.15, "NEE": 0.65,

        # Financials
        "JPM": 1.10, "GS": 1.35, "MS": 1.40, "BAC": 1.30,
        "WFC": 1.15, "BRK-B": 0.55, "AXP": 1.20,

        # Healthcare
        "UNH": 0.70, "JNJ": 0.55, "LLY": 0.75, "PFE": 0.65,
        "ABBV": 0.60, "MRK": 0.50, "AMGN": 0.55,

        # Consumer
        "WMT": 0.50, "COST": 0.75, "HD": 1.05, "TGT": 1.10,

        # Fintech
        "HOOD": 2.30, "MSTR": 3.50, "COIN": 2.80, "SQ": 2.15,
    }

    @classmethod
    def get_beta(cls, symbol: str) -> float:
        """Return stock Beta (default 1.0 if not registered)"""
        return cls.KNOWN_BETAS.get(symbol, 1.0)

    @classmethod
    def calculate_position_size(
        cls,
        symbol: str,
        config: RiskShieldConfig,
    ) -> dict:
        """
        Calculate Beta-adjusted investment

        Returns:
            {
                "beta": float,
                "base_investment": float,
                "adjusted_investment": float,
                "multiplier": float,
                "risk_level": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
            }
        """
        if not config.beta_enabled:
            return {
                "beta": 1.0,
                "base_investment": config.base_investment,
                "adjusted_investment": config.base_investment,
                "multiplier": 1.0,
                "risk_level": "MEDIUM",
            }

        beta = cls.get_beta(symbol)

        # Calculate adjustment multiplier: (Reference Beta / Stock Beta) ^ Scale Factor
        if beta > 0:
            raw_multiplier = (config.beta_neutral / beta) ** config.beta_scale_factor
        else:
            raw_multiplier = 1.0

        # Min/Max clamp
        multiplier = max(
            config.beta_min_multiplier,
            min(config.beta_max_multiplier, raw_multiplier)
        )

        adjusted = config.base_investment * multiplier

        # Risk level
        if beta <= 0.7:
            risk_level = "LOW"
        elif beta <= 1.3:
            risk_level = "MEDIUM"
        elif beta <= 2.0:
            risk_level = "HIGH"
        else:
            risk_level = "EXTREME"

        return {
            "beta": beta,
            "base_investment": config.base_investment,
            "adjusted_investment": round(adjusted, 2),
            "multiplier": round(multiplier, 3),
            "risk_level": risk_level,
        }


# ═══════════════════════════════════════════════════════════════
#  3. Earnings Miss Pattern Filter
# ═══════════════════════════════════════════════════════════════

class EarningsMissFilter:
    """
    Detect recent N-quarter earnings miss patterns

    AVAV lesson: 3 misses out of last 4 quarters → high probability of another miss.
    → Penalize or exclude chronic miss stocks from screener
    """

    # Recent 4-quarter earnings miss history (True = miss)
    # In production, auto-updated via IB API or external data
    KNOWN_MISS_HISTORY = {
        "AVAV": [True, True, False, True],    # 3 out of 4 quarters missed!
        "TSLA": [False, True, False, False],
        "INTC": [True, True, False, True],
        "BA":   [True, False, True, False],
        "PLTR": [False, False, False, False],  # No misses
        "NVDA": [False, False, False, False],
        "XOM":  [False, False, True, False],
        "LMT":  [False, False, False, False],
        "HOOD": [False, True, False, True],
        "MSTR": [True, False, True, False],
    }

    @classmethod
    def get_miss_count(cls, symbol: str) -> int:
        """Number of earnings misses in the last 4 quarters"""
        history = cls.KNOWN_MISS_HISTORY.get(symbol, [])
        return sum(1 for x in history if x)

    @classmethod
    def check(cls, symbol: str, config: RiskShieldConfig) -> dict:
        """
        Check earnings miss pattern

        Returns:
            {
                "miss_count": int,
                "miss_rate": float (0~1),
                "action": "BLOCK" | "PENALIZE" | "ALLOW",
                "penalty": float (score penalty),
            }
        """
        if not config.miss_pattern_enabled:
            return {"miss_count": 0, "action": "ALLOW", "penalty": 0}

        miss_count = cls.get_miss_count(symbol)
        total_quarters = config.miss_lookback_quarters
        miss_rate = miss_count / total_quarters if total_quarters > 0 else 0

        # 3 or more misses: fully block buys
        if miss_count >= config.miss_block_threshold:
            return {
                "miss_count": miss_count,
                "miss_rate": miss_rate,
                "action": "BLOCK",
                "penalty": config.miss_penalty_score * 2,
                "reason": f"🚫 {miss_count}/{total_quarters}Q missed — chronic miss stock blocked",
            }

        # 2 misses: score penalty
        if miss_count >= config.miss_threshold:
            return {
                "miss_count": miss_count,
                "miss_rate": miss_rate,
                "action": "PENALIZE",
                "penalty": config.miss_penalty_score,
                "reason": f"⚠️ {miss_count}/{total_quarters}Q missed — score -{config.miss_penalty_score}",
            }

        return {
            "miss_count": miss_count,
            "miss_rate": miss_rate,
            "action": "ALLOW",
            "penalty": 0,
        }


# ═══════════════════════════════════════════════════════════════
#  4. Sector Concentration Limit
# ═══════════════════════════════════════════════════════════════

class SectorLimiter:
    """
    Prevent excessive exposure to a single sector

    Holding 4 stocks in the Defense sector (LMT, NOC, RTX, AVAV)
    → All get hit at once by sector-wide headwinds
    → Limit to 2~3 stocks per sector
    """

    SECTOR_MAP = {
        "Tech/AI": ["NVDA","AAPL","MSFT","GOOGL","AMZN","META","TSLA","TSM","AMD","INTC","MU","PLTR","APP","AVGO","ORCL","CRM"],
        "Energy": ["XOM","CVX","COP","OXY","DVN","EOG","SLB","BP","MPC","VLO","PSX","HES","HAL","SHEL","PXD"],
        "Defense": ["LMT","NOC","RTX","GD","BA","LHX","AVAV","KTOS"],
        "Tankers": ["FRO","DHT","INSW","STNG","TNK"],
        "Nuclear": ["CEG","VST","GEV","NRG","NEE"],
        "Financials": ["JPM","GS","MS","BAC","WFC","BRK-B","AXP"],
        "Healthcare": ["UNH","JNJ","LLY","PFE","ABBV","MRK","AMGN"],
        "Consumer": ["WMT","COST","HD","TGT","LULU","NKE"],
        "Fintech": ["HOOD","MSTR","COIN","SQ"],
    }

    @classmethod
    def get_sector(cls, symbol: str) -> str:
        for sector, symbols in cls.SECTOR_MAP.items():
            if symbol in symbols:
                return sector
        return "Other"

    @classmethod
    def check(
        cls,
        symbol: str,
        current_holdings: list,
        config: RiskShieldConfig,
    ) -> dict:
        """
        Check whether sector limit is exceeded

        Parameters:
            symbol: Target stock for purchase
            current_holdings: List of currently held stocks ["LMT", "NOC", ...]
        """
        if not config.sector_limit_enabled:
            return {"action": "ALLOW", "sector": "", "count": 0}

        sector = cls.get_sector(symbol)
        sector_holdings = [s for s in current_holdings if cls.get_sector(s) == sector]
        count = len(sector_holdings)

        if count >= config.max_stocks_per_sector:
            return {
                "action": "BLOCK",
                "sector": sector,
                "count": count,
                "holdings": sector_holdings,
                "reason": f"🚫 {sector} sector {count}/{config.max_stocks_per_sector} exceeded",
            }

        return {
            "action": "ALLOW",
            "sector": sector,
            "count": count,
        }


# ═══════════════════════════════════════════════════════════════
#  Integrated Risk Check Engine
# ═══════════════════════════════════════════════════════════════

class RiskShield:
    """
    Integrated Risk Defense System

    Runs all risk filters at once and makes the final decision.
    This check must be passed before any buy in Smart Trader.
    """

    def __init__(self, config: RiskShieldConfig = None, ib=None):
        self.config = config or RiskShieldConfig()
        self.earnings_filter = EarningsCalendarFilter(ib)
        self.daily_pnl = 0.0
        self.initial_portfolio = 0.0

    def set_daily_baseline(self, portfolio_value: float):
        """Set portfolio baseline value at market open"""
        self.initial_portfolio = portfolio_value
        self.daily_pnl = 0.0

    def update_daily_pnl(self, pnl: float):
        """Update daily P&L"""
        self.daily_pnl += pnl

    def check_daily_limit(self) -> bool:
        """Check whether daily loss limit is exceeded"""
        if not self.config.daily_loss_limit_enabled or self.initial_portfolio <= 0:
            return False

        pnl_pct = (self.daily_pnl / self.initial_portfolio) * 100
        return pnl_pct <= self.config.daily_loss_limit_pct

    def full_check(
        self,
        symbol: str,
        current_holdings: list = None,
    ) -> RiskCheckResult:
        """
        Run full risk check

        Order:
        1. Daily loss limit check
        2. Earnings calendar check
        3. Earnings miss pattern check
        4. Beta position sizing
        5. Sector concentration check

        → If any is BLOCK, final decision is BLOCK
        → If any is REDUCE, final decision is REDUCE
        → If all are ALLOW, final decision is ALLOW
        """
        current_holdings = current_holdings or []
        result = RiskCheckResult(symbol=symbol)
        reasons = []
        final_action = RiskAction.ALLOW

        # ── 0. Daily loss limit ──
        if self.check_daily_limit():
            result.action = RiskAction.BLOCK
            result.reasons = [f"🛑 Daily loss limit exceeded ({self.daily_pnl/self.initial_portfolio*100:.1f}%)"]
            result.adjusted_investment = 0
            return result

        # ── 1. Earnings calendar ──
        earnings = self.earnings_filter.check(symbol, self.config)
        result.has_earnings_soon = earnings.get("has_earnings_soon", False)
        result.earnings_date = earnings.get("earnings_date", "")

        if earnings["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            days = earnings.get("days_until", 0)
            reasons.append(
                f"📅 Earnings {'today' if days == 0 else f'in {days} day(s)'} "
                f"({result.earnings_date}) → buy blocked"
            )
            result.position_reduce_pct = earnings.get("reduce_pct", 0)
        elif earnings["action"] == "REDUCE":
            if final_action != RiskAction.BLOCK:
                final_action = RiskAction.REDUCE
            reasons.append(
                f"📅 Earnings in {earnings.get('days_until', '?')} day(s) → reduce position"
            )
            result.position_reduce_pct = earnings.get("reduce_pct", 0)

        # ── 2. Earnings miss pattern ──
        miss = EarningsMissFilter.check(symbol, self.config)
        result.earnings_miss_count = miss["miss_count"]

        if miss["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            reasons.append(miss.get("reason", "Chronic miss blocked"))
            result.score_penalty = miss["penalty"]
        elif miss["action"] == "PENALIZE":
            reasons.append(miss.get("reason", "Earnings miss penalty"))
            result.score_penalty = miss["penalty"]

        # ── 3. Beta position sizing ──
        sizing = BetaPositionSizer.calculate_position_size(symbol, self.config)
        result.beta = sizing["beta"]
        result.adjusted_investment = sizing["adjusted_investment"]

        if sizing["risk_level"] == "EXTREME":
            reasons.append(
                f"🔴 Beta {sizing['beta']:.2f} (extreme risk) → "
                f"Investment ${sizing['adjusted_investment']:,.0f} "
                f"({sizing['multiplier']:.0%} of base)"
            )
        elif sizing["risk_level"] == "HIGH":
            reasons.append(
                f"🟡 Beta {sizing['beta']:.2f} (high risk) → "
                f"Investment ${sizing['adjusted_investment']:,.0f}"
            )

        # ── 4. Sector concentration ──
        sector = SectorLimiter.check(symbol, current_holdings, self.config)
        result.sector = sector.get("sector", "")
        result.sector_count = sector.get("count", 0)

        if sector["action"] == "BLOCK":
            final_action = RiskAction.BLOCK
            reasons.append(sector.get("reason", "Sector limit exceeded"))

        # ── Final decision ──
        # Investment = 0 when BLOCK
        if final_action == RiskAction.BLOCK:
            result.adjusted_investment = 0.0

        result.action = final_action
        result.reasons = reasons

        return result

    def print_report(self, results: list):
        """Print risk check report"""
        print("\n" + "═" * 70)
        print("  🛡️ Risk Shield Report")
        print("═" * 70)

        blocked = [r for r in results if r.action == RiskAction.BLOCK]
        reduced = [r for r in results if r.action == RiskAction.REDUCE]
        allowed = [r for r in results if r.action == RiskAction.ALLOW]

        if blocked:
            print(f"\n  🚫 Blocked ({len(blocked)} stocks):")
            for r in blocked:
                print(f"    {r}")

        if reduced:
            print(f"\n  ⚠️ Reduced ({len(reduced)} stocks):")
            for r in reduced:
                print(f"    {r}")

        print(f"\n  ✅ Allowed ({len(allowed)} stocks):")
        for r in allowed:
            beta_note = ""
            if r.beta > 1.5:
                beta_note = f" [Beta {r.beta:.1f} → ${r.adjusted_investment:,.0f}]"
            print(f"    {r.symbol:6s} ${r.adjusted_investment:>8,.0f}{beta_note}")

        total_investment = sum(r.adjusted_investment for r in results if r.action != RiskAction.BLOCK)
        print(f"\n  💰 Total investment: ${total_investment:,.0f}")
        print("═" * 70)


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    """AVAV incident simulation"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🛡️ Risk Shield Demo — AVAV Earnings Day Simulation      ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    config = RiskShieldConfig()
    shield = RiskShield(config)

    # Load earnings calendar (AVAV: 3/10, MU: 3/18)
    shield.earnings_filter.load_earnings_calendar({
        "AVAV": "2026-03-10",
        "MU": "2026-03-18",
        "LULU": "2026-03-17",
    })

    # Currently held stocks
    current_holdings = ["LMT", "NOC", "RTX", "XOM", "CVX"]

    # Risk check 10 buy candidates
    candidates = ["XOM", "CVX", "COP", "DVN", "LMT", "NOC", "AVAV", "FRO", "OXY", "NVDA"]

    print("  📋 Risk check for 10 buy candidates:\n")
    results = []

    for sym in candidates:
        result = shield.full_check(sym, current_holdings)
        results.append(result)

        icon = {"✅ Allow": "✅", "⚠️ Reduce": "⚠️", "🚫 Block": "🚫"}[result.action.value]
        print(f"  {icon} {sym:6s} | Beta: {result.beta:4.2f} | "
              f"Investment: ${result.adjusted_investment:>8,.0f} | "
              f"Misses: {result.earnings_miss_count}/4")
        for reason in result.reasons:
            print(f"           {reason}")
        print()

    shield.print_report(results)


if __name__ == "__main__":
    demo()
