"""
═══════════════════════════════════════════════════════════════════
  Day Trader v1.0 - IB 데이 트레이딩 자동매매 엔진

  스캘핑(1-5분) + 인트라데이(15분-1시간) 혼합 전략
  유동성 높은 대형주 + 프리마켓 핫 종목 대상

  모듈 통합:
    - day_strategies.py — VWAP, EMA, Volume, RSI+MACD 앙상블
    - day_risk.py      — 일일 리스크 관리, EOD 청산
    - signal_bridge.py — 시장 센티먼트 (선택)

  실행:
    python run.py --day                # ALERT 모드
    python run.py --day --auto         # AUTO 모드 (자동매매)
    python run.py --day --daemon       # 매일 자동 시작
═══════════════════════════════════════════════════════════════════
"""

import logging
import time
import json
import os
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
    HAS_IB = True
except ImportError as e:
    print(f"  필수 패키지 설치 필요: pip install ib_insync pandas numpy")
    print(f"  Missing: {e}")
    HAS_IB = False


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

class TradeMode(Enum):
    AUTO = "auto"
    ALERT = "alert"


@dataclass
class DayTraderConfig:
    """데이 트레이더 전체 설정"""

    # ── IB 연결 ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # Paper: 7497, Live: 7496
    client_id: int = 3           # 기존 SmartTrader(1), Screener(2)와 분리

    # ── 모드 ──
    trade_mode: TradeMode = TradeMode.ALERT

    # ── 자본 ──
    capital: float = 75_000.0

    # ── 프리마켓 스캐너 ──
    core_watchlist: list = field(default_factory=lambda: [
        "NVDA", "AAPL", "TSLA", "AMD", "META",
        "AMZN", "GOOGL", "MSFT", "QQQ", "SPY",
    ])
    scanner_gap_threshold_pct: float = 2.0    # 프리마켓 갭 최소 ±2%
    scanner_max_hot_stocks: int = 5           # 프리마켓 핫 종목 최대 추가 수

    # ── 분봉 설정 ──
    primary_bar_size: str = "5 mins"          # 주 분석 타임프레임
    scalp_bar_size: str = "1 min"             # 스캘핑 타임프레임
    history_duration: str = "1 D"             # 당일 데이터
    analysis_interval_sec: int = 30           # 분석 주기 (초)

    # ── 주문 설정 ──
    use_limit_orders: bool = False            # True: 지정가, False: 시장가
    limit_offset_pct: float = 0.05            # 지정가 오프셋 (%)

    # ── 로깅 ──
    log_file: str = "day_trader.log"


# ═══════════════════════════════════════════════════════════════
#  프리마켓 스캐너
# ═══════════════════════════════════════════════════════════════

class PremarketScanner:
    """
    프리마켓 유동성 종목 탐색

    1. 고정 핵심 리스트 (항상 포함)
    2. IB Scanner로 프리마켓 갭 + 거래량 급증 종목 추가
    """

    def __init__(self, ib: 'IB', config: DayTraderConfig):
        self.ib = ib
        self.config = config
        self.logger = logging.getLogger("PremarketScanner")

    def scan(self) -> list[str]:
        """프리마켓 스캔 → 최종 워치리스트 반환"""
        watchlist = list(self.config.core_watchlist)
        self.logger.info(f"📋 핵심 워치리스트: {watchlist}")

        # IB Scanner로 핫 종목 추가
        hot_stocks = self._scan_premarket_movers()
        for sym in hot_stocks:
            if sym not in watchlist:
                watchlist.append(sym)
                self.logger.info(f"  🔥 핫 종목 추가: {sym}")

        self.logger.info(f"📋 최종 워치리스트 ({len(watchlist)}개): {watchlist}")
        return watchlist

    def _scan_premarket_movers(self) -> list[str]:
        """IB Scanner API로 프리마켓 갭 종목 탐색"""
        hot = []
        try:
            # IB Scanner: 프리마켓 갭 상위 종목
            scan_params = ScannerSubscription(
                instrument="STK",
                locationCode="STK.US.MAJOR",
                scanCode="TOP_PERC_GAIN",
                numberOfRows=20,
                abovePrice=10.0,          # $10 이상
                aboveVolume=100000,        # 거래량 10만+
            )
            scan_data = self.ib.reqScannerData(scan_params)

            for item in scan_data[:self.config.scanner_max_hot_stocks]:
                sym = item.contractDetails.contract.symbol
                hot.append(sym)

            self.ib.cancelScannerSubscription(scan_data)
            self.logger.info(f"  🔍 IB Scanner: {len(hot)}개 핫 종목 발견")

        except Exception as e:
            self.logger.warning(f"  ⚠️ 프리마켓 스캔 실패 (핵심 리스트만 사용): {e}")

        return hot


