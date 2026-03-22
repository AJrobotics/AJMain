#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  run.py - IB Smart Trader 통합 실행 스크립트
  
  사용법:
    python run.py                    # 1회 스크리닝 (ALERT 모드)
    python run.py --auto             # 1회 스크리닝 + 자동매매
    python run.py --daemon           # 매일 자동 스크리닝 (데몬)
    python run.py --daemon --auto    # 매일 자동 스크리닝 + 자동매매
    python run.py --evaluate         # 전일 성과만 확인
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
    
    args = parser.parse_args()
    
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


if __name__ == "__main__":
    main()
