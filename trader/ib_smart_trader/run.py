#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  run.py - IB Smart Trader 통합 실행 스크립트

  사용법:
    ── Swing Trading (기존) ──
    python run.py                    # 1회 스크리닝 (ALERT 모드)
    python run.py --auto             # 1회 스크리닝 + 자동매매
    python run.py --daemon           # 매일 자동 스크리닝 (데몬)
    python run.py --daemon --auto    # 매일 자동 스크리닝 + 자동매매
    python run.py --evaluate         # 전일 성과만 확인

    ── Day Trading ──
    python run.py --day              # 데이 트레이딩 ALERT 모드
    python run.py --day --auto       # 데이 트레이딩 AUTO 모드
    python run.py --day --daemon     # 매일 아침 자동 시작
    python run.py --day --demo       # 오프라인 데모

    ── Politician Trading (의원 추종) ──
    python run.py --politician              # ALERT 모드
    python run.py --politician --auto       # AUTO 모드 (자동매매)
    python run.py --politician --daemon     # 매일 자동 시작
    python run.py --politician --demo       # 오프라인 데모
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
        help="자동매매 모드 (기본: 알림만)"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="데몬 모드 - 매일 장 마감 후 자동 실행"
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help="전일 추천 종목 성과 평가만 실행"
    )
    parser.add_argument(
        "--screen-only", action="store_true",
        help="스크리닝만 실행 (매매 안 함)"
    )
    parser.add_argument(
        "--port", type=int, default=7497,
        help="TWS 포트 (기본: 7497 Paper, Live: 7496)"
    )
    parser.add_argument(
        "--day", action="store_true",
        help="데이 트레이딩 모드 (분봉 기반 스캘핑+인트라데이)"
    )
    parser.add_argument(
        "--politician", action="store_true",
        help="의원 거래 추종 모드 (Congressional Trading Follower)"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="오프라인 데모 (IB 연결 없이 전략 테스트)"
    )

    args = parser.parse_args()

    # ── Day Trading 모드 ──
    if args.day:
        _run_day_trading(args)
        return

    # ── Politician Trading 모드 ──
    if args.politician:
        _run_politician_trading(args)
        return

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   🤖  IB Smart Trader v2.0                                   ║
    ║       자동 스크리닝 + 알고리즘 트레이딩                      ║
    ║                                                              ║
    ║   전략: MA Crossover + % 변동 + 모멘텀 스코어링              ║
    ║   유니버스: 90+ 종목 (Tech, Energy, Defense, Finance...)     ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    mode = "auto" if args.auto else "alert"
    print(f"  📋 모드: {'🤖 AUTO (자동매매)' if args.auto else '🔔 ALERT (알림만)'}")
    print(f"  📡 TWS 포트: {args.port}")
    print(f"  ⏰ 데몬: {'ON (매일 자동)' if args.daemon else 'OFF (1회 실행)'}")
    print()
    
    if args.port == 7496:
        print("  ⚠️  경고: LIVE 트레이딩 포트(7496)가 선택되었습니다!")
        confirm = input("  계속하시겠습니까? (yes/no): ")
        if confirm.lower() != "yes":
            print("  취소됨.")
            return
    
    if args.screen_only:
        # 스크리닝만
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)
        
        if not screener.connect():
            return
        try:
            screener.evaluate_previous_picks()
            screener.run_screening()
            allocs = screener.get_picks_with_allocation()
            
            print("\n  💰 추천 종목 & 투자 배분:")
            print("─" * 60)
            for a in allocs:
                print(
                    f"    {a['symbol']:6s} | "
                    f"${a['allocation']:8,.0f} | "
                    f"{a['shares']:3d}주 | "
                    f"점수: {a['score']:5.1f} | "
                    f"{a['reason']}"
                )
        finally:
            screener.disconnect()
    
    elif args.evaluate:
        # 성과 평가만
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)
        
        if screener.connect():
            screener.evaluate_previous_picks()
            screener.disconnect()
    
    elif args.daemon:
        # 데몬 모드
        from auto_screener import AutoScreener, ScreenerConfig
        config = ScreenerConfig(ib_port=args.port)
        screener = AutoScreener(config)
        screener.run_daemon()
    
    else:
        # 통합 실행 (스크리닝 → 매매)
        from auto_screener import run_integrated
        run_integrated(mode)


def _run_day_trading(args):
    """데이 트레이딩 실행"""
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║   🏎️  Day Trader v1.0                                        ║
    ║       스캘핑 + 인트라데이 알고리즘 트레이딩                  ║
    ║                                                              ║
    ║   전략: VWAP Bounce + EMA Scalp + Volume Breakout            ║
    ║         + RSI/MACD Combo (4개 앙상블)                        ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    # 데모 모드
    if args.demo:
        from day_trader import demo
        demo()
        return

    mode = "auto" if args.auto else "alert"
    print(f"  📋 모드: {'🤖 AUTO (자동매매)' if args.auto else '🔔 ALERT (알림만)'}")
    print(f"  📡 TWS 포트: {args.port}")
    print(f"  ⏰ 데몬: {'ON (매일 자동)' if args.daemon else 'OFF'}")
    print()

    if args.port == 7496:
        print("  ⚠️  경고: LIVE 트레이딩 포트(7496)가 선택되었습니다!")
        confirm = input("  계속하시겠습니까? (yes/no): ")
        if confirm.lower() != "yes":
            print("  취소됨.")
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
