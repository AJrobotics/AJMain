#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  run.py - IB Smart Trader Unified Launch Script

  Usage:
    ── Swing Trading (default) ──
    python run.py                    # One-time screening (ALERT mode)
    python run.py --auto             # One-time screening + auto trading
    python run.py --daemon           # Daily auto screening (daemon)
    python run.py --daemon --auto    # Daily auto screening + auto trading
    python run.py --evaluate         # Evaluate previous day's performance only

    ── Day Trading ──
    python run.py --day              # Day trading ALERT mode
    python run.py --day --auto       # Day trading AUTO mode
    python run.py --day --daemon     # Auto start every morning
    python run.py --day --demo       # Offline demo

    ── Politician Trading (Congressional follower) ──
    python run.py --politician              # ALERT mode
    python run.py --politician --auto       # AUTO mode (auto trading)
    python run.py --politician --daemon     # Auto start daily
    python run.py --politician --demo       # Offline demo
═══════════════════════════════════════════════════════════════
"""

import argparse
import sys

def main():
    parser = argparse.ArgumentParser(
        description="🤖 IB Smart Trader + Auto Screener"
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Auto trading mode (default: alerts only)"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Daemon mode - auto run daily after market close"
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help="Run previous day's pick performance evaluation only"
    )
    parser.add_argument(
        "--screen-only", action="store_true",
        help="Run screening only (no trading)"
    )
    parser.add_argument(
        "--port", type=int, default=7497,
        help="TWS port (default: 7497 Paper, Live: 7496)"
    )
    parser.add_argument(
        "--day", action="store_true",
        help="Day trading mode (minute-bar based scalping + intraday)"
    )
    parser.add_argument(
        "--politician", action="store_true",
        help="Congressional trade follower mode (Congressional Trading Follower)"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Offline demo (strategy test without IB connection)"
    )

    args = parser.parse_args()

    # ── Day Trading mode ──
    if args.day:
        _run_day_trading(args)
        return

    # ── Politician Trading mode ──
    if args.politician:
        _run_politician_trading(args)
        return

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   🤖  IB Smart Trader v2.0                                   ║
    ║       Auto Screening + Algorithmic Trading                   ║
    ║                                                              ║
    ║   Strategy: MA Crossover + % Change + Momentum Scoring       ║
    ║   Universe: 90+ Stocks (Tech, Energy, Defense, Finance...)   ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    mode = "auto" if args.auto else "alert"
    print(f"  📋 Mode: {'🤖 AUTO (auto trading)' if args.auto else '🔔 ALERT (alerts only)'}")
    print(f"  📡 TWS Port: {args.port}")
    print(f"  ⏰ Daemon: {'ON (daily auto)' if args.daemon else 'OFF (one-time run)'}")
    print()

    if args.port == 7496:
        print("  ⚠️  Warning: LIVE trading port (7496) has been selected!")
        confirm = input("  Do you want to continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return

    if args.screen_only:
        # Screening only
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)

        if not screener.connect():
            return
        try:
            screener.evaluate_previous_picks()
            screener.run_screening()
            allocs = screener.get_picks_with_allocation()

            print("\n  💰 Recommended Picks & Investment Allocation:")
            print("─" * 60)
            for a in allocs:
                print(
                    f"    {a['symbol']:6s} | "
                    f"${a['allocation']:8,.0f} | "
                    f"{a['shares']:3d} shares | "
                    f"Score: {a['score']:5.1f} | "
                    f"{a['reason']}"
                )
        finally:
            screener.disconnect()

    elif args.evaluate:
        # Performance evaluation only
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)

        if screener.connect():
            screener.evaluate_previous_picks()
            screener.disconnect()

    elif args.daemon:
        # Daemon mode
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)
        screener.run_daemon()

    else:
        # Unified run (screening -> trading)
        from auto_screener import run_integrated
        run_integrated(mode)


def _run_day_trading(args):
    """Run day trading"""
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   🏎️  Day Trader v1.0                                        ║
    ║       Scalping + Intraday Algorithmic Trading                ║
    ║                                                              ║
    ║   Strategy: VWAP Bounce + EMA Scalp + Volume Breakout        ║
    ║             + RSI/MACD Combo (4-strategy ensemble)            ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    # Demo mode
    if args.demo:
        from day_trader import demo
        demo()
        return

    mode = "auto" if args.auto else "alert"
    print(f"  📋 Mode: {'🤖 AUTO (auto trading)' if args.auto else '🔔 ALERT (alerts only)'}")
    print(f"  📡 TWS Port: {args.port}")
    print(f"  ⏰ Daemon: {'ON (daily auto)' if args.daemon else 'OFF'}")
    print()

    if args.port == 7496:
        print("  ⚠️  Warning: LIVE trading port (7496) has been selected!")
        confirm = input("  Do you want to continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return

    from day_trader import DayTrader, DayTraderConfig, TradeMode

    config = DayTraderConfig(
        ib_port=args.port,
        trade_mode=TradeMode.AUTO if args.auto else TradeMode.ALERT,
    )
    trader = DayTrader(config)

    if args.daemon:
        trader.run_daemon()
    else:
        trader.run()


def _run_politician_trading(args):
    """Politician Trading - Congressional trade follower"""
    print("""
    +==============================================================+
    |                                                              |
    |   Politician Trader v1.0                                     |
    |   Congressional Trade Follower + Political Event Reactor     |
    |                                                              |
    |   Strategies: DisclosureFollower + ClusterDetection          |
    |               + CommitteeInsider + PoliticalEventReactor     |
    |               (4-strategy ensemble)                          |
    |                                                              |
    +==============================================================+
    """)

    if args.demo:
        from politician_trader import demo
        demo()
        return

    mode = "auto" if args.auto else "alert"
    print(f"  Mode: {'AUTO (live trading)' if args.auto else 'ALERT (signals only)'}")
    print(f"  TWS Port: {args.port}")
    print(f"  Daemon: {'ON (daily auto)' if args.daemon else 'OFF'}")
    print()

    if args.port == 7496:
        print("  WARNING: LIVE trading port (7496) selected!")
        confirm = input("  Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return

    from politician_trader import PoliticianTrader, PoliticianTraderConfig, TradeMode

    config = PoliticianTraderConfig(
        ib_port=args.port,
        trade_mode=TradeMode.AUTO if args.auto else TradeMode.ALERT,
    )
    trader = PoliticianTrader(config)

    if args.daemon:
        trader.run_daemon()
    else:
        trader.run()


if __name__ == "__main__":
    main()