# ═══════════════════════════════════════════════════════════════
#  메인 데이 트레이더
# ═══════════════════════════════════════════════════════════════

class DayTrader:
    """IB 데이 트레이딩 자동매매 엔진"""

    def __init__(self, config: DayTraderConfig = None):
        self.config = config or DayTraderConfig()
        self.ib = IB() if HAS_IB else None
        self.running = False
        self.watchlist: list = []       # IB Contract 리스트
        self.watchlist_symbols: list[str] = []
        self.positions: dict = {}       # Day Trader 자체 포지션만 추적 (IB 전체 X)
        self.signals_history: list = []

        # 전략 엔진
        self.ensemble = None
        self.strategy_config = None
        try:
            from day_strategies import DayStrategyEnsemble, DayStrategyConfig
            self.strategy_config = DayStrategyConfig()
            self.ensemble = DayStrategyEnsemble(self.strategy_config)
        except ImportError:
            print("  ⚠️ day_strategies.py 미발견")

        # 리스크 매니저
        self.risk_manager = None
        try:
            from day_risk import DayRiskManager, DayRiskConfig
            self.risk_manager = DayRiskManager(
                DayRiskConfig(capital=self.config.capital)
            )
        except ImportError:
            print("  ⚠️ day_risk.py 미발견")

        # Signal Bridge (선택)
        self.signal_bridge = None
        try:
            from signal_bridge import SignalBridge, SignalBridgeConfig
            self.signal_bridge = SignalBridge(SignalBridgeConfig())
        except ImportError:
            pass  # 선택 모듈

        self._setup_logging()

    def _setup_logging(self):
        self.logger = logging.getLogger("DayTrader")
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

    # ── IB 연결 ───────────────────────────────────────────────

    def connect(self) -> bool:
        """IB Gateway/TWS 연결"""
        if not HAS_IB:
            self.logger.error("❌ ib_insync 미설치")
            return False

        self.logger.info("=" * 60)
        self.logger.info("  🏎️ Day Trader v1.0 시작")
        self.logger.info(f"  모드: {self.config.trade_mode.value.upper()}")
        self.logger.info(f"  자본: ${self.config.capital:,.0f}")
        self.logger.info("=" * 60)

        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id,
            )
            accounts = self.ib.managedAccounts()
            self.logger.info(f"✅ IB 연결 성공 | 계좌: {accounts}")
            return True
        except Exception as e:
            self.logger.error(f"❌ IB 연결 실패: {e}")
            return False

    def disconnect(self):
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            self.logger.info("🔌 IB 연결 해제")

    # ── 워치리스트 ────────────────────────────────────────────

    def setup_watchlist(self, symbols: list[str] = None):
        """워치리스트 설정 (프리마켓 스캔 또는 수동)"""
        if symbols is None:
            # 프리마켓 스캐너 실행
            scanner = PremarketScanner(self.ib, self.config)
            symbols = scanner.scan()

        self.watchlist = []
        self.watchlist_symbols = []

        for sym in symbols:
            try:
                contract = Stock(sym, "SMART", "USD")
                self.ib.qualifyContracts(contract)
                self.watchlist.append(contract)
                self.watchlist_symbols.append(sym)
            except Exception as e:
                self.logger.warning(f"  ⚠️ {sym} 계약 검증 실패: {e}")

        self.logger.info(f"👀 워치리스트: {self.watchlist_symbols}")

    def load_portfolio(self):
        """Day Trader 자체 포지션만 로드 (risk_manager 기반).

        ⚠️ 중요: IB 포트폴리오(ib.portfolio())는 계좌 내 모든 포지션을 반환하므로
        Smart Trader 등 다른 전략의 포지션과 섞입니다.
        Day Trader는 반드시 risk_manager.positions만 참조하여
        자기가 직접 매수한 포지션만 관리합니다.
        """
        # IB 전체 포트폴리오는 참고용으로만 읽음 (self.positions에 저장하지 않음)
        self.positions = {}
        if self.risk_manager and self.risk_manager.positions:
            for sym, pos in self.risk_manager.positions.items():
                self.positions[sym] = {
                    "contract": None,
                    "quantity": pos.quantity,
                    "avg_cost": pos.entry_price,
                    "market_value": 0,
                    "unrealized_pnl": 0,
                }

    # ── 데이터 수집 ───────────────────────────────────────────

    def get_intraday_bars(self, contract, bar_size: str = None) -> Optional[pd.DataFrame]:
        """분봉 데이터 수집"""
        if bar_size is None:
            bar_size = self.config.primary_bar_size

        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.history_duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if not bars:
                return None

            df = util.df(bars)
            df.set_index("date", inplace=True)
            return df
        except Exception as e:
            self.logger.error(f"❌ {contract.symbol} 분봉 요청 실패: {e}")
            return None

    def get_current_price(self, contract) -> Optional[float]:
        """현재가 조회"""
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(1)
            price = ticker.marketPrice()
            if price and not pd.isna(price):
                return float(price)
            price = ticker.last
            if price and not pd.isna(price):
                return float(price)
            return None
        except Exception:
            return None

    # ── 분석 & 매매 ──────────────────────────────────────────

    def analyze_stock(self, contract) -> Optional[dict]:
        """단일 종목 분봉 분석"""
        symbol = contract.symbol
        df = self.get_intraday_bars(contract)

        if df is None or len(df) < 30:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # 오전 세션 체크
        is_morning = False
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            is_morning = now_et.hour < 11 or (now_et.hour == 10 and now_et.minute <= 30)
        except Exception:
            pass

        # 앙상블 분석
        if self.ensemble is None:
            return None

        decision = self.ensemble.analyze(symbol, close, high, low, volume, is_morning)

        # Signal Bridge 체크 (선택)
        bridge_blocked = False
        if self.signal_bridge and decision.final_signal.name == "BUY":
            blocked, reason = self.signal_bridge.should_block_buy()
            if blocked:
                self.logger.info(f"  📡 {reason}")
                bridge_blocked = True

        return {
            "symbol": symbol,
            "contract": contract,
            "decision": decision,
            "bridge_blocked": bridge_blocked,
            "current_price": float(close.iloc[-1]),
        }

    def process_signal(self, analysis: dict):
        """분석 결과 → 주문 실행"""
        decision = analysis["decision"]
        symbol = analysis["symbol"]
        price = analysis["current_price"]

        # 로깅
        buy_count = sum(1 for s in decision.individual_signals if s.signal.name == "BUY")
        sell_count = sum(1 for s in decision.individual_signals if s.signal.name == "SELL")

        self.logger.info(
            f"  🎯 {symbol} | {decision.final_signal.value} | "
            f"합의: {decision.consensus_score:+.3f} | "
            f"BUY:{buy_count} SELL:{sell_count}"
        )
        for sig in decision.individual_signals:
            self.logger.info(
                f"      {sig.strategy_name:15s} → {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) {sig.reason}"
            )

        if decision.final_signal.name == "HOLD":
            return

        if analysis.get("bridge_blocked"):
            return

        # 리스크 체크
        if self.risk_manager:
            risk = self.risk_manager.check_risk(symbol, price)

            # 강제 청산 필요
            if risk.must_close_all:
                self.logger.warning(f"  🔴 {risk.level.value} — 전량 청산 명령")
                self._liquidate_all()
                return

            # 개별 종목 청산
            for sym in risk.must_close_symbols:
                self.logger.warning(f"  🔴 {sym} 강제 청산")
                self._close_position(sym)

            # 신규 진입 불가
            if not risk.can_open_new and decision.final_signal.name == "BUY":
                self.logger.info(f"  🟡 신규 진입 불가: {', '.join(risk.reasons)}")
                return

        # 주문 실행
        if decision.final_signal.name == "BUY":
            self._execute_buy(analysis)
        elif decision.final_signal.name == "SELL":
            self._execute_sell(analysis)

    def _execute_buy(self, analysis: dict):
        """매수 주문"""
        symbol = analysis["symbol"]
        price = analysis["current_price"]
        decision = analysis["decision"]

        # 이미 보유 중이면 스킵
        if symbol in (self.risk_manager.positions if self.risk_manager else {}):
            self.logger.info(f"  ⚪ {symbol} 이미 보유 중 — 매수 스킵")
            return

        # 포지션 사이징
        shares = 10  # 기본값
        if self.risk_manager:
            stop_distance = abs(price - decision.stop_loss_price) if decision.stop_loss_price > 0 else 0
            sizing = self.risk_manager.calculate_position_size(
                symbol, price, stop_distance=stop_distance,
            )
            shares = sizing["shares"]
            self.logger.info(
                f"  📏 사이징: {shares}주 × ${price:.2f} = "
                f"${sizing['dollar_amount']:,.2f} | {sizing['method']}"
            )

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  🔔 [ALERT] BUY {symbol} x{shares} @ ${price:.2f} | "
                f"SL=${decision.stop_loss_price:.2f} TP=${decision.take_profit_price:.2f}"
            )
            return

        # AUTO 모드: 실제 주문
        try:
            contract = analysis["contract"]
            if self.config.use_limit_orders:
                limit_price = round(price * (1 + self.config.limit_offset_pct / 100), 2)
                order = LimitOrder("BUY", shares, limit_price)
            else:
                order = MarketOrder("BUY", shares)

            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  ✅ BUY 주문 전송! {symbol} x{shares} | "
                f"주문ID: {trade.order.orderId} | 상태: {trade.orderStatus.status}"
            )

            # 리스크 매니저에 포지션 기록
            if self.risk_manager:
                self.risk_manager.open_position(
                    symbol, "LONG", price, shares,
                    stop_loss=decision.stop_loss_price,
                    take_profit=decision.take_profit_price,
                )

        except Exception as e:
            self.logger.error(f"  ❌ BUY 주문 실패: {e}")

    def _execute_sell(self, analysis: dict):
        """매도 주문 — Day Trader가 직접 매수한 포지션만 매도"""
        symbol = analysis["symbol"]
        price = analysis["current_price"]

        # ⚠️ 핵심: risk_manager에 기록된 Day Trader 자체 포지션만 매도
        # IB 전체 포트폴리오(self.positions)는 참조하지 않음
        if not self.risk_manager or symbol not in self.risk_manager.positions:
            return  # Day Trader가 산 적 없으면 무조건 스킵

        current_qty = self.risk_manager.positions[symbol].quantity
        if current_qty <= 0:
            return

        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  🔔 [ALERT] SELL {symbol} x{current_qty} @ ${price:.2f}"
            )
            return

        try:
            contract = analysis["contract"]
            order = MarketOrder("SELL", int(current_qty))
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

            self.logger.info(
                f"  ✅ SELL 주문 전송! {symbol} x{current_qty} | "
                f"주문ID: {trade.order.orderId}"
            )

            if self.risk_manager:
                self.risk_manager.close_position(symbol, price)

        except Exception as e:
            self.logger.error(f"  ❌ SELL 주문 실패: {e}")

    def _close_position(self, symbol: str):
        """특정 종목 포지션 청산 — Day Trader 자체 포지션만"""
        if not HAS_IB:
            return

        # Day Trader가 산 적 없으면 스킵
        if not self.risk_manager or symbol not in self.risk_manager.positions:
            self.logger.info(f"  ⚪ {symbol} Day Trader 포지션 아님 — 청산 스킵")
            return

        qty = self.risk_manager.positions[symbol].quantity
        if qty <= 0:
            return

        price = None
        for contract in self.watchlist:
            if contract.symbol == symbol:
                price = self.get_current_price(contract)
                if price and self.config.trade_mode == TradeMode.AUTO:
                    if qty > 0:
                        order = MarketOrder("SELL", int(qty))
                        self.ib.placeOrder(contract, order)
                        self.ib.sleep(1)
                if price and self.risk_manager:
                    self.risk_manager.close_position(symbol, price)
                break

    def _liquidate_all(self):
        """전 포지션 강제 청산"""
        self.logger.warning("  🔴🔴🔴 전 포지션 강제 청산 시작!")

        if not self.risk_manager:
            return

        symbols = list(self.risk_manager.positions.keys())
        for symbol in symbols:
            self._close_position(symbol)
            self.logger.info(f"    청산: {symbol}")

        self.logger.warning("  🔴🔴🔴 전 포지션 청산 완료")

    # ── 메인 루프 ─────────────────────────────────────────────

    def run(self, symbols: list[str] = None):
        """메인 데이 트레이딩 루프"""
        if not self.ib or not self.ib.isConnected():
            if not self.connect():
                return

        # 워치리스트 설정
        self.setup_watchlist(symbols)
        if not self.watchlist:
            self.logger.error("❌ 워치리스트 비어있음!")
            return

        # 포트폴리오 로드
        self.load_portfolio()

        # 리스크 매니저 일일 리셋
        if self.risk_manager:
            self.risk_manager.reset_daily()

        self.running = True
        self.logger.info(
            f"\n🚀 데이 트레이딩 시작! "
            f"워치리스트: {self.watchlist_symbols} | "
            f"분석주기: {self.config.analysis_interval_sec}초"
        )

        try:
            while self.running:
                # 장 상태 확인
                if not self._is_market_open():
                    self.logger.info("  💤 장 마감 — 대기 중...")
                    self.ib.sleep(60)
                    continue

                # 리스크 체크 (EOD 등)
                if self.risk_manager:
                    risk = self.risk_manager.check_risk()
                    if risk.must_close_all:
                        self._liquidate_all()
                        self.logger.info("  ⏰ EOD 청산 완료 — 종료")
                        break

                # 전 종목 분석
                self._scan_cycle()

                # 대시보드
                self.print_dashboard()

                # 대기
                self.ib.sleep(self.config.analysis_interval_sec)

        except KeyboardInterrupt:
            self.logger.info("\n⛔ 사용자 중단")
        finally:
            # 잔여 포지션 처리
            if self.risk_manager and self.risk_manager.positions:
                self.logger.warning(f"  ⚠️ 잔여 포지션 {len(self.risk_manager.positions)}개")
                if self.config.trade_mode == TradeMode.AUTO:
                    self._liquidate_all()

            self.running = False
            self.disconnect()

    def _scan_cycle(self):
        """전 종목 1회 분석 사이클"""
        self.logger.info(f"\n{'─' * 60}")
        self.logger.info(
            f"  🔄 스캔 사이클 | {datetime.now():%H:%M:%S} | "
            f"워치리스트: {len(self.watchlist_symbols)}개"
        )

        # 현재가 업데이트
        if self.risk_manager and self.risk_manager.positions:
            price_map = {}
            for contract in self.watchlist:
                sym = contract.symbol
                if sym in self.risk_manager.positions:
                    price = self.get_current_price(contract)
                    if price:
                        price_map[sym] = price
            self.risk_manager.update_prices(price_map)

        # 전 종목 분석
        for contract in self.watchlist:
            try:
                analysis = self.analyze_stock(contract)
                if analysis:
                    self.process_signal(analysis)
            except Exception as e:
                self.logger.error(f"  ❌ {contract.symbol} 분석 오류: {e}")

    def _is_market_open(self) -> bool:
        """미국 주식 시장 개장 여부"""
        try:
            from signal_bridge import is_market_open
            return is_market_open()
        except ImportError:
            pass

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.weekday() >= 5:
                return False
            market_open = now_et.replace(hour=9, minute=30, second=0)
            market_close = now_et.replace(hour=16, minute=0, second=0)
            return market_open <= now_et <= market_close
        except Exception:
            return True  # 판별 불가 시 실행

    # ── 대시보드 ──────────────────────────────────────────────

    def print_dashboard(self):
        """데이 트레이딩 대시보드"""
        now = datetime.now()

        print("\n")
        print("╔" + "═" * 68 + "╗")
        print(f"║  🏎️ Day Trader Dashboard          {now:%Y-%m-%d %H:%M:%S}  ║")
        mode_str = '🤖 AUTO' if self.config.trade_mode == TradeMode.AUTO else '🔔 ALERT'
        print(f"║  모드: {mode_str:50s}  ║")
        print("╠" + "═" * 68 + "╣")

        # 리스크 상태
        if self.risk_manager:
            status = self.risk_manager.get_status()
            icon = "🟢" if status["daily_pnl"] >= 0 else "🔴"
            line = (
                f"║  {icon} PnL: ${status['daily_pnl']:+,.2f} "
                f"({status['daily_pnl_pct']:+.2f}%) | "
                f"포지션: {status['position_count']}/{status['max_positions']} | "
                f"매매: {status['trade_count']}회"
            )
            print(f"{line:<69s}║")

            # 포지션 상세
            if self.risk_manager.positions:
                for sym, pos in self.risk_manager.positions.items():
                    pnl_icon = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
                    line = (
                        f"║    {pnl_icon} {sym:6s} x{pos.quantity:4d} "
                        f"@ ${pos.entry_price:.2f} → ${pos.current_price:.2f} "
                        f"| ${pos.unrealized_pnl:+,.2f}"
                    )
                    print(f"{line:<69s}║")

        print("╚" + "═" * 68 + "╝")

    # ── 데몬 모드 ─────────────────────────────────────────────

    def run_daemon(self):
        """매일 아침 자동 시작 데몬"""
        self.logger.info("  🕐 데몬 모드 — 매일 장 시작 시 자동 실행")

        while True:
            try:
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                now_et = datetime.now()

            # 평일 9:25 ET에 시작 준비
            if now_et.weekday() < 5 and now_et.hour == 9 and now_et.minute >= 25:
                self.logger.info(f"  🌅 장 시작 준비 ({now_et:%Y-%m-%d %H:%M})")
                self.run()
                self.logger.info("  🌙 장 종료 — 내일까지 대기")

            time.sleep(60)


# ═══════════════════════════════════════════════════════════════
#  데모 (IB 없이 로직 테스트)
# ═══════════════════════════════════════════════════════════════

def demo():
    """오프라인 데모"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🏎️ Day Trader v1.0 데모 (오프라인)                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    # 전략 데모
    try:
        from day_strategies import DayStrategyEnsemble, DayStrategyConfig
        from day_strategies import demo as strategies_demo
        strategies_demo()
    except ImportError as e:
        print(f"  ⚠️ 전략 모듈 로드 실패: {e}")

    print()

    # 리스크 데모
    try:
        from day_risk import demo as risk_demo
        risk_demo()
    except ImportError as e:
        print(f"  ⚠️ 리스크 모듈 로드 실패: {e}")

    print("\n  ✅ 데모 완료!")
    print("  실제 실행: python run.py --day [--auto]")


if __name__ == "__main__":
    demo()
