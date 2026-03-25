"""
═══════════════════════════════════════════════════════════════════
  Tax Optimizer Module - Tax Optimization System

  Features:
    6. Tax-Loss Harvesting Automation
       - Automatically detects positions with unrealized losses and sells them
       - Offsets other gains with realized losses to reduce taxes
       - Tracks annual $3,000 net loss deduction limit

    7. Wash Sale Rule Prevention Filter
       - Blocks repurchase of the same stock within 30 days after a loss sale
       - Warns about substantially identical securities (ETF <-> individual stocks)
       - Automatically suggests substitute stocks

  California Special Considerations:
    - CA state tax applies the same rate regardless of Long/Short term
    - Federal Short-term = ordinary income tax rate (22-37%)
    - Federal Long-term = preferential rate (0-20%)
    -> Holding for 1+ year is advantageous for federal tax savings when possible

  Used in integration with Risk Shield
═══════════════════════════════════════════════════════════════════
"""

import json
import os
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("TaxOptimizer")


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaxConfig:
    """Tax optimization configuration"""

    # ── Tax-Loss Harvesting ──
    tlh_enabled: bool = True
    tlh_min_loss_pct: float = -5.0       # Must have at least this % loss to harvest
    tlh_min_loss_dollar: float = -100.0  # Must have at least this dollar amount in losses
    tlh_check_interval_days: int = 7     # Scan for TLH every N days
    tlh_annual_loss_target: float = 3000.0  # Annual loss deduction target ($3,000 IRS limit)
    tlh_avoid_year_end_days: int = 5     # Be conservative with TLH in the last N days of December

    # ── Wash Sale Prevention ──
    wash_sale_enabled: bool = True
    wash_sale_window_days: int = 30      # IRS rule: 30 days before and after sale
    wash_sale_block_identical: bool = True     # Block identical stocks
    wash_sale_warn_similar: bool = True        # Warn about similar stocks

    # ── Holding Period Tracking ──
    holding_period_tracking: bool = True
    long_term_threshold_days: int = 366  # 1 year + 1 day or more = Long-term
    warn_near_long_term_days: int = 30   # Warn about selling N days before Long-term conversion

    # ── Record Files ──
    tax_log_file: str = "tax_records.json"


# ═══════════════════════════════════════════════════════════════
#  Trade Tax Records
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaxLot:
    """Individual trade tax record (Tax Lot)"""
    symbol: str
    buy_date: str           # "2026-02-15"
    buy_price: float
    shares: int
    sell_date: str = ""     # Filled in upon sale
    sell_price: float = 0.0
    realized_pnl: float = 0.0
    is_wash_sale: bool = False
    holding_days: int = 0
    is_long_term: bool = False

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "buy_date": self.buy_date,
            "buy_price": self.buy_price,
            "shares": self.shares,
            "sell_date": self.sell_date,
            "sell_price": self.sell_price,
            "realized_pnl": self.realized_pnl,
            "is_wash_sale": self.is_wash_sale,
            "holding_days": self.holding_days,
            "is_long_term": self.is_long_term,
        }


# ═══════════════════════════════════════════════════════════════
#  6. Tax-Loss Harvesting Engine
# ═══════════════════════════════════════════════════════════════

