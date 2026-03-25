"""
═══════════════════════════════════════════════════════════════════
  Auto Stock Screener - Automatic Stock Screening & Daily Rebalancing Module

  Features:
    1. Scan the full universe after market close every day
    2. Select TOP 10 based on momentum + volume + technical indicators
    3. Detect sector rotation (energy, defense, AI, etc.)
    4. Integrate with Smart Trader for automated trading
    5. Generate daily reports & logging

  Usage:
    python auto_screener.py          # One-time screening
    python auto_screener.py --daemon  # Daily automatic execution
═══════════════════════════════════════════════════════════════════
"""

import logging
import json
import os
import sys
import time
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║  Required packages need to be installed:                 ║
    ║  pip install ib_insync pandas numpy                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  Screening Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class ScreenerConfig:
    """Screener configuration"""

    # ── IB Connection ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    client_id: int = 2          # Use a different ID from Smart Trader

    # ── Screening Universe ──
    # Screening pool based on large-cap + mid-cap + sector ETFs
    universe: list = field(default_factory=lambda: [
        # ── Mega Cap Tech / AI ──
        "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "TSM",
        "AVGO", "AMD", "INTC", "MU", "ORCL", "CRM", "PLTR", "APP",

        # ── Energy / Oil ──
        "XOM", "CVX", "COP", "OXY", "DVN", "EOG", "PXD", "HES",
        "SLB", "HAL", "BP", "SHEL", "MPC", "VLO", "PSX",

        # ── Defense / Aerospace ──
        "LMT", "NOC", "RTX", "GD", "BA", "LHX", "AVAV", "KTOS",

        # ── Oil Tankers / Shipping ──
        "FRO", "DHT", "INSW", "STNG", "TNK",

        # ── Nuclear / Energy Infrastructure ──
        "CEG", "VST", "GEV", "NRG", "NEE",

        # ── Financials ──
        "JPM", "GS", "MS", "BAC", "WFC", "BRK-B", "AXP",

        # ── Healthcare / Biotech ──
        "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "AMGN",

        # ── Consumer / Retail ──
        "WMT", "COST", "HD", "TGT", "LULU", "NKE",

        # ── Gold / Commodities ──
        "GLD", "NEM", "GOLD", "FNV",

        # ── Fintech / Crypto-adjacent ──
        "HOOD", "MSTR", "COIN", "SQ",
    ])

    # ── Screening Criteria ──
    top_picks: int = 10              # Number of final selected stocks
    min_avg_volume: int = 500000     # Minimum average volume
    lookback_days: str = "30 D"      # History period
    bar_size: str = "1 day"          # Bar size

    # ── Weights (total = 1.0) ──
    weight_momentum_5d: float = 0.25   # 5-day momentum
    weight_momentum_10d: float = 0.20  # 10-day momentum
    weight_volume_surge: float = 0.15  # Volume surge
    weight_ma_trend: float = 0.20      # MA trend (price > MA)
    weight_volatility: float = 0.10    # Volatility (prefer moderate level)
    weight_rsi_zone: float = 0.10      # RSI zone score

    # ── Investment Settings ──
    investment_per_stock: float = 10000.0  # Investment per stock
    max_total_investment: float = 100000.0 # Maximum total investment

    # ── Schedule ──
    run_time_hour: int = 16        # Execution time (16 = 4 PM, after market close)
    run_time_minute: int = 30

    # ── Files ──
    picks_file: str = "daily_picks.json"
    history_file: str = "screening_history.json"
    report_dir: str = "reports"
    log_file: str = "screener.log"

    def save(self, path="screener_config.json"):
        data = asdict(self)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path="screener_config.json"):
        if not os.path.exists(path):
            return cls()
        with open(path, "r") as f:
            return cls(**json.load(f))


# ═══════════════════════════════════════════════════════════════
#  Stock Score Calculation
# ═══════════════════════════════════════════════════════════════

