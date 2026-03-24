"""
═══════════════════════════════════════════════════════════════════
  Politician Trading Strategies - Congressional Trade Following Engine

  Strategies (4-strategy ensemble):
    1. DisclosureFollower  (35%) - Follow high-reliability individual politicians
    2. ClusterDetection    (25%) - Detect simultaneous purchases by multiple politicians
    3. CommitteeInsider    (25%) - Committee-sector aligned trades
    4. PoliticalEventReactor(15%) - Political events -> sector trades

  Trade mode auto-selection:
    - Disclosure-based strategies (1,2,3) -> swing (days~weeks)
    - Event-based strategy (4)           -> day (intraday)
═══════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import Counter

from politician_data import (
    CongressionalTrade, PoliticianProfile, PoliticalEvent,
    DisclosureType, COMMITTEE_SECTOR_MAP, SECTOR_SYMBOLS,
)


# ═══════════════════════════════════════════════════════════════
#  Signal types (compatible with existing system)
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class PoliticianSignal:
    """Signal from an individual strategy"""
    strategy_name: str
    signal: SignalType
    confidence: float       # 0.0 ~ 1.0
    reason: str
    trade_mode: str = "swing"   # "swing" or "day"
    hold_period_days: int = 0   # recommended holding period
    metadata: dict = field(default_factory=dict)


@dataclass
class PoliticianEnsembleDecision:
    """Ensemble final decision"""
    symbol: str
    final_signal: SignalType
    consensus_score: float           # -1.0 ~ +1.0
    individual_signals: list
    trade_mode: str = "swing"        # auto-selected: "swing" or "day"
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    hold_period_days: int = 0
    source_disclosure: dict = field(default_factory=dict)
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self):
        sigs = ", ".join(
            f"{s.strategy_name}={s.signal.name}({s.confidence:.0%})"
            for s in self.individual_signals
        )
        sl = f"SL=${self.stop_loss_price:.2f}" if self.stop_loss_price > 0 else "SL=none"
        tp = f"TP=${self.take_profit_price:.2f}" if self.take_profit_price > 0 else "TP=none"
        return (
            f"{self.final_signal.value} {self.symbol} | "
            f"consensus: {self.consensus_score:+.2f} | "
            f"mode: {self.trade_mode} | "
            f"{sl} | {tp} | "
            f"strategies: [{sigs}]"
        )


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

@dataclass
class PoliticianStrategyConfig:
    """Politician-following strategy configuration"""

    # ── Strategy 1: DisclosureFollower ──
    min_politician_reliability: float = 0.45    # minimum reliability (follow threshold)
    committee_sector_boost: float = 0.20        # additional confidence for committee-sector match
    disclosure_recency_weight: bool = True      # higher confidence for more recent disclosures
    max_disclosure_age_days: int = 7            # Ignore disclosures older than 7 days

    # ── Strategy 2: ClusterDetection ──
    cluster_min_politicians: int = 3            # N or more simultaneous purchases = cluster
    cluster_lookback_days: int = 7              # Only look at last 7 days for clusters
    cluster_confidence_boost: float = 0.15

    # ── Strategy 3: CommitteeInsider ──
    committee_match_required: bool = False      # if True, only trade on committee-sector match
    committee_confidence_base: float = 0.70

    # ── Strategy 4: PoliticalEventReactor ──
    event_impact_threshold: float = 0.6         # minimum impact score
    event_sector_mapping: bool = True

    # ── Ensemble ──
    ensemble_buy_threshold: float = 0.30
    ensemble_sell_threshold: float = -0.30
    min_strategies_agree: int = 1

    # ── Weights (sum = 1.0) ──
    weight_disclosure: float = 0.35
    weight_cluster: float = 0.25
    weight_committee: float = 0.25
    weight_event: float = 0.15

    # ── Stop-loss / Take-profit (swing mode) ──
    swing_stop_loss_pct: float = 5.0
    swing_take_profit_pct: float = 12.0
    swing_default_hold_days: int = 21

    # ── Stop-loss / Take-profit (day mode) ──
    day_stop_loss_pct: float = 1.5
    day_take_profit_pct: float = 3.0


# ═══════════════════════════════════════════════════════════════
#  Strategy 1: DisclosureFollower
# ═══════════════════════════════════════════════════════════════

class DisclosureFollower:
    """
    High-reliability politician trade following strategy

    Core logic:
    - Only follow trades from politicians with strong past performance
    - Confidence based on reliability_score + disclosure amount + disclosure freshness
    - Purchase disclosure -> BUY, sale disclosure -> SELL
    """

    @staticmethod
    def get_signal(
        symbol: str,
        disclosures: list[CongressionalTrade],
        profiles: dict[str, PoliticianProfile],
        config: PoliticianStrategyConfig,
    ) -> PoliticianSignal:
        # Filter disclosures related to this symbol
        relevant = [d for d in disclosures if d.symbol == symbol]
        if not relevant:
            return PoliticianSignal(
                "DISCLOSURE_FOLLOWER", SignalType.HOLD, 0.0,
                f"{symbol}: No related disclosures",
            )

        # Prioritize most recent disclosures
        relevant.sort(key=lambda d: d.disclosure_date, reverse=True)
        best_trade = relevant[0]

        # Check politician profile
        profile = profiles.get(best_trade.politician_name)
        if not profile or profile.reliability_score < config.min_politician_reliability:
            rel_str = f"{profile.reliability_score:.3f}" if profile else "N/A"
            return PoliticianSignal(
                "DISCLOSURE_FOLLOWER", SignalType.HOLD, 0.2,
                f"{symbol}: {best_trade.politician_name} reliability {rel_str} < {config.min_politician_reliability}",
            )

        # Check disclosure age
        try:
            disc_dt = datetime.strptime(best_trade.disclosure_date[:10], "%Y-%m-%d")
            age_days = (datetime.now() - disc_dt).days
            if age_days > config.max_disclosure_age_days:
                return PoliticianSignal(
                    "DISCLOSURE_FOLLOWER", SignalType.HOLD, 0.2,
                    f"{symbol}: disclosure {age_days} days old (threshold: {config.max_disclosure_age_days} days)",
                )
        except Exception:
            age_days = 7

        # Calculate confidence
        base_confidence = profile.reliability_score

        # Amount bonus (higher for larger trades)
        amount = best_trade.midpoint_amount
        if amount >= 500_000:
            base_confidence += 0.15
        elif amount >= 100_000:
            base_confidence += 0.10
        elif amount >= 50_000:
            base_confidence += 0.05

        # Freshness bonus (higher for more recent disclosures)
        if config.disclosure_recency_weight:
            recency_bonus = max(0, (config.max_disclosure_age_days - age_days)) / config.max_disclosure_age_days * 0.10
            base_confidence += recency_bonus

        confidence = min(0.95, base_confidence)

        # Generate signal
        if best_trade.disclosure_type == DisclosureType.PURCHASE:
            signal = SignalType.BUY
            reason = (
                f"{best_trade.politician_name} ({best_trade.party}) purchase | "
                f"amount: ${amount:,.0f} | reliability: {profile.reliability_score:.3f} | "
                f"disclosed: {age_days} days ago"
            )
        elif best_trade.disclosure_type == DisclosureType.SALE:
            signal = SignalType.SELL
            reason = (
                f"{best_trade.politician_name} ({best_trade.party}) sale | "
                f"amount: ${amount:,.0f} | disclosed: {age_days} days ago"
            )
        else:
            signal = SignalType.HOLD
            reason = f"{symbol}: exchange trade — direction unclear"
            confidence = 0.3

        return PoliticianSignal(
            "DISCLOSURE_FOLLOWER", signal, confidence, reason,
            trade_mode="swing",
            hold_period_days=config.swing_default_hold_days,
            metadata={
                "politician": best_trade.politician_name,
                "amount": amount,
                "reliability": profile.reliability_score,
                "age_days": age_days,
            },
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 2: ClusterDetection
# ═══════════════════════════════════════════════════════════════

class ClusterDetection:
    """
    Multi-politician simultaneous purchase detection strategy

    Core logic:
    - 3+ politicians buying the same stock within 14 days -> strong BUY signal
    - More politicians -> higher confidence
    - Bipartisan crossover (both parties buying) -> additional weight
    """

    @staticmethod
    def get_signal(
        symbol: str,
        disclosures: list[CongressionalTrade],
        profiles: dict[str, PoliticianProfile],
        config: PoliticianStrategyConfig,
    ) -> PoliticianSignal:
        # Purchase disclosures within the last N days
        cutoff = datetime.now() - timedelta(days=config.cluster_lookback_days)
        recent_buys = []
        recent_sells = []

        for d in disclosures:
            if d.symbol != symbol:
                continue
            try:
                disc_dt = datetime.strptime(d.disclosure_date[:10], "%Y-%m-%d")
                if disc_dt < cutoff:
                    continue
            except Exception:
                continue

            if d.disclosure_type == DisclosureType.PURCHASE:
                recent_buys.append(d)
            elif d.disclosure_type == DisclosureType.SALE:
                recent_sells.append(d)

        # Unique politician count
        buy_politicians = list(set(d.politician_name for d in recent_buys))
        sell_politicians = list(set(d.politician_name for d in recent_sells))

        buy_count = len(buy_politicians)
        sell_count = len(sell_politicians)

        if buy_count < config.cluster_min_politicians and sell_count < config.cluster_min_politicians:
            return PoliticianSignal(
                "CLUSTER_DETECTION", SignalType.HOLD, 0.2,
                f"{symbol}: below cluster threshold (buys: {buy_count}, sells: {sell_count}, threshold: {config.cluster_min_politicians})",
            )

        # Buy cluster
        if buy_count >= config.cluster_min_politicians and buy_count > sell_count:
            confidence = 0.60
            # Extra politician count bonus
            extra = buy_count - config.cluster_min_politicians
            confidence += extra * config.cluster_confidence_boost

            # Bipartisan crossover bonus
            parties = set(d.party for d in recent_buys if d.party)
            if len(parties) >= 2:
                confidence += 0.10

            confidence = min(0.95, confidence)

            return PoliticianSignal(
                "CLUSTER_DETECTION", SignalType.BUY, confidence,
                f"{symbol}: {buy_count}-politician buy cluster | "
                f"politicians: {', '.join(buy_politicians[:4])} | "
                f"bipartisan: {'yes' if len(parties) >= 2 else 'no'}",
                trade_mode="swing",
                hold_period_days=config.swing_default_hold_days,
                metadata={
                    "buy_count": buy_count,
                    "politicians": buy_politicians,
                    "bipartisan": len(parties) >= 2,
                },
            )

        # Sell cluster
        if sell_count >= config.cluster_min_politicians and sell_count > buy_count:
            confidence = 0.55
            extra = sell_count - config.cluster_min_politicians
            confidence += extra * config.cluster_confidence_boost
            confidence = min(0.90, confidence)

            return PoliticianSignal(
                "CLUSTER_DETECTION", SignalType.SELL, confidence,
                f"{symbol}: {sell_count}-politician sell cluster",
                trade_mode="swing",
                metadata={"sell_count": sell_count, "politicians": sell_politicians},
            )

        return PoliticianSignal(
            "CLUSTER_DETECTION", SignalType.HOLD, 0.3,
            f"{symbol}: buys {buy_count} vs sells {sell_count} — mixed",
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 3: CommitteeInsider
# ═══════════════════════════════════════════════════════════════

class CommitteeInsider:
    """
    Committee-sector aligned trade strategy

    Core logic:
    - Politician trading stocks in their committee's jurisdiction = possible insider info
    - Armed Services member -> defense stock purchase -> high confidence
    - No committee match -> treated same as a regular disclosure
    """

    @staticmethod
    def get_signal(
        symbol: str,
        disclosures: list[CongressionalTrade],
        profiles: dict[str, PoliticianProfile],
        config: PoliticianStrategyConfig,
    ) -> PoliticianSignal:
        relevant = [d for d in disclosures if d.symbol == symbol]
        if not relevant:
            return PoliticianSignal(
                "COMMITTEE_INSIDER", SignalType.HOLD, 0.0,
                f"{symbol}: No related disclosures",
            )

        # Committee-sector match check
        matches = []
        for trade in relevant:
            for committee in trade.committees:
                sectors_for_committee = COMMITTEE_SECTOR_MAP.get(committee, [])
                # Check if the stock's sector falls under the committee's jurisdiction
                if trade.sector and trade.sector in sectors_for_committee:
                    matches.append((trade, committee))
                    continue
                # Check if the symbol itself is in the committee's symbol list
                if symbol in sectors_for_committee:
                    matches.append((trade, committee))

        if not matches:
            if config.committee_match_required:
                return PoliticianSignal(
                    "COMMITTEE_INSIDER", SignalType.HOLD, 0.2,
                    f"{symbol}: No committee-sector match",
                )
            # Weak signal even without match
            best = relevant[0]
            if best.disclosure_type == DisclosureType.PURCHASE:
                return PoliticianSignal(
                    "COMMITTEE_INSIDER", SignalType.BUY, 0.35,
                    f"{symbol}: {best.politician_name} purchase (no committee match)",
                    trade_mode="swing",
                )
            return PoliticianSignal(
                "COMMITTEE_INSIDER", SignalType.HOLD, 0.25,
                f"{symbol}: No committee match, weak signal",
            )

        # Highest confidence among matched trades
        best_trade, best_committee = matches[0]
        profile = profiles.get(best_trade.politician_name)

        confidence = config.committee_confidence_base
        if profile:
            confidence += profile.reliability_score * 0.15

        # Multiple match bonus
        if len(matches) > 1:
            confidence += 0.10

        confidence = min(0.95, confidence)

        if best_trade.disclosure_type == DisclosureType.PURCHASE:
            return PoliticianSignal(
                "COMMITTEE_INSIDER", SignalType.BUY, confidence,
                f"{symbol}: {best_trade.politician_name} [{best_committee}] member -> "
                f"jurisdictional sector ({best_trade.sector}) purchase! | "
                f"amount: ${best_trade.midpoint_amount:,.0f}",
                trade_mode="swing",
                hold_period_days=config.swing_default_hold_days,
                metadata={
                    "politician": best_trade.politician_name,
                    "committee": best_committee,
                    "sector": best_trade.sector,
                    "match_count": len(matches),
                },
            )
        elif best_trade.disclosure_type == DisclosureType.SALE:
            return PoliticianSignal(
                "COMMITTEE_INSIDER", SignalType.SELL, confidence * 0.9,
                f"{symbol}: {best_trade.politician_name} [{best_committee}] member -> "
                f"jurisdictional sector sale!",
                trade_mode="swing",
                metadata={"politician": best_trade.politician_name, "committee": best_committee},
            )

        return PoliticianSignal(
            "COMMITTEE_INSIDER", SignalType.HOLD, 0.3,
            f"{symbol}: committee match found but exchange trade",
        )


# ═══════════════════════════════════════════════════════════════
#  Strategy 4: PoliticalEventReactor
# ═══════════════════════════════════════════════════════════════

class PoliticalEventReactor:
    """
    Political event reaction strategy

    Core logic:
    - Immediately react to bill passages, executive orders, hearings, etc.
    - Event -> affected sectors -> representative stocks/ETFs
    - Speed is key -> day mode
    """

    @staticmethod
    def get_signal(
        symbol: str,
        events: list[PoliticalEvent],
        config: PoliticianStrategyConfig,
    ) -> PoliticianSignal:
        if not events:
            return PoliticianSignal(
                "EVENT_REACTOR", SignalType.HOLD, 0.0,
                f"{symbol}: No related political events",
                trade_mode="day",
            )

        # Find events that affect this symbol
        relevant_events = []
        for event in events:
            if event.impact_score < config.event_impact_threshold:
                continue
            # Check if the symbol belongs to an affected sector
            for sector in event.affected_sectors:
                sector_symbols = SECTOR_SYMBOLS.get(sector, [])
                if symbol in sector_symbols:
                    relevant_events.append((event, sector))
                    break

        if not relevant_events:
            return PoliticianSignal(
                "EVENT_REACTOR", SignalType.HOLD, 0.1,
                f"{symbol}: No impacting events",
                trade_mode="day",
            )

        # Most impactful event
        best_event, best_sector = max(relevant_events, key=lambda x: x[0].impact_score)
        confidence = best_event.impact_score * 0.85

        # Multiple event bonus
        if len(relevant_events) > 1:
            confidence += 0.10

        confidence = min(0.90, confidence)

        if best_event.sentiment == "bullish":
            return PoliticianSignal(
                "EVENT_REACTOR", SignalType.BUY, confidence,
                f"{symbol}: [{best_event.event_type}] {best_event.title[:50]} -> "
                f"{best_sector} sector beneficiary",
                trade_mode="day",
                hold_period_days=1,
                metadata={
                    "event_type": best_event.event_type,
                    "sector": best_sector,
                    "impact": best_event.impact_score,
                },
            )
        elif best_event.sentiment == "bearish":
            return PoliticianSignal(
                "EVENT_REACTOR", SignalType.SELL, confidence,
                f"{symbol}: [{best_event.event_type}] {best_event.title[:50]} -> "
                f"{best_sector} sector adversely affected",
                trade_mode="day",
                hold_period_days=1,
                metadata={
                    "event_type": best_event.event_type,
                    "sector": best_sector,
                    "impact": best_event.impact_score,
                },
            )

        return PoliticianSignal(
            "EVENT_REACTOR", SignalType.HOLD, 0.3,
            f"{symbol}: [{best_event.event_type}] neutral event",
            trade_mode="day",
        )


# ═══════════════════════════════════════════════════════════════
#  Ensemble engine
# ═══════════════════════════════════════════════════════════════

class PoliticianStrategyEnsemble:
    """
    Consensus-based trading decision from 4 strategies

    How it works:
    1. Each strategy returns BUY/SELL/HOLD + confidence
    2. Weighted sum: BUY=+1, SELL=-1, HOLD=0 x confidence x weight
    3. Threshold exceeded + minimum N strategies agree -> trade
    4. Auto trade_mode: EventReactor dominant -> day, otherwise swing
    """

    def __init__(self, config: PoliticianStrategyConfig = None):
        self.config = config or PoliticianStrategyConfig()

    def analyze(
        self,
        symbol: str,
        disclosures: list[CongressionalTrade],
        profiles: dict[str, PoliticianProfile],
        events: list[PoliticalEvent],
        current_price: float,
    ) -> PoliticianEnsembleDecision:
        """Run all strategies -> ensemble decision"""
        cfg = self.config
        signals: list[PoliticianSignal] = []

        # ── Strategy 1: DisclosureFollower ──
        signals.append(DisclosureFollower.get_signal(symbol, disclosures, profiles, cfg))

        # ── Strategy 2: ClusterDetection ──
        signals.append(ClusterDetection.get_signal(symbol, disclosures, profiles, cfg))

        # ── Strategy 3: CommitteeInsider ──
        signals.append(CommitteeInsider.get_signal(symbol, disclosures, profiles, cfg))

        # ── Strategy 4: PoliticalEventReactor ──
        signals.append(PoliticalEventReactor.get_signal(symbol, events, cfg))

        # === Ensemble score calculation ===
        weight_map = {
            "DISCLOSURE_FOLLOWER": cfg.weight_disclosure,
            "CLUSTER_DETECTION": cfg.weight_cluster,
            "COMMITTEE_INSIDER": cfg.weight_committee,
            "EVENT_REACTOR": cfg.weight_event,
        }

        consensus_score = 0.0
        buy_count = 0
        sell_count = 0
        event_dominant = False

        for sig in signals:
            weight = weight_map.get(sig.strategy_name, 0.1)
            if sig.signal == SignalType.BUY:
                consensus_score += sig.confidence * weight
                buy_count += 1
                if sig.strategy_name == "EVENT_REACTOR" and sig.confidence > 0.5:
                    event_dominant = True
            elif sig.signal == SignalType.SELL:
                consensus_score -= sig.confidence * weight
                sell_count += 1
                if sig.strategy_name == "EVENT_REACTOR" and sig.confidence > 0.5:
                    event_dominant = True

        # Normalize (-1 ~ +1)
        max_possible = sum(weight_map.values())
        if max_possible > 0:
            consensus_score = consensus_score / max_possible

        # === Auto trade_mode selection ===
        trade_mode = "day" if event_dominant else "swing"

        # === Final decision ===
        final_signal = SignalType.HOLD
        reason_parts = []

        if (consensus_score >= cfg.ensemble_buy_threshold and
                buy_count >= cfg.min_strategies_agree):
            final_signal = SignalType.BUY
            reason_parts.append(
                f"consensus BUY ({buy_count} strategies, score: {consensus_score:+.2f})"
            )
        elif (consensus_score <= cfg.ensemble_sell_threshold and
              sell_count >= cfg.min_strategies_agree):
            final_signal = SignalType.SELL
            reason_parts.append(
                f"consensus SELL ({sell_count} strategies, score: {consensus_score:+.2f})"
            )
        else:
            reason_parts.append(
                f"no consensus (BUY:{buy_count} SELL:{sell_count}, "
                f"score: {consensus_score:+.2f}, threshold: +/-{cfg.ensemble_buy_threshold})"
            )

        # === Stop-loss / Take-profit ===
        if trade_mode == "day":
            stop_loss = round(current_price * (1 - cfg.day_stop_loss_pct / 100), 2)
            take_profit = round(current_price * (1 + cfg.day_take_profit_pct / 100), 2)
            hold_days = 1
            reason_parts.append(
                f"Day mode | SL={cfg.day_stop_loss_pct}% TP={cfg.day_take_profit_pct}%"
            )
        else:
            stop_loss = round(current_price * (1 - cfg.swing_stop_loss_pct / 100), 2)
            take_profit = round(current_price * (1 + cfg.swing_take_profit_pct / 100), 2)
            hold_days = cfg.swing_default_hold_days
            reason_parts.append(
                f"Swing mode | SL={cfg.swing_stop_loss_pct}% TP={cfg.swing_take_profit_pct}% | "
                f"hold: {hold_days} days"
            )

        if final_signal == SignalType.SELL:
            # Invert SL/TP for sell
            stop_loss = round(current_price * (1 + cfg.swing_stop_loss_pct / 100), 2) if trade_mode == "swing" else round(current_price * (1 + cfg.day_stop_loss_pct / 100), 2)
            take_profit = round(current_price * (1 - cfg.swing_take_profit_pct / 100), 2) if trade_mode == "swing" else round(current_price * (1 - cfg.day_take_profit_pct / 100), 2)

        # Related disclosure info
        source_disc = {}
        relevant_discs = [d for d in disclosures if d.symbol == symbol]
        if relevant_discs:
            d = relevant_discs[0]
            source_disc = {
                "politician": d.politician_name,
                "type": d.disclosure_type.value,
                "amount": d.midpoint_amount,
                "date": d.disclosure_date,
            }

        return PoliticianEnsembleDecision(
            symbol=symbol,
            final_signal=final_signal,
            consensus_score=round(consensus_score, 3),
            individual_signals=signals,
            trade_mode=trade_mode,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            hold_period_days=hold_days,
            source_disclosure=source_disc,
            reason=" | ".join(reason_parts),
        )


# ═══════════════════════════════════════════════════════════════
#  Demo
# ═══════════════════════════════════════════════════════════════

def demo():
    """Demo — test ensemble with dummy data"""
    from politician_data import PoliticianDataFetcher, PoliticianDataConfig

    print("=" * 70)
    print("  🏛️  Politician Trading Strategies Demo")
    print("=" * 70)

    fetcher = PoliticianDataFetcher(PoliticianDataConfig(cache_dir="politician_cache"))
    disclosures = fetcher.fetch_recent_disclosures()
    profiles = fetcher.build_politician_profiles()
    events = fetcher.fetch_political_events()

    config = PoliticianStrategyConfig()
    ensemble = PoliticianStrategyEnsemble(config)

    # Target symbols (symbols found in disclosures)
    symbols_to_test = list(set(d.symbol for d in disclosures))

    # Mock current prices
    mock_prices = {
        "NVDA": 185.00, "LMT": 520.00, "XOM": 110.00,
        "RTX": 125.00, "AAPL": 220.00, "GOOGL": 175.00,
    }

    for symbol in symbols_to_test:
        price = mock_prices.get(symbol, 100.00)
        decision = ensemble.analyze(symbol, disclosures, profiles, events, price)

        print(f"\n  📊 {symbol} @ ${price:.2f}")
        print(f"  {decision}")
        print(f"  Individual strategies:")
        for sig in decision.individual_signals:
            print(
                f"    {sig.strategy_name:22s} -> {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) [{sig.trade_mode}] {sig.reason[:60]}"
            )

    print("\n" + "=" * 70)


if __name__ == "__main__":
    demo()