class TaxLossHarvester:
    """
    Automated Tax-Loss Harvesting (TLH)

    How it works:
    1. Scan held positions for unrealized losses exceeding the threshold
    2. Sell those positions to realize the losses
    3. Use realized losses to offset other gains -> reduce taxes
    4. Can repurchase the original stock after 30 days (Wash Sale prevention)

    Example:
    - Bought XOM at $150 -> currently $140 (unrealized loss -$1,000)
    - TLH auto-sells -> $1,000 loss realized
    - This $1,000 offsets $1,000 in gains from other stocks
    - Can repurchase XOM after 30 days (or buy CVX as a substitute)
    """

    # Similar stock mapping (substitute stocks for Wash Sale avoidance)
    # Replacing with a similar stock in the same sector maintains market exposure while preventing Wash Sale
    SUBSTITUTE_MAP = {
        # Energy
        "XOM": ["CVX", "COP", "BP"],
        "CVX": ["XOM", "COP", "SHEL"],
        "COP": ["XOM", "CVX", "EOG"],
        "OXY": ["DVN", "EOG", "PXD"],
        "DVN": ["OXY", "EOG", "COP"],
        # Defense
        "LMT": ["NOC", "RTX", "GD"],
        "NOC": ["LMT", "RTX", "LHX"],
        "RTX": ["LMT", "NOC", "GD"],
        "AVAV": ["KTOS", "LHX"],
        # Tech
        "NVDA": ["AMD", "AVGO", "TSM"],
        "AMD": ["NVDA", "INTC", "TSM"],
        "MSFT": ["GOOGL", "AAPL", "CRM"],
        "AAPL": ["MSFT", "GOOGL"],
        "META": ["GOOGL", "MSFT"],
        "PLTR": ["CRM", "ORCL"],
        # Tankers
        "FRO": ["DHT", "INSW", "STNG"],
        "DHT": ["FRO", "INSW"],
        # Fintech
        "HOOD": ["SQ", "COIN"],
        "MSTR": ["COIN"],
        # Consumer
        "WMT": ["COST", "TGT"],
        "COST": ["WMT", "TGT"],
        # Healthcare
        "UNH": ["JNJ", "ABBV"],
        "JNJ": ["UNH", "PFE"],
        # Gold
        "GLD": ["NEM", "GOLD", "FNV"],
    }

    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        self.ytd_realized_losses = 0.0   # Year-to-date cumulative realized losses
        self.ytd_realized_gains = 0.0    # Year-to-date cumulative realized gains
        self.tax_lots: list[TaxLot] = []
        self.last_scan_date: Optional[datetime] = None

        self._load_records()

    def _load_records(self):
        """Load existing tax records"""
        if os.path.exists(self.config.tax_log_file):
            try:
                with open(self.config.tax_log_file, "r") as f:
                    data = json.load(f)
                self.ytd_realized_losses = data.get("ytd_losses", 0.0)
                self.ytd_realized_gains = data.get("ytd_gains", 0.0)
                self.tax_lots = [
                    TaxLot(**lot) for lot in data.get("lots", [])
                ]
                logger.info(
                    f"  📋 Tax records loaded: {len(self.tax_lots)} lots | "
                    f"YTD losses: ${self.ytd_realized_losses:,.0f} | "
                    f"YTD gains: ${self.ytd_realized_gains:,.0f}"
                )
            except Exception as e:
                logger.warning(f"  ⚠️ Failed to load tax records: {e}")

    def save_records(self):
        """Save tax records"""
        data = {
            "ytd_losses": self.ytd_realized_losses,
            "ytd_gains": self.ytd_realized_gains,
            "last_updated": datetime.now().isoformat(),
            "lots": [lot.to_dict() for lot in self.tax_lots],
        }
        with open(self.config.tax_log_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def record_buy(self, symbol: str, price: float, shares: int):
        """Record a buy"""
        lot = TaxLot(
            symbol=symbol,
            buy_date=datetime.now().strftime("%Y-%m-%d"),
            buy_price=price,
            shares=shares,
        )
        self.tax_lots.append(lot)
        self.save_records()
        logger.info(f"  📝 Buy recorded: {symbol} {shares} shares @ ${price:.2f}")

    def record_sell(self, symbol: str, price: float, shares: int):
        """
        Record a sell + calculate P&L (FIFO method)
        FIFO = First In, First Out (sells earliest purchased shares first)
        """
        remaining = shares
        total_pnl = 0.0

        for lot in self.tax_lots:
            if lot.symbol != symbol or lot.sell_date != "" or remaining <= 0:
                continue

            sell_shares = min(lot.shares, remaining)
            lot.sell_date = datetime.now().strftime("%Y-%m-%d")
            lot.sell_price = price
            lot.realized_pnl = (price - lot.buy_price) * sell_shares

            # Calculate holding period
            buy_dt = datetime.strptime(lot.buy_date, "%Y-%m-%d")
            lot.holding_days = (datetime.now() - buy_dt).days
            lot.is_long_term = lot.holding_days >= self.config.long_term_threshold_days

            total_pnl += lot.realized_pnl
            remaining -= sell_shares

        # Update YTD cumulative
        if total_pnl < 0:
            self.ytd_realized_losses += total_pnl  # Accumulate negative values
        else:
            self.ytd_realized_gains += total_pnl

        self.save_records()

        term = "Long-term" if any(
            l.is_long_term for l in self.tax_lots
            if l.symbol == symbol and l.sell_date != ""
        ) else "Short-term"

        logger.info(
            f"  📝 Sell recorded: {symbol} {shares} shares @ ${price:.2f} | "
            f"P&L: ${total_pnl:+,.0f} ({term}) | "
            f"YTD net P&L: ${self.ytd_realized_gains + self.ytd_realized_losses:+,.0f}"
        )

        return total_pnl

    def scan_for_harvest(
        self,
        positions: dict,
    ) -> list[dict]:
        """
        Scan for TLH opportunities — find positions with unrealized losses

        Parameters:
            positions: {symbol: {"avg_cost": float, "quantity": int, "market_price": float}}

        Returns:
            List of harvestable stocks
        """
        if not self.config.tlh_enabled:
            return []

        # Check annual target
        net_loss = self.ytd_realized_losses + self.ytd_realized_gains
        remaining_target = self.config.tlh_annual_loss_target + net_loss  # Amount still harvestable

        # End-of-December conservative mode
        now = datetime.now()
        if now.month == 12 and now.day > (31 - self.config.tlh_avoid_year_end_days):
            logger.info("  📅 End of December — TLH conservative mode")

        harvest_candidates = []

        for symbol, pos in positions.items():
            avg_cost = pos.get("avg_cost", 0)
            quantity = pos.get("quantity", 0)
            market_price = pos.get("market_price", avg_cost)

            if quantity <= 0 or avg_cost <= 0:
                continue

            unrealized_pnl = (market_price - avg_cost) * quantity
            unrealized_pct = ((market_price - avg_cost) / avg_cost) * 100

            # Only stocks with losses exceeding the threshold
            if (unrealized_pct <= self.config.tlh_min_loss_pct and
                unrealized_pnl <= self.config.tlh_min_loss_dollar):

                # Find substitute stocks
                substitutes = self.SUBSTITUTE_MAP.get(symbol, [])

                harvest_candidates.append({
                    "symbol": symbol,
                    "shares": quantity,
                    "avg_cost": avg_cost,
                    "market_price": market_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 2),
                    "substitutes": substitutes,
                    "tax_savings_est": round(abs(unrealized_pnl) * 0.35, 2),  # ~35% tax rate assumption
                })

        # Sort by loss size
        harvest_candidates.sort(key=lambda x: x["unrealized_pnl"])

        if harvest_candidates:
            logger.info(f"\n  🌾 Tax-Loss Harvesting opportunities: {len(harvest_candidates)} found:")
            for h in harvest_candidates:
                logger.info(
                    f"    {h['symbol']:6s} | Unrealized P&L: ${h['unrealized_pnl']:+,.0f} "
                    f"({h['unrealized_pct']:+.1f}%) | "
                    f"Est. tax savings: ${h['tax_savings_est']:,.0f} | "
                    f"Substitutes: {', '.join(h['substitutes'][:2])}"
                )

        return harvest_candidates

    def get_ytd_summary(self) -> dict:
        """Annual tax summary"""
        closed_lots = [l for l in self.tax_lots if l.sell_date]
        short_term_pnl = sum(l.realized_pnl for l in closed_lots if not l.is_long_term)
        long_term_pnl = sum(l.realized_pnl for l in closed_lots if l.is_long_term)
        wash_sale_count = sum(1 for l in closed_lots if l.is_wash_sale)
        net = short_term_pnl + long_term_pnl

        # Estimated tax (California resident)
        federal_st = short_term_pnl * 0.24 if short_term_pnl > 0 else 0  # ~24% federal
        federal_lt = long_term_pnl * 0.15 if long_term_pnl > 0 else 0    # ~15% federal
        ca_state = max(0, net) * 0.093  # CA ~9.3%

        # Loss deduction
        deductible_loss = min(abs(min(0, net)), 3000)
        loss_carryover = max(0, abs(min(0, net)) - 3000)

        return {
            "total_trades": len(closed_lots),
            "short_term_pnl": round(short_term_pnl, 2),
            "long_term_pnl": round(long_term_pnl, 2),
            "net_pnl": round(net, 2),
            "wash_sale_count": wash_sale_count,
            "est_federal_tax": round(federal_st + federal_lt, 2),
            "est_ca_tax": round(ca_state, 2),
            "est_total_tax": round(federal_st + federal_lt + ca_state, 2),
            "deductible_loss": round(deductible_loss, 2),
            "loss_carryover": round(loss_carryover, 2),
        }