@dataclass
class StockScore:
    """Individual stock analysis result"""
    symbol: str
    name: str = ""
    sector: str = ""

    # Price info
    current_price: float = 0.0
    price_5d_ago: float = 0.0
    price_10d_ago: float = 0.0

    # Individual scores (0~100)
    momentum_5d_score: float = 0.0
    momentum_10d_score: float = 0.0
    volume_surge_score: float = 0.0
    ma_trend_score: float = 0.0
    volatility_score: float = 0.0
    rsi_score: float = 0.0

    # Total score
    total_score: float = 0.0

    # Additional info
    momentum_5d_pct: float = 0.0
    momentum_10d_pct: float = 0.0
    avg_volume: float = 0.0
    recent_volume: float = 0.0
    volume_ratio: float = 0.0
    ma_10: float = 0.0
    ma_30: float = 0.0
    rsi: float = 0.0
    signal: str = ""      # BUY / WATCH / AVOID
    reason: str = ""

    timestamp: str = ""


class StockAnalyzer:
    """Stock analysis & scoring engine"""

    @staticmethod
    def calc_rsi(prices: pd.Series, period: int = 14) -> float:
        """Calculate RSI (Relative Strength Index)"""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    @staticmethod
    def score_momentum(pct_change: float) -> float:
        """Momentum scoring (0~100)"""
        # Strong rise = high score, but excessive rise is penalized
        if pct_change > 20:
            return 70     # Possible overbought
        elif pct_change > 10:
            return 90
        elif pct_change > 5:
            return 100
        elif pct_change > 2:
            return 85
        elif pct_change > 0:
            return 70
        elif pct_change > -3:
            return 50     # Small decline = could be a buying opportunity
        elif pct_change > -5:
            return 60     # Dip buying opportunity
        elif pct_change > -10:
            return 40
        else:
            return 20     # Sharp drop = risky

    @staticmethod
    def score_volume(volume_ratio: float) -> float:
        """Volume ratio score (recent 5-day / 30-day average)"""
        if volume_ratio > 3.0:
            return 100    # Explosive interest
        elif volume_ratio > 2.0:
            return 90
        elif volume_ratio > 1.5:
            return 80
        elif volume_ratio > 1.2:
            return 70
        elif volume_ratio > 0.8:
            return 50     # Normal
        else:
            return 30     # Declining interest

    @staticmethod
    def score_ma_trend(price: float, ma_10: float, ma_30: float) -> float:
        """Moving average trend score"""
        score = 50
        if price > ma_10:
            score += 20
        if price > ma_30:
            score += 15
        if ma_10 > ma_30:
            score += 15   # Golden cross structure
        return min(score, 100)

    @staticmethod
    def score_volatility(daily_returns_std: float) -> float:
        """Volatility score (moderate volatility preferred)"""
        # Too low = no momentum, too high = risky
        if daily_returns_std < 0.005:
            return 30
        elif daily_returns_std < 0.01:
            return 50
        elif daily_returns_std < 0.02:
            return 80
        elif daily_returns_std < 0.03:
            return 90     # Moderate volatility
        elif daily_returns_std < 0.05:
            return 70
        else:
            return 40     # Excessive volatility

    @staticmethod
    def score_rsi(rsi: float) -> float:
        """RSI zone score"""
        if rsi < 30:
            return 90     # Oversold -> expect rebound
        elif rsi < 40:
            return 80
        elif rsi < 50:
            return 65
        elif rsi < 60:
            return 70     # Neutral to early bullish
        elif rsi < 70:
            return 75     # Bullish zone
        elif rsi < 80:
            return 50     # Approaching overbought
        else:
            return 25     # Overbought -> correction risk


# ═══════════════════════════════════════════════════════════════
#  Main Screener
# ═══════════════════════════════════════════════════════════════

