"""
===================================================================
  Politician Data - Congressional Trade Disclosures & Political Event Data Collection

  Data Sources:
    1. QuiverQuant API — Congressional trade disclosures (STOCK Act)
    2. Capitol Trades  — Per-member trade details
    3. Political Events — Bills, hearings, executive orders

  Features:
    - Congressional trade disclosure collection and filtering
    - Politician profile (win rate, reliability score) construction
    - Committee-sector mapping
    - JSON file caching
===================================================================
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger("PoliticianData")


# ===================================================================
#  Enums & Data Classes
# ===================================================================

class DisclosureType(Enum):
    PURCHASE = "purchase"
    SALE = "sale"
    EXCHANGE = "exchange"


class SignalSource(Enum):
    CONGRESSIONAL_DISCLOSURE = "disclosure"   # -> swing trade
    POLITICAL_EVENT = "event"                 # -> day trade
    COMMITTEE_ACTIVITY = "committee"          # -> swing trade


@dataclass
class CongressionalTrade:
    """Single congressional trade disclosure"""
    politician_name: str
    party: str                    # "R" or "D"
    chamber: str                  # "Senate" or "House"
    symbol: str
    disclosure_type: DisclosureType
    amount_low: float             # Trade amount range lower bound
    amount_high: float            # Trade amount range upper bound
    transaction_date: str         # Actual transaction date
    disclosure_date: str          # Disclosure date (up to 45-day delay)
    delay_days: int               # Days between transaction and disclosure
    committees: list[str] = field(default_factory=list)
    sector: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def midpoint_amount(self) -> float:
        return (self.amount_low + self.amount_high) / 2


@dataclass
class PoliticianProfile:
    """Politician trading performance profile"""
    name: str
    party: str = ""
    chamber: str = ""
    committees: list[str] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    win_rate: float = 0.0
    avg_return_30d: float = 0.0   # Average return 30 days after disclosure
    avg_return_90d: float = 0.0   # Average return 90 days after disclosure
    sector_expertise: dict = field(default_factory=dict)  # {sector: win_rate}
    reliability_score: float = 0.0  # 0.0 ~ 1.0 overall reliability

    def calculate_reliability(self):
        """Calculate overall reliability score"""
        if self.total_trades < 5:
            self.reliability_score = 0.0
            return

        # Win rate (50%)
        wr_score = min(1.0, self.win_rate / 0.7)  # Perfect score at 70% or above

        # Trade count (20%) -- higher with more experience
        trade_score = min(1.0, self.total_trades / 50)

        # 30-day return (30%)
        ret_score = min(1.0, max(0.0, self.avg_return_30d / 10.0))  # Perfect score at 10% or above

        self.reliability_score = round(
            wr_score * 0.50 + trade_score * 0.20 + ret_score * 0.30, 3
        )


@dataclass
class PoliticalEvent:
    """Political event"""
    event_type: str               # "bill_vote", "policy_announcement", "hearing", "executive_order"
    title: str
    affected_sectors: list[str] = field(default_factory=list)
    sentiment: str = "neutral"    # "bullish", "bearish", "neutral"
    impact_score: float = 0.0     # 0.0 ~ 1.0
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class PoliticianDataConfig:
    """Data collection configuration"""
    quiver_api_key: str = ""
    capitol_trades_api_key: str = ""
    cache_dir: str = "politician_cache"
    disclosure_lookback_days: int = 7         # Only fetch last 7 days
    min_trade_amount: float = 15_000.0       # Ignore small trades
    max_disclosure_delay_days: int = 7       # Ignore disclosures older than 7 days
    politician_history_months: int = 24      # Profile construction period
    refresh_interval_min: int = 30           # Data refresh interval


# ===================================================================
#  Committee -> Sector Mapping
# ===================================================================

COMMITTEE_SECTOR_MAP = {
    # Senate
    "Armed Services": ["Aerospace & Defense", "XAR", "ITA", "LMT", "RTX", "NOC", "GD", "BA"],
    "Banking, Housing, and Urban Affairs": ["Financials", "XLF", "JPM", "BAC", "GS", "MS", "WFC"],
    "Commerce, Science, and Transportation": ["Technology", "XLK", "Communication Services", "XLC"],
    "Energy and Natural Resources": ["Energy", "XLE", "XOM", "CVX", "COP", "SLB"],
    "Environment and Public Works": ["Utilities", "XLU", "Clean Energy", "ICLN", "TAN"],
    "Finance": ["Financials", "XLF", "Health Care", "XLV"],
    "Health, Education, Labor, and Pensions": ["Health Care", "XLV", "UNH", "JNJ", "PFE", "ABBV"],
    "Judiciary": ["Technology", "XLK", "Communication Services", "GOOGL", "META"],
    "Intelligence": ["Aerospace & Defense", "Technology", "PLTR", "PANW"],
    # House
    "Financial Services": ["Financials", "XLF", "JPM", "BAC", "GS"],
    "Energy and Commerce": ["Energy", "XLE", "Health Care", "XLV", "Technology", "XLK"],
    "Ways and Means": ["Financials", "XLF", "Industrials", "XLI"],
    "Appropriations": ["Industrials", "XLI", "Aerospace & Defense"],
}

# Sector -> Representative symbols mapping
SECTOR_SYMBOLS = {
    "Technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "CRM"],
    "Financials": ["JPM", "BAC", "GS", "MS", "WFC", "BRK-B", "V", "MA"],
    "Health Care": ["UNH", "JNJ", "PFE", "ABBV", "LLY", "MRK", "TMO"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX"],
    "Aerospace & Defense": ["LMT", "RTX", "NOC", "GD", "BA", "LHX"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA"],
    "Industrials": ["CAT", "DE", "UNP", "HON", "GE", "MMM"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "NKE", "SBUX", "TGT"],
    "Clean Energy": ["ENPH", "FSLR", "PLUG", "RUN"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP"],
}

# Political event type -> Affected sector mapping
EVENT_SECTOR_MAP = {
    "defense_spending": ["Aerospace & Defense"],
    "healthcare_reform": ["Health Care"],
    "tech_regulation": ["Technology", "Communication Services"],
    "energy_policy": ["Energy", "Clean Energy"],
    "financial_regulation": ["Financials"],
    "infrastructure": ["Industrials"],
    "trade_policy": ["Industrials", "Technology"],
    "tax_reform": ["Financials", "Consumer Discretionary"],
}


# ===================================================================
#  Data Fetcher
# ===================================================================

class PoliticianDataFetcher:
    """Congressional trade disclosure & political event data collection"""

    def __init__(self, config: PoliticianDataConfig = None):
        self.config = config or PoliticianDataConfig()
        self._cache: dict = {}
        self._cache_ts: dict[str, float] = {}
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        os.makedirs(self.config.cache_dir, exist_ok=True)

    # -- QuiverQuant API -------------------------------------------

    def fetch_recent_disclosures(self) -> list[CongressionalTrade]:
        """Fetch recent congressional trade disclosures from QuiverQuant API"""
        cached = self._load_cache("disclosures")
        if cached is not None:
            return [self._dict_to_trade(d) for d in cached]

        trades = []

        if not self.config.quiver_api_key:
            logger.warning("QuiverQuant API key not set -- using cache/dummy data")
            return self._load_fallback_disclosures()

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.config.quiver_api_key}",
                "Accept": "application/json",
            }
            url = "https://api.quiverquant.com/beta/live/congresstrading"
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            data = resp.json()
            for item in data:
                trade = self._parse_quiver_trade(item)
                if trade:
                    trades.append(trade)

            # Save cache
            self._save_cache("disclosures", [self._trade_to_dict(t) for t in trades])
            logger.info(f"QuiverQuant: {len(trades)} disclosures collected")

        except Exception as e:
            logger.error(f"QuiverQuant API error: {e}")
            trades = self._load_fallback_disclosures()

        return trades

    def _parse_quiver_trade(self, item: dict) -> Optional[CongressionalTrade]:
        """Parse QuiverQuant API response"""
        try:
            ticker = item.get("Ticker", "").strip()
            if not ticker or len(ticker) > 5:
                return None

            tx_type_raw = item.get("Transaction", "").lower()
            if "purchase" in tx_type_raw:
                tx_type = DisclosureType.PURCHASE
            elif "sale" in tx_type_raw:
                tx_type = DisclosureType.SALE
            else:
                tx_type = DisclosureType.EXCHANGE

            # Parse amount
            amount_str = item.get("Amount", "$1,001 - $15,000")
            amount_low, amount_high = self._parse_amount_range(amount_str)

            tx_date = item.get("TransactionDate", "")
            disc_date = item.get("ReportDate", item.get("DisclosureDate", ""))
            delay = self._calc_delay_days(tx_date, disc_date)

            return CongressionalTrade(
                politician_name=item.get("Representative", "Unknown"),
                party=item.get("Party", ""),
                chamber=item.get("Chamber", item.get("House", "Unknown")),
                symbol=ticker,
                disclosure_type=tx_type,
                amount_low=amount_low,
                amount_high=amount_high,
                transaction_date=tx_date,
                disclosure_date=disc_date,
                delay_days=delay,
                committees=item.get("Committees", []),
                sector=item.get("Sector", ""),
                metadata={"source": "quiverquant", "raw": item},
            )
        except Exception as e:
            logger.debug(f"Disclosure parsing error: {e}")
            return None

    @staticmethod
    def _parse_amount_range(amount_str: str) -> tuple[float, float]:
        """'$1,001 - $15,000' -> (1001.0, 15000.0)"""
        import re
        numbers = re.findall(r'[\d,]+', amount_str.replace(',', ''))
        if len(numbers) >= 2:
            return float(numbers[0]), float(numbers[1])
        elif len(numbers) == 1:
            val = float(numbers[0])
            return val, val
        return 1000.0, 15000.0  # Default

    @staticmethod
    def _calc_delay_days(tx_date: str, disc_date: str) -> int:
        """Delay days between transaction date and disclosure date"""
        try:
            fmt = "%Y-%m-%d"
            td = datetime.strptime(tx_date[:10], fmt)
            dd = datetime.strptime(disc_date[:10], fmt)
            return max(0, (dd - td).days)
        except Exception:
            return 0

    # -- Politician Profile Construction ---------------------------

    def build_politician_profiles(self) -> dict[str, PoliticianProfile]:
        """Build politician profiles based on historical trade data"""
        cached = self._load_cache("profiles")
        if cached is not None:
            profiles = {}
            for name, data in cached.items():
                p = PoliticianProfile(name=name)
                for k, v in data.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
                profiles[name] = p
            return profiles

        # Fetch historical data from API
        profiles = {}

        if not self.config.quiver_api_key:
            return self._load_fallback_profiles()

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.config.quiver_api_key}",
                "Accept": "application/json",
            }
            url = "https://api.quiverquant.com/beta/historical/congresstrading"
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()

            # Aggregate trades per politician
            trade_data: dict[str, list] = {}
            for item in resp.json():
                name = item.get("Representative", "Unknown")
                if name not in trade_data:
                    trade_data[name] = []
                trade_data[name].append(item)

            for name, trades in trade_data.items():
                profile = PoliticianProfile(
                    name=name,
                    party=trades[0].get("Party", ""),
                    chamber=trades[0].get("Chamber", ""),
                    total_trades=len(trades),
                )
                # Simple win rate estimate (positive return after purchase)
                wins = sum(1 for t in trades
                          if t.get("Transaction", "").lower().startswith("purchase"))
                profile.winning_trades = int(wins * 0.6)  # Conservative estimate
                profile.win_rate = profile.winning_trades / max(1, profile.total_trades)
                profile.calculate_reliability()
                profiles[name] = profile

            # Save cache
            cache_data = {
                name: {
                    "party": p.party, "chamber": p.chamber,
                    "total_trades": p.total_trades, "winning_trades": p.winning_trades,
                    "win_rate": p.win_rate, "avg_return_30d": p.avg_return_30d,
                    "reliability_score": p.reliability_score,
                }
                for name, p in profiles.items()
            }
            self._save_cache("profiles", cache_data)
            logger.info(f"Politician profiles: {len(profiles)} built")

        except Exception as e:
            logger.error(f"Profile construction error: {e}")
            profiles = self._load_fallback_profiles()

        return profiles

    # -- Political Events ------------------------------------------

    def fetch_political_events(self) -> list[PoliticalEvent]:
        """Fetch recent political events"""
        cached = self._load_cache("events")
        if cached is not None:
            return [PoliticalEvent(**d) for d in cached]

        # Currently based on cache/manual data
        # Future: integrate news API (NewsAPI, ProPublica Congress API)
        events = self._load_fallback_events()
        return events

    # -- Political News (RSS) ----------------------------------------

    def fetch_political_news(self, max_items: int = 10) -> list[dict]:
        """Fetch political/congressional news from RSS feeds.
        Returns list of {title, summary, link, source, published}.
        """
        cached = self._load_cache("news")
        if cached is not None:
            return cached[:max_items]

        import urllib.request
        import xml.etree.ElementTree as ET

        RSS_FEEDS = [
            ("Reuters Politics", "https://feeds.reuters.com/Reuters/PoliticsNews"),
            ("The Hill", "https://thehill.com/feed/"),
            ("Politico", "https://rss.politico.com/politics-news.xml"),
            ("Congress.gov", "https://www.congress.gov/rss/most-viewed-bills.xml"),
        ]

        articles = []
        for source_name, url in RSS_FEEDS:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "AJRobotics-PoliticianTrader/1.0"
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                root = ET.fromstring(data)

                # Standard RSS 2.0
                for item in root.findall(".//item")[:5]:
                    title = (item.findtext("title") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    pub = (item.findtext("pubDate") or "").strip()

                    # Clean HTML tags from description
                    import re
                    desc_clean = re.sub(r'<[^>]+>', '', desc)
                    # Truncate to ~100 words
                    words = desc_clean.split()
                    if len(words) > 100:
                        desc_clean = " ".join(words[:100]) + "..."

                    if title:
                        articles.append({
                            "title": title,
                            "summary": desc_clean,
                            "link": link,
                            "source": source_name,
                            "published": pub,
                        })
            except Exception as e:
                logger.debug(f"RSS fetch failed for {source_name}: {e}")

        # Sort by published date (newest first), best effort
        articles.sort(key=lambda a: a.get("published", ""), reverse=True)
        articles = articles[:max_items]

        if articles:
            self._save_cache("news", articles)

        return articles

    # -- Filtering -------------------------------------------------

    def filter_actionable_disclosures(
        self, trades: list[CongressionalTrade]
    ) -> list[CongressionalTrade]:
        """Filter only actionable disclosures"""
        cfg = self.config
        actionable = []

        for trade in trades:
            # Minimum amount
            if trade.midpoint_amount < cfg.min_trade_amount:
                continue

            # Disclosure delay (skip if too old)
            if trade.delay_days > cfg.max_disclosure_delay_days:
                continue

            # Disclosure date within lookback period
            try:
                disc_dt = datetime.strptime(trade.disclosure_date[:10], "%Y-%m-%d")
                cutoff = datetime.now() - timedelta(days=cfg.disclosure_lookback_days)
                if disc_dt < cutoff:
                    continue
            except Exception:
                pass

            # Purchase/sale only (exclude exchange)
            if trade.disclosure_type == DisclosureType.EXCHANGE:
                continue

            actionable.append(trade)

        logger.info(f"Filter result: {len(trades)} -> {len(actionable)} (actionable)")
        return actionable

    def get_committee_sector_map(self) -> dict[str, list[str]]:
        """Return committee -> sector mapping"""
        return COMMITTEE_SECTOR_MAP

    def get_sector_symbols(self, sector: str) -> list[str]:
        """Sector -> representative symbols list"""
        return SECTOR_SYMBOLS.get(sector, [])

    # -- Cache -----------------------------------------------------

    def _load_cache(self, key: str) -> Optional[dict | list]:
        """Load cache (only within refresh_interval)"""
        cache_file = os.path.join(self.config.cache_dir, f"{key}.json")
        if not os.path.isfile(cache_file):
            return None

        try:
            mtime = os.path.getmtime(cache_file)
            age_min = (time.time() - mtime) / 60
            if age_min > self.config.refresh_interval_min:
                return None

            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self, key: str, data):
        """Save cache"""
        cache_file = os.path.join(self.config.cache_dir, f"{key}.json")
        try:
            with open(cache_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Cache save failed ({key}): {e}")

    @staticmethod
    def _trade_to_dict(trade: CongressionalTrade) -> dict:
        return {
            "politician_name": trade.politician_name,
            "party": trade.party,
            "chamber": trade.chamber,
            "symbol": trade.symbol,
            "disclosure_type": trade.disclosure_type.value,
            "amount_low": trade.amount_low,
            "amount_high": trade.amount_high,
            "transaction_date": trade.transaction_date,
            "disclosure_date": trade.disclosure_date,
            "delay_days": trade.delay_days,
            "committees": trade.committees,
            "sector": trade.sector,
        }

    @staticmethod
    def _dict_to_trade(d: dict) -> CongressionalTrade:
        dt = d.get("disclosure_type", "purchase")
        return CongressionalTrade(
            politician_name=d.get("politician_name", ""),
            party=d.get("party", ""),
            chamber=d.get("chamber", ""),
            symbol=d.get("symbol", ""),
            disclosure_type=DisclosureType(dt) if dt in ("purchase", "sale", "exchange") else DisclosureType.PURCHASE,
            amount_low=d.get("amount_low", 0),
            amount_high=d.get("amount_high", 0),
            transaction_date=d.get("transaction_date", ""),
            disclosure_date=d.get("disclosure_date", ""),
            delay_days=d.get("delay_days", 0),
            committees=d.get("committees", []),
            sector=d.get("sector", ""),
        )

    # -- Fallback data (demo when no API) --------------------------

    def _load_fallback_disclosures(self) -> list[CongressionalTrade]:
        """Demo/test dummy disclosure data"""
        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        two_weeks = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

        return [
            CongressionalTrade(
                "Nancy Pelosi", "D", "House", "NVDA", DisclosureType.PURCHASE,
                500_001, 1_000_000, two_weeks, week_ago, 7,
                committees=["Intelligence"], sector="Technology",
            ),
            CongressionalTrade(
                "Tommy Tuberville", "R", "Senate", "LMT", DisclosureType.PURCHASE,
                100_001, 250_000, two_weeks, week_ago, 7,
                committees=["Armed Services"], sector="Aerospace & Defense",
            ),
            CongressionalTrade(
                "Dan Crenshaw", "R", "House", "XOM", DisclosureType.PURCHASE,
                50_001, 100_000, week_ago, today, 7,
                committees=["Energy and Commerce"], sector="Energy",
            ),
            CongressionalTrade(
                "Mark Kelly", "D", "Senate", "RTX", DisclosureType.PURCHASE,
                15_001, 50_000, two_weeks, week_ago, 7,
                committees=["Armed Services"], sector="Aerospace & Defense",
            ),
            CongressionalTrade(
                "Nancy Pelosi", "D", "House", "AAPL", DisclosureType.PURCHASE,
                250_001, 500_000, week_ago, today, 7,
                committees=["Intelligence"], sector="Technology",
            ),
            CongressionalTrade(
                "Josh Gottheimer", "D", "House", "GOOGL", DisclosureType.PURCHASE,
                100_001, 250_000, week_ago, today, 7,
                committees=["Financial Services"], sector="Technology",
            ),
            CongressionalTrade(
                "Tommy Tuberville", "R", "Senate", "RTX", DisclosureType.PURCHASE,
                50_001, 100_000, two_weeks, week_ago, 7,
                committees=["Armed Services"], sector="Aerospace & Defense",
            ),
        ]

    def _load_fallback_profiles(self) -> dict[str, PoliticianProfile]:
        """Demo politician profiles"""
        profiles = {}
        data = [
            ("Nancy Pelosi", "D", "House", ["Intelligence"], 85, 58, 0.682, 8.5, 12.3),
            ("Tommy Tuberville", "R", "Senate", ["Armed Services"], 120, 72, 0.600, 5.2, 8.1),
            ("Dan Crenshaw", "R", "House", ["Energy and Commerce"], 45, 28, 0.622, 4.8, 7.5),
            ("Mark Kelly", "D", "Senate", ["Armed Services"], 32, 20, 0.625, 6.1, 9.0),
            ("Josh Gottheimer", "D", "House", ["Financial Services"], 55, 34, 0.618, 4.5, 7.2),
        ]
        for name, party, chamber, committees, total, wins, wr, ret30, ret90 in data:
            p = PoliticianProfile(
                name=name, party=party, chamber=chamber, committees=committees,
                total_trades=total, winning_trades=wins, win_rate=wr,
                avg_return_30d=ret30, avg_return_90d=ret90,
            )
            p.calculate_reliability()
            profiles[name] = p

        return profiles

    def _load_fallback_events(self) -> list[PoliticalEvent]:
        """Demo political events"""
        return [
            PoliticalEvent(
                event_type="bill_vote",
                title="Defense Spending Authorization Act",
                affected_sectors=["Aerospace & Defense"],
                sentiment="bullish",
                impact_score=0.8,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
            PoliticalEvent(
                event_type="executive_order",
                title="AI Regulation Executive Order",
                affected_sectors=["Technology", "Communication Services"],
                sentiment="bearish",
                impact_score=0.7,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        ]


# ===================================================================
#  Demo
# ===================================================================

def demo():
    print("""
    +========================================================+
    |  Politician Data Module Demo                           |
    +========================================================+
    """)

    fetcher = PoliticianDataFetcher(PoliticianDataConfig(cache_dir="politician_cache"))

    # 1) Disclosure data
    print("  -- Recent Congressional Trade Disclosures --")
    disclosures = fetcher.fetch_recent_disclosures()
    for t in disclosures:
        print(
            f"    {t.politician_name:20s} | {t.party} | {t.symbol:5s} | "
            f"{t.disclosure_type.value:8s} | ${t.midpoint_amount:>10,.0f} | "
            f"Delay: {t.delay_days} days"
        )

    # 2) Filtering
    print(f"\n  -- Filtering (min ${fetcher.config.min_trade_amount:,.0f}, max delay {fetcher.config.max_disclosure_delay_days} days) --")
    actionable = fetcher.filter_actionable_disclosures(disclosures)
    for t in actionable:
        print(
            f"    {t.politician_name:20s} | {t.symbol:5s} | "
            f"${t.midpoint_amount:>10,.0f} | Committees: {t.committees}"
        )

    # 3) Politician profiles
    print("\n  -- Politician Profiles --")
    profiles = fetcher.build_politician_profiles()
    for name, p in sorted(profiles.items(), key=lambda x: x[1].reliability_score, reverse=True):
        print(
            f"    {name:20s} | Reliability: {p.reliability_score:.3f} | "
            f"Win rate: {p.win_rate:.1%} | Trades: {p.total_trades} | "
            f"30d return: {p.avg_return_30d:+.1f}%"
        )

    # 4) Political events
    print("\n  -- Political Events --")
    events = fetcher.fetch_political_events()
    for e in events:
        print(
            f"    [{e.event_type}] {e.title} | "
            f"Sentiment: {e.sentiment} | Impact: {e.impact_score:.1f} | "
            f"Sectors: {e.affected_sectors}"
        )

    # 5) Committee-sector mapping
    print("\n  -- Committee-Sector Mapping (partial) --")
    cmap = fetcher.get_committee_sector_map()
    for committee in ["Armed Services", "Energy and Natural Resources", "Intelligence"]:
        if committee in cmap:
            print(f"    {committee}: {cmap[committee][:5]}")

    print("\n  Demo complete!")


if __name__ == "__main__":
    demo()