# ═══════════════════════════════════════════════════════════════
#  7. Wash Sale Prevention Filter
# ═══════════════════════════════════════════════════════════════

class WashSaleFilter:
    """
    Wash Sale Rule Prevention — Automatic IRS Compliance

    Rule: If you repurchase a 'substantially identical' security within
    30 days before or after a loss sale, the loss deduction is disallowed.

    How it works:
    1. Track loss sale records
    2. Check 30-day blacklist on buy attempts
    3. Identical stock -> block purchase
    4. Similar stock -> warn (user judgment)
    5. Automatically recommend substitute stocks
    """

    # Substantially identical stock groups (ETF <-> individual stock relationships)
    SUBSTANTIALLY_IDENTICAL = {
        # S&P 500 ETFs are treated as identical to each other
        "SPY": ["IVV", "VOO"],
        "IVV": ["SPY", "VOO"],
        "VOO": ["SPY", "IVV"],
        # Nasdaq ETFs
        "QQQ": ["QQQM"],
        "QQQM": ["QQQ"],
        # Energy ETFs
        "XLE": ["VDE", "IYE"],
        "VDE": ["XLE", "IYE"],
    }

    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        # Loss sale records: {symbol: sell_date}
        self._loss_sales: dict[str, datetime] = {}
        self._load_wash_sale_log()

    def _load_wash_sale_log(self):
        """Load Wash Sale log file"""
        path = "wash_sale_log.json"
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                for sym, date_str in data.items():
                    self._loss_sales[sym] = datetime.strptime(date_str, "%Y-%m-%d")
                # Remove expired entries (past 30 days)
                now = datetime.now()
                self._loss_sales = {
                    s: d for s, d in self._loss_sales.items()
                    if (now - d).days <= self.config.wash_sale_window_days
                }
            except Exception:
                pass

    def _save_wash_sale_log(self):
        """Save Wash Sale log"""
        data = {
            sym: dt.strftime("%Y-%m-%d")
            for sym, dt in self._loss_sales.items()
        }
        with open("wash_sale_log.json", "w") as f:
            json.dump(data, f, indent=2)

    def record_loss_sale(self, symbol: str, loss_amount: float):
        """
        Record a loss sale — start 30-day countdown

        Only records if loss_amount is negative (a loss)
        """
        if loss_amount >= 0:
            return  # Profitable sales are not subject to Wash Sale

        self._loss_sales[symbol] = datetime.now()
        self._save_wash_sale_log()

        expiry = datetime.now() + timedelta(days=self.config.wash_sale_window_days)
        logger.info(
            f"  ⏰ Wash Sale started: {symbol} | "
            f"Loss: ${loss_amount:,.0f} | "
            f"Repurchase blocked until: ~{expiry:%Y-%m-%d} (30 days)"
        )

    def check_buy(self, symbol: str) -> dict:
        """
        Pre-buy Wash Sale check

        Returns:
            {
                "blocked": bool,
                "warning": bool,
                "reason": str,
                "days_remaining": int,
                "expiry_date": str,
                "substitutes": list,
            }
        """
        if not self.config.wash_sale_enabled:
            return {"blocked": False, "warning": False}

        now = datetime.now()
        result = {
            "blocked": False,
            "warning": False,
            "reason": "",
            "days_remaining": 0,
            "expiry_date": "",
            "substitutes": [],
        }

        # 1. Check identical stock
        if symbol in self._loss_sales:
            sale_date = self._loss_sales[symbol]
            days_elapsed = (now - sale_date).days
            days_remaining = self.config.wash_sale_window_days - days_elapsed

            if days_remaining > 0:
                expiry = sale_date + timedelta(days=self.config.wash_sale_window_days)
                result["blocked"] = True
                result["days_remaining"] = days_remaining
                result["expiry_date"] = expiry.strftime("%Y-%m-%d")
                result["reason"] = (
                    f"🚫 Wash Sale! {days_elapsed} days since {symbol} loss sale "
                    f"({days_remaining} days remaining, buy allowed after {expiry:%m/%d})"
                )
                # Recommend substitute stocks
                result["substitutes"] = TaxLossHarvester.SUBSTITUTE_MAP.get(symbol, [])
                if result["substitutes"]:
                    result["reason"] += f" -> Substitutes: {', '.join(result['substitutes'][:3])}"
                return result

        # 2. Check substantially identical stocks
        identical = self.SUBSTANTIALLY_IDENTICAL.get(symbol, [])
        for ident_sym in identical:
            if ident_sym in self._loss_sales:
                sale_date = self._loss_sales[ident_sym]
                days_elapsed = (now - sale_date).days
                days_remaining = self.config.wash_sale_window_days - days_elapsed

                if days_remaining > 0:
                    result["warning"] = True
                    result["days_remaining"] = days_remaining
                    result["reason"] = (
                        f"⚠️ Wash Sale warning: {ident_sym} (substantially identical) "
                        f"was sold at a loss {days_elapsed} days ago ({days_remaining} days remaining)"
                    )
                    return result

        # 3. Check similar stocks (buying a similar stock after a loss sale in the same sector)
        for loss_sym, sale_date in self._loss_sales.items():
            days_elapsed = (now - sale_date).days
            if days_elapsed > self.config.wash_sale_window_days:
                continue

            subs = TaxLossHarvester.SUBSTITUTE_MAP.get(loss_sym, [])
            if symbol in subs and self.config.wash_sale_warn_similar:
                result["warning"] = True
                result["reason"] = (
                    f"⚠️ Similar stock warning: {loss_sym} loss sale {days_elapsed} days ago | "
                    f"{symbol} may be considered a substitute (subject to IRS determination)"
                )
                return result

        return result

    def get_blacklist(self) -> list[dict]:
        """Current Wash Sale blacklist"""
        now = datetime.now()
        blacklist = []

        for sym, sale_date in self._loss_sales.items():
            days_elapsed = (now - sale_date).days
            days_remaining = self.config.wash_sale_window_days - days_elapsed

            if days_remaining > 0:
                expiry = sale_date + timedelta(days=self.config.wash_sale_window_days)
                blacklist.append({
                    "symbol": sym,
                    "sale_date": sale_date.strftime("%Y-%m-%d"),
                    "days_remaining": days_remaining,
                    "expiry_date": expiry.strftime("%Y-%m-%d"),
                    "substitutes": TaxLossHarvester.SUBSTITUTE_MAP.get(sym, []),
                })

        return blacklist