class AutoScreener:
    """Automatic stock screening engine"""

    def __init__(self, config: ScreenerConfig = None):
        self.config = config or ScreenerConfig()
        self.ib = IB()
        self.analyzer = StockAnalyzer()
        self.scores: list[StockScore] = []
        self.picks: list[StockScore] = []
        self.screening_history: list = []

        self._setup_logging()
        os.makedirs(self.config.report_dir, exist_ok=True)

    def _setup_logging(self):
        self.logger = logging.getLogger("AutoScreener")
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
        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id
            )
            self.logger.info("✅ TWS connection successful (Screener)")
            return True
        except Exception as e:
            self.logger.error(f"❌ TWS connection failed: {e}")
            return False

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    # ── Data Collection ───────────────────────────────────────────

    def fetch_stock_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch stock historical data"""
        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.lookback_days,
                barSizeSetting=self.config.bar_size,
                whatToShow="ADJUSTED_LAST",
                useRTH=True,
                formatDate=1,
            )

            if not bars or len(bars) < 10:
                return None

            df = util.df(bars)
            df.set_index("date", inplace=True)
            return df

        except Exception as e:
            self.logger.warning(f"  ⚠️ {symbol} data failed: {e}")
            return None

    # ── Individual Stock Analysis ────────────────────────────────

    def analyze_stock(self, symbol: str) -> Optional[StockScore]:
        """Analyze a single stock -> return score"""
        df = self.fetch_stock_data(symbol)
        if df is None:
            return None

        close = df["close"]
        volume = df["volume"]

        score = StockScore(symbol=symbol)
        score.timestamp = datetime.now().isoformat()

        # Price info
        score.current_price = float(close.iloc[-1])
        score.price_5d_ago = float(close.iloc[-6]) if len(close) >= 6 else score.current_price
        score.price_10d_ago = float(close.iloc[-11]) if len(close) >= 11 else score.current_price

        # ── 1. Momentum (5-day) ──
        score.momentum_5d_pct = (
            (score.current_price - score.price_5d_ago) / score.price_5d_ago * 100
        )
        score.momentum_5d_score = self.analyzer.score_momentum(score.momentum_5d_pct)

        # ── 2. Momentum (10-day) ──
        score.momentum_10d_pct = (
            (score.current_price - score.price_10d_ago) / score.price_10d_ago * 100
        )
        score.momentum_10d_score = self.analyzer.score_momentum(score.momentum_10d_pct)

        # ── 3. Volume Surge ──
        score.avg_volume = float(volume.iloc[-30:].mean()) if len(volume) >= 30 else float(volume.mean())
        score.recent_volume = float(volume.iloc[-5:].mean())
        score.volume_ratio = (
            score.recent_volume / score.avg_volume
            if score.avg_volume > 0 else 1.0
        )
        score.volume_surge_score = self.analyzer.score_volume(score.volume_ratio)

        # Minimum volume filter
        if score.avg_volume < self.config.min_avg_volume:
            return None

        # ── 4. MA Trend ──
        score.ma_10 = float(close.rolling(10).mean().iloc[-1])
        score.ma_30 = float(close.rolling(min(30, len(close))).mean().iloc[-1])
        score.ma_trend_score = self.analyzer.score_ma_trend(
            score.current_price, score.ma_10, score.ma_30
        )

        # ── 5. Volatility ──
        daily_returns = close.pct_change().dropna()
        volatility = float(daily_returns.std()) if len(daily_returns) > 5 else 0.02
        score.volatility_score = self.analyzer.score_volatility(volatility)

        # ── 6. RSI ──
        score.rsi = self.analyzer.calc_rsi(close)
        score.rsi_score = self.analyzer.score_rsi(score.rsi)

        # ── Total Score Calculation ──
        cfg = self.config
        score.total_score = (
            score.momentum_5d_score * cfg.weight_momentum_5d +
            score.momentum_10d_score * cfg.weight_momentum_10d +
            score.volume_surge_score * cfg.weight_volume_surge +
            score.ma_trend_score * cfg.weight_ma_trend +
            score.volatility_score * cfg.weight_volatility +
            score.rsi_score * cfg.weight_rsi_zone
        )

        # ── Signal Decision ──
        if score.total_score >= 75:
            score.signal = "🟢 BUY"
            score.reason = self._generate_reason(score)
        elif score.total_score >= 55:
            score.signal = "🟡 WATCH"
            score.reason = "Hold - further confirmation needed"
        else:
            score.signal = "🔴 AVOID"
            score.reason = "Negative indicators"

        return score

    def _generate_reason(self, score: StockScore) -> str:
        """Generate buy reason"""
        reasons = []
        if score.momentum_5d_pct > 3:
            reasons.append(f"5-day +{score.momentum_5d_pct:.1f}% gain")
        if score.momentum_5d_pct < -3:
            reasons.append(f"5-day {score.momentum_5d_pct:.1f}% drop (dip buy)")
        if score.volume_ratio > 1.5:
            reasons.append(f"Volume surged {score.volume_ratio:.1f}x")
        if score.current_price > score.ma_10 > score.ma_30:
            reasons.append("Golden cross structure")
        if score.rsi < 40:
            reasons.append(f"RSI {score.rsi:.0f} oversold")
        elif 50 < score.rsi < 70:
            reasons.append(f"RSI {score.rsi:.0f} bullish zone")

        return " | ".join(reasons) if reasons else "Strong overall score"

    # ── Full Screening Execution ────────────────────────────────

    def run_screening(self) -> list[StockScore]:
        """Scan the full universe -> select TOP N"""
        self.logger.info("\n" + "═" * 60)
        self.logger.info(f"  🔍 Auto screening started [{datetime.now():%Y-%m-%d %H:%M}]")
        self.logger.info(f"  Universe: {len(self.config.universe)} stocks")
        self.logger.info("═" * 60)

        self.scores = []
        total = len(self.config.universe)

        for i, symbol in enumerate(self.config.universe):
            self.logger.info(
                f"  [{i+1}/{total}] Analyzing: {symbol}..."
            )

            score = self.analyze_stock(symbol)
            if score:
                self.scores.append(score)
                self.logger.info(
                    f"    → Score: {score.total_score:.1f} | "
                    f"5-day: {score.momentum_5d_pct:+.1f}% | "
                    f"RSI: {score.rsi:.0f} | "
                    f"Volume: {score.volume_ratio:.1f}x | "
                    f"{score.signal}"
                )

            # Prevent API rate limiting
            self.ib.sleep(0.5)

        # Sort by score
        self.scores.sort(key=lambda s: s.total_score, reverse=True)

        # Select TOP N
        self.picks = self.scores[:self.config.top_picks]

        self.logger.info(f"\n{'─' * 60}")
        self.logger.info(f"  📊 Screening complete! Analyzed: {len(self.scores)} stocks")
        self.logger.info(f"  🏆 TOP {self.config.top_picks} selected:")
        self.logger.info(f"{'─' * 60}")

        for i, pick in enumerate(self.picks):
            self.logger.info(
                f"  #{i+1:2d} {pick.symbol:6s} | "
                f"Score: {pick.total_score:5.1f} | "
                f"${pick.current_price:8.2f} | "
                f"5-day: {pick.momentum_5d_pct:+6.1f}% | "
                f"RSI: {pick.rsi:5.1f} | "
                f"{pick.signal} | {pick.reason}"
            )

        # Save results
        self._save_picks()
        self._save_report()
        self._save_history()

        return self.picks

    # ── Save Results ─────────────────────────────────────────────

    def _save_picks(self):
        """Save today's recommended stocks"""
        data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "total_screened": len(self.scores),
            "picks": [asdict(p) for p in self.picks],
            "all_scores": [
                {
                    "symbol": s.symbol,
                    "score": round(s.total_score, 1),
                    "signal": s.signal,
                }
                for s in self.scores[:30]  # Top 30 only
            ],
        }

        with open(self.config.picks_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        self.logger.info(f"  💾 Recommended stocks saved: {self.config.picks_file}")

    def _save_report(self):
        """Generate daily report"""
        date_str = datetime.now().strftime("%Y%m%d")
        report_path = os.path.join(
            self.config.report_dir, f"report_{date_str}.txt"
        )

        lines = []
        lines.append("=" * 70)
        lines.append(f"  📊 IB Smart Trader - Daily Screening Report")
        lines.append(f"  Date: {datetime.now():%Y-%m-%d %H:%M}")
        lines.append(f"  Stocks analyzed: {len(self.scores)}")
        lines.append("=" * 70)
        lines.append("")

        lines.append("  🏆 Today's TOP 10 Recommended Stocks:")
        lines.append("─" * 70)
        lines.append(
            f"  {'#':>3s} {'Symbol':6s} {'Score':>6s} {'Price':>10s} "
            f"{'5-day%':>7s} {'10-day%':>7s} {'RSI':>5s} {'Vol':>5s} Signal"
        )
        lines.append("─" * 70)

        for i, p in enumerate(self.picks):
            lines.append(
                f"  {i+1:3d} {p.symbol:6s} {p.total_score:6.1f} "
                f"${p.current_price:9.2f} {p.momentum_5d_pct:+6.1f}% "
                f"{p.momentum_10d_pct:+6.1f}% {p.rsi:5.1f} "
                f"{p.volume_ratio:4.1f}x {p.signal}"
            )

        lines.append("")
        lines.append("─" * 70)
        lines.append(f"  Investment plan: ${self.config.investment_per_stock:,.0f} per stock")
        lines.append(
            f"  Total investment: ${self.config.investment_per_stock * len(self.picks):,.0f}"
        )
        lines.append("")

        # Sector analysis
        lines.append("  📈 Sector Strength Analysis:")
        sector_scores = {}
        for s in self.scores:
            # Simple sector classification
            sector = self._classify_sector(s.symbol)
            if sector not in sector_scores:
                sector_scores[sector] = []
            sector_scores[sector].append(s.total_score)

        for sector, scores in sorted(
            sector_scores.items(),
            key=lambda x: np.mean(x[1]),
            reverse=True
        ):
            avg = np.mean(scores)
            bar = "█" * int(avg / 5)
            lines.append(f"    {sector:20s} {avg:5.1f} {bar}")

        lines.append("")
        lines.append("═" * 70)

        report_text = "\n".join(lines)

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)

        # Print to console as well
        print(report_text)
        self.logger.info(f"  📄 Report saved: {report_path}")

    def _save_history(self):
        """Accumulate screening history"""
        history = []
        if os.path.exists(self.config.history_file):
            with open(self.config.history_file, "r") as f:
                history = json.load(f)

        history.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "picks": [
                {
                    "symbol": p.symbol,
                    "score": round(p.total_score, 1),
                    "price": p.current_price,
                    "momentum_5d": round(p.momentum_5d_pct, 2),
                }
                for p in self.picks
            ]
        })

        # Keep only the last 90 days
        history = history[-90:]

        with open(self.config.history_file, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def _classify_sector(self, symbol: str) -> str:
        """Simple sector classification"""
        sectors = {
            "Tech / AI": ["NVDA","AAPL","MSFT","GOOGL","AMZN","META","TSLA","TSM","AVGO","AMD","INTC","MU","ORCL","CRM","PLTR","APP"],
            "Energy / Oil": ["XOM","CVX","COP","OXY","DVN","EOG","PXD","HES","SLB","HAL","BP","SHEL","MPC","VLO","PSX"],
            "Defense": ["LMT","NOC","RTX","GD","BA","LHX","AVAV","KTOS"],
            "Tankers": ["FRO","DHT","INSW","STNG","TNK"],
            "Nuclear/Utilities": ["CEG","VST","GEV","NRG","NEE"],
            "Financials": ["JPM","GS","MS","BAC","WFC","BRK-B","AXP"],
            "Healthcare": ["UNH","JNJ","LLY","PFE","ABBV","MRK","AMGN"],
            "Consumer": ["WMT","COST","HD","TGT","LULU","NKE"],
            "Gold/Commodities": ["GLD","NEM","GOLD","FNV"],
            "Fintech/Crypto": ["HOOD","MSTR","COIN","SQ"],
        }
        for sector, symbols in sectors.items():
            if symbol in symbols:
                return sector
        return "Other"

    # ── Smart Trader Integration ─────────────────────────────────

    def get_watchlist_for_trader(self) -> list[str]:
        """Watchlist to pass to Smart Trader"""
        return [p.symbol for p in self.picks]

    def get_picks_with_allocation(self) -> list[dict]:
        """Recommended list with investment allocation"""
        total_score = sum(p.total_score for p in self.picks)
        result = []

        for pick in self.picks:
            # Score-proportional allocation (weighted investment)
            weight = pick.total_score / total_score if total_score > 0 else 1.0 / len(self.picks)
            allocation = self.config.max_total_investment * weight
            shares = int(allocation / pick.current_price) if pick.current_price > 0 else 0

            result.append({
                "symbol": pick.symbol,
                "score": round(pick.total_score, 1),
                "price": pick.current_price,
                "allocation": round(allocation, 2),
                "shares": shares,
                "signal": pick.signal,
                "reason": pick.reason,
            })

        return result

    # ── Previous Day Performance Evaluation ────────────────────────

    def evaluate_previous_picks(self) -> Optional[dict]:
        """Evaluate actual performance of previous day's recommended stocks"""
        if not os.path.exists(self.config.picks_file):
            return None

        with open(self.config.picks_file, "r") as f:
            prev_data = json.load(f)

        prev_date = prev_data.get("date", "")
        today = datetime.now().strftime("%Y-%m-%d")

        if prev_date == today:
            self.logger.info("  ℹ️  Already screened today. Skipping previous day evaluation.")
            return None

        self.logger.info(f"\n📋 Previous day ({prev_date}) recommended stock performance evaluation:")
        self.logger.info("─" * 60)

        results = []
        total_pnl = 0

        for pick_data in prev_data.get("picks", []):
            symbol = pick_data["symbol"]
            prev_price = pick_data["current_price"]

            # Fetch current price
            df = self.fetch_stock_data(symbol)
            if df is None:
                continue

            current_price = float(df["close"].iloc[-1])
            pnl_pct = (current_price - prev_price) / prev_price * 100
            investment = self.config.investment_per_stock
            pnl_dollar = investment * (pnl_pct / 100)
            total_pnl += pnl_dollar

            icon = "🟢" if pnl_pct >= 0 else "🔴"
            self.logger.info(
                f"  {icon} {symbol:6s} | "
                f"${prev_price:.2f} → ${current_price:.2f} | "
                f"{pnl_pct:+.2f}% | "
                f"P&L: ${pnl_dollar:+,.0f}"
            )

            results.append({
                "symbol": symbol,
                "prev_price": prev_price,
                "current_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollar": round(pnl_dollar, 2),
            })

            self.ib.sleep(0.3)

        self.logger.info("─" * 60)
        self.logger.info(f"  📊 Total P&L: ${total_pnl:+,.2f}")
        self.logger.info("─" * 60)

        return {
            "date": prev_date,
            "eval_date": today,
            "results": results,
            "total_pnl": round(total_pnl, 2),
        }

    # ── Daemon Mode (Daily Automatic Execution) ────────────────────

    def run_daemon(self):
        """
        Daemon mode - Automatic screening at a scheduled time every day

        Process:
        1. Evaluate previous day's recommended stock performance
        2. Run new screening
        3. Select TOP 10 & generate report
        4. Pass watchlist to Smart Trader
        5. Wait until the next day
        """
        self.logger.info("\n" + "╔" + "═" * 58 + "╗")
        self.logger.info("║  🤖 Auto Screener - Daemon mode started                  ║")
        self.logger.info(f"║  Run time: Daily at {self.config.run_time_hour:02d}:{self.config.run_time_minute:02d}                                ║")
        self.logger.info("║  Ctrl+C to exit                                           ║")
        self.logger.info("╚" + "═" * 58 + "╝\n")

        while True:
            now = datetime.now()
            target = now.replace(
                hour=self.config.run_time_hour,
                minute=self.config.run_time_minute,
                second=0,
                microsecond=0,
            )

            # If today's time has passed, schedule for tomorrow
            if now >= target:
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            self.logger.info(
                f"⏰ Next screening: {target:%Y-%m-%d %H:%M} "
                f"({wait_seconds/3600:.1f} hours from now)"
            )

            # Wait
            try:
                time.sleep(wait_seconds)
            except KeyboardInterrupt:
                self.logger.info("\n🛑 Daemon mode terminated")
                break

            # Execute
            try:
                if not self.ib.isConnected():
                    if not self.connect():
                        self.logger.error("❌ TWS connection failed. Retrying in 1 hour.")
                        time.sleep(3600)
                        continue

                # 1) Previous day performance evaluation
                self.evaluate_previous_picks()

                # 2) New screening
                picks = self.run_screening()

                # 3) Create watchlist file (for Smart Trader integration)
                watchlist = self.get_picks_with_allocation()
                with open("today_watchlist.json", "w") as f:
                    json.dump({
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "watchlist": watchlist,
                    }, f, indent=2, ensure_ascii=False)

                self.logger.info(
                    f"\n✅ Screening complete! "
                    f"TOP {len(picks)}: "
                    f"{', '.join(p.symbol for p in picks)}"
                )

            except Exception as e:
                self.logger.error(f"❌ Screening error: {e}", exc_info=True)

            finally:
                self.disconnect()


# ═══════════════════════════════════════════════════════════════
#  Smart Trader Integrated Runner
# ═══════════════════════════════════════════════════════════════

def run_integrated(trade_mode: str = "alert"):
    """
    Screener + Smart Trader integrated execution

    1. Select TOP 10 via screener
    2. Pass results to Smart Trader
    3. Smart Trader monitors & executes trades
    """
    from smart_trader import SmartTrader, TradingConfig, TradeMode

    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🤖 IB Smart Trader + Auto Screener                     ║
    ║     Integrated Automated Trading System                  ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    # 1) Screening
    screener_config = ScreenerConfig()
    screener = AutoScreener(screener_config)

    if not screener.connect():
        return

    # Previous day performance evaluation
    screener.evaluate_previous_picks()

    # New screening
    picks = screener.run_screening()
    watchlist = [p.symbol for p in picks]
    screener.disconnect()

    if not watchlist:
        print("❌ No recommended stocks found.")
        return

    # 2) Smart Trader configuration
    mode = TradeMode.AUTO if trade_mode == "auto" else TradeMode.ALERT
    trader_config = TradingConfig(
        ib_port=7497,
        client_id=1,
        trade_mode=mode,
        ma_short_period=10,
        ma_long_period=30,
        buy_drop_pct=-5.0,
        sell_rise_pct=5.0,
        pct_lookback_days=5,
        default_quantity=10,
        check_interval_sec=60,
    )

    # 3) Run Smart Trader
    trader = SmartTrader(trader_config)
    trader.run(watchlist)


# ═══════════════════════════════════════════════════════════════
#  CLI Execution
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IB Smart Trader - Auto Stock Screener"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Daemon mode (daily automatic execution)"
    )
    parser.add_argument(
        "--integrated", action="store_true",
        help="Screener + Smart Trader integrated execution"
    )
    parser.add_argument(
        "--mode", choices=["alert", "auto"], default="alert",
        help="Trading mode: alert (notifications only) | auto (automated trading)"
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help="Evaluate previous day's recommended stock performance only"
    )

    args = parser.parse_args()

    if args.integrated:
        run_integrated(args.mode)
        return

    config = ScreenerConfig()
    config.save()
    screener = AutoScreener(config)

    if args.daemon:
        # Daemon mode
        screener.run_daemon()
    else:
        # One-time execution
        if not screener.connect():
            return

        try:
            if args.evaluate:
                screener.evaluate_previous_picks()
            else:
                screener.evaluate_previous_picks()
                picks = screener.run_screening()

                # Print investment allocation
                allocations = screener.get_picks_with_allocation()
                print("\n  💰 Investment Allocation:")
                print("─" * 60)
                for a in allocations:
                    print(
                        f"    {a['symbol']:6s} | "
                        f"${a['allocation']:9,.0f} | "
                        f"{a['shares']:4d} shares @ ${a['price']:.2f} | "
                        f"Score: {a['score']}"
                    )
                total_alloc = sum(a["allocation"] for a in allocations)
                print("─" * 60)
                print(f"    Total investment: ${total_alloc:,.0f}")
        finally:
            screener.disconnect()


if __name__ == "__main__":
    main()