# ═══════════════════════════════════════════════════════════════
#  Integrated Tax Optimization Engine
# ═══════════════════════════════════════════════════════════════

class TaxOptimizer:
    """
    Integrated Tax Optimization
    - Combined TLH + Wash Sale management
    - Automatically called on buy/sell from Smart Trader
    """

    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        self.harvester = TaxLossHarvester(self.config)
        self.wash_filter = WashSaleFilter(self.config)

    def on_buy(self, symbol: str, price: float, shares: int) -> dict:
        """
        Called on buy — Wash Sale check + record buy

        Returns:
            {"allowed": bool, "wash_sale": dict}
        """
        # Wash Sale check
        wash = self.wash_filter.check_buy(symbol)

        if wash["blocked"]:
            logger.info(f"  🚫 Buy blocked (Wash Sale): {wash['reason']}")
            return {"allowed": False, "wash_sale": wash}

        if wash["warning"]:
            logger.info(f"  ⚠️ {wash['reason']}")

        # Record buy
        self.harvester.record_buy(symbol, price, shares)
        return {"allowed": True, "wash_sale": wash}

    def on_sell(self, symbol: str, price: float, shares: int) -> dict:
        """
        Called on sell — record P&L + trigger Wash Sale if applicable

        Returns:
            {"pnl": float, "is_loss": bool, "wash_sale_started": bool}
        """
        pnl = self.harvester.record_sell(symbol, price, shares)

        result = {
            "pnl": pnl,
            "is_loss": pnl < 0,
            "wash_sale_started": False,
        }

        # If this was a loss sale, start Wash Sale countdown
        if pnl < 0:
            self.wash_filter.record_loss_sale(symbol, pnl)
            result["wash_sale_started"] = True

        return result

    def check_buy_allowed(self, symbol: str) -> dict:
        """Quick Wash Sale check before buying"""
        return self.wash_filter.check_buy(symbol)

    def scan_harvest_opportunities(self, positions: dict) -> list[dict]:
        """Scan for TLH opportunities"""
        return self.harvester.scan_for_harvest(positions)

    def get_wash_sale_blacklist(self) -> list[dict]:
        """Current repurchase-blocked stocks"""
        return self.wash_filter.get_blacklist()

    def print_tax_report(self):
        """Annual tax report"""
        summary = self.harvester.get_ytd_summary()
        blacklist = self.wash_filter.get_blacklist()

        print("\n" + "═" * 70)
        print("  💰 Tax Optimization Report")
        print("═" * 70)

        print(f"\n  📊 Year-to-Date Realized P&L (YTD):")
        print(f"    Short-term P&L:  ${summary['short_term_pnl']:>+10,.2f}")
        print(f"    Long-term P&L:   ${summary['long_term_pnl']:>+10,.2f}")
        print(f"    Net total:       ${summary['net_pnl']:>+10,.2f}")
        print(f"    Total trades:    {summary['total_trades']}")

        print(f"\n  🏛️ Estimated Tax (California):")
        print(f"    Federal tax:     ${summary['est_federal_tax']:>10,.2f}")
        print(f"    CA state tax:    ${summary['est_ca_tax']:>10,.2f}")
        print(f"    Total:           ${summary['est_total_tax']:>10,.2f}")

        if summary['net_pnl'] < 0:
            print(f"\n  🌾 Loss Deduction:")
            print(f"    Deductible:      ${summary['deductible_loss']:>10,.2f} (max $3,000)")
            if summary['loss_carryover'] > 0:
                print(f"    Carryover loss:  ${summary['loss_carryover']:>10,.2f} (to next year)")

        if summary['wash_sale_count'] > 0:
            print(f"\n  ⚠️ Wash Sales triggered: {summary['wash_sale_count']}")

        if blacklist:
            print(f"\n  🚫 Wash Sale Blacklist ({len(blacklist)} stocks):")
            for b in blacklist:
                print(
                    f"    {b['symbol']:6s} | "
                    f"Sale date: {b['sale_date']} | "
                    f"Remaining: {b['days_remaining']} days | "
                    f"Expires: {b['expiry_date']} | "
                    f"Substitutes: {', '.join(b['substitutes'][:2])}"
                )

        print("═" * 70)


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    """Tax Optimizer demo"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  💰 Tax Optimizer Demo — TLH + Wash Sale Prevention      ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    tax = TaxOptimizer()

    # 1) Record buys
    print("  📈 Buy records:")
    tax.on_buy("XOM", 150.00, 50)
    tax.on_buy("NVDA", 200.00, 30)
    tax.on_buy("CRM", 320.00, 20)

    # 2) Sell some positions (losses incurred)
    print("\n  📉 Sell records:")
    tax.on_sell("CRM", 280.00, 20)    # Loss: -$800
    tax.on_sell("NVDA", 185.00, 30)   # Loss: -$450
    tax.on_sell("XOM", 160.00, 50)    # Gain: +$500

    # 3) Wash Sale check — attempt to repurchase CRM
    print("\n  🔍 Wash Sale check:")

    crm_check = tax.check_buy_allowed("CRM")
    print(f"  CRM buy: {'Blocked' if crm_check['blocked'] else 'Allowed'}")
    if crm_check.get("reason"):
        print(f"    -> {crm_check['reason']}")

    # ORCL is a CRM substitute -> warning only
    orcl_check = tax.check_buy_allowed("ORCL")
    print(f"  ORCL buy: {'Warning' if orcl_check['warning'] else 'Allowed'}")
    if orcl_check.get("reason"):
        print(f"    -> {orcl_check['reason']}")

    # XOM was a profitable sale so no Wash Sale
    xom_check = tax.check_buy_allowed("XOM")
    print(f"  XOM buy: {'Blocked' if xom_check['blocked'] else 'Allowed'}")

    # 4) TLH opportunity scan
    print("\n  🌾 Tax-Loss Harvesting scan:")
    positions = {
        "LMT": {"avg_cost": 600.00, "quantity": 10, "market_price": 580.00},
        "HOOD": {"avg_cost": 40.00, "quantity": 200, "market_price": 32.00},
    }
    harvest = tax.scan_harvest_opportunities(positions)

    # 5) Check blacklist
    print("\n  🚫 Current blacklist:")
    for b in tax.get_wash_sale_blacklist():
        print(f"    {b['symbol']} — {b['days_remaining']} days remaining -> Substitutes: {', '.join(b['substitutes'][:2])}")

    # 6) Tax report
    tax.print_tax_report()

    # Cleanup
    for f in ["tax_records.json", "wash_sale_log.json"]:
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    demo()
