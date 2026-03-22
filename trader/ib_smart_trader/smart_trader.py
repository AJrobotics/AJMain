"""
═══════════════════════════════════════════════════════════════════
  IB Smart Trader v2.0 - Interactive Brokers 자동 매매 시스템
  
  전략 (5개 앙상블):
    1. 이동평균 크로스오버 (MA Crossover) - 단기/장기 MA 교차
    2. % 변동 기반 매매 - 설정된 % 하락/상승 시 매수/매도
    3. ATR 동적 손절/익절 - 변동성 기반 리스크 관리 [NEW]
    4. 적응형 RSI - 트렌드 맥락 기반 매매 신호 [NEW]
    5. 멀티 전략 앙상블 - 복수 전략 합의 시만 매매 [NEW]
  
  모드:
    - AUTO:  자동 매매 실행
    - ALERT: 신호만 알림 (콘솔 + 로그)
  
  연결: TWS (Trader Workstation) via ib_insync
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
from collections import deque

# ── 서드파티 임포트 ──────────────────────────────────────────────
try:
    from ib_insync import *
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║  필수 패키지가 설치되지 않았습니다.                     ║
    ║  아래 명령어로 설치해주세요:                            ║
    ║                                                          ║
    ║  pip install ib_insync pandas numpy                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    print(f"Missing: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  설정 (Config)
# ═══════════════════════════════════════════════════════════════

class TradeMode(Enum):
    AUTO = "auto"       # 자동 매매
    ALERT = "alert"     # 신호만 알림


@dataclass
class TradingConfig:
    """전체 트레이딩 설정"""
    
    # ── IB 연결 설정 ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # TWS Paper: 7497, TWS Live: 7496
    client_id: int = 1
    
    # ── 트레이딩 모드 ──
    trade_mode: TradeMode = TradeMode.ALERT  # 기본: 알림만
    
    # ── MA Crossover 전략 설정 ──
    ma_short_period: int = 10    # 단기 이동평균 (일)
    ma_long_period: int = 30     # 장기 이동평균 (일)
    
    # ── % 변동 전략 설정 ──
    buy_drop_pct: float = -5.0   # 이 % 이상 하락시 매수 신호
    sell_rise_pct: float = 5.0   # 이 % 이상 상승시 매도 신호
    pct_lookback_days: int = 5   # 며칠 전 대비 비교할지
    
    # ── 주문 설정 ──
    default_quantity: int = 10   # 기본 주문 수량
    max_position_size: int = 100 # 종목당 최대 보유 수량
    
    # ── 앙상블 모드 (v2.0 신규) ──
    use_ensemble: bool = True     # True: 5전략 앙상블, False: 기존 개별 전략
    
    # ── 모니터링 설정 ──
    check_interval_sec: int = 30       # 장중 신호 체크 주기 (초)
    check_interval_off_sec: int = 900  # 장외 신호 체크 주기 (초, 15분)
    history_bar_size: str = "1 day"
    history_duration: str = "60 D"
    
    # ── 로깅 ──
    log_file: str = "smart_trader.log"
    
    def save(self, filepath: str = "config.json"):
        """설정을 JSON으로 저장"""
        data = asdict(self)
        data["trade_mode"] = self.trade_mode.value
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  ✅ 설정 저장됨: {filepath}")
    
    @classmethod
    def load(cls, filepath: str = "config.json") -> "TradingConfig":
        """JSON에서 설정 로드"""
        if not os.path.exists(filepath):
            print(f"  ⚠️  설정 파일 없음. 기본 설정 사용.")
            return cls()
        with open(filepath, "r") as f:
            data = json.load(f)
        data["trade_mode"] = TradeMode(data.get("trade_mode", "alert"))
        return cls(**data)


# ═══════════════════════════════════════════════════════════════
#  신호 & 로그 데이터 구조
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    BUY = "🟢 BUY"
    SELL = "🔴 SELL"
    HOLD = "⚪ HOLD"


@dataclass
class TradeSignal:
    """매매 신호"""
    symbol: str
    signal: SignalType
    strategy: str           # "MA_CROSSOVER" or "PCT_CHANGE"
    price: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
    executed: bool = False
    
    def __str__(self):
        status = "✅ 실행됨" if self.executed else "⏳ 대기"
        return (
            f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] "
            f"{self.signal.value} {self.symbol} @ ${self.price:.2f} "
            f"| 전략: {self.strategy} | {self.reason} | {status}"
        )


# ═══════════════════════════════════════════════════════════════
#  기술적 분석 엔진
# ═══════════════════════════════════════════════════════════════

class TechnicalAnalyzer:
    """기술적 지표 계산"""
    
    @staticmethod
    def moving_average(prices: pd.Series, period: int) -> pd.Series:
        """단순 이동평균 (SMA) 계산"""
        return prices.rolling(window=period).mean()
    
    @staticmethod
    def check_ma_crossover(
        prices: pd.Series, 
        short_period: int, 
        long_period: int
    ) -> Optional[SignalType]:
        """
        MA 크로스오버 확인
        - 골든 크로스 (단기 > 장기): BUY
        - 데드 크로스 (단기 < 장기): SELL
        """
        if len(prices) < long_period + 2:
            return None
        
        ma_short = TechnicalAnalyzer.moving_average(prices, short_period)
        ma_long = TechnicalAnalyzer.moving_average(prices, long_period)
        
        # 현재와 이전 값 비교
        curr_short = ma_short.iloc[-1]
        prev_short = ma_short.iloc[-2]
        curr_long = ma_long.iloc[-1]
        prev_long = ma_long.iloc[-2]
        
        # NaN 체크
        if any(pd.isna([curr_short, prev_short, curr_long, prev_long])):
            return None
        
        # 골든 크로스: 단기가 장기를 아래에서 위로 돌파
        if prev_short <= prev_long and curr_short > curr_long:
            return SignalType.BUY
        
        # 데드 크로스: 단기가 장기를 위에서 아래로 돌파
        if prev_short >= prev_long and curr_short < curr_long:
            return SignalType.SELL
        
        return SignalType.HOLD
    
    @staticmethod
    def check_pct_change(
        prices: pd.Series, 
        buy_threshold: float, 
        sell_threshold: float,
        lookback: int
    ) -> tuple[Optional[SignalType], float]:
        """
        % 변동 확인
        - lookback일 전 대비 buy_threshold% 이상 하락: BUY
        - lookback일 전 대비 sell_threshold% 이상 상승: SELL
        """
        if len(prices) < lookback + 1:
            return None, 0.0
        
        current_price = prices.iloc[-1]
        past_price = prices.iloc[-(lookback + 1)]
        
        if past_price == 0:
            return None, 0.0
        
        pct_change = ((current_price - past_price) / past_price) * 100
        
        if pct_change <= buy_threshold:
            return SignalType.BUY, pct_change
        elif pct_change >= sell_threshold:
            return SignalType.SELL, pct_change
        
        return SignalType.HOLD, pct_change
    
    @staticmethod
    def get_ma_values(
        prices: pd.Series,
        short_period: int,
        long_period: int
    ) -> dict:
        """현재 MA 값 반환 (대시보드용)"""
        ma_short = TechnicalAnalyzer.moving_average(prices, short_period)
        ma_long = TechnicalAnalyzer.moving_average(prices, long_period)
        return {
            "ma_short": round(ma_short.iloc[-1], 2) if not pd.isna(ma_short.iloc[-1]) else None,
            "ma_long": round(ma_long.iloc[-1], 2) if not pd.isna(ma_long.iloc[-1]) else None,
            "spread": round(ma_short.iloc[-1] - ma_long.iloc[-1], 2) 
                      if not any(pd.isna([ma_short.iloc[-1], ma_long.iloc[-1]])) else None,
        }


# ═══════════════════════════════════════════════════════════════
#  메인 트레이딩 봇
# ═══════════════════════════════════════════════════════════════

class SmartTrader:
    """IB Smart Trader 메인 클래스"""
    
    def __init__(self, config: TradingConfig = None):
        self.config = config or TradingConfig()
        self.ib = IB()
        self.analyzer = TechnicalAnalyzer()
        self.signals_history: list[TradeSignal] = []
        self.positions: dict = {}
        self.watchlist: list[Stock] = []
        self.running = False
        
        # v2.0: 앙상블 엔진 초기화
        self.ensemble = None
        if self.config.use_ensemble:
            try:
                from advanced_strategies import (
                    StrategyEnsemble, AdvancedConfig, EnsembleDecision,
                    SignalType as AdvSignalType,
                )
                self.ensemble = StrategyEnsemble(AdvancedConfig())
                self._adv_signal_type = AdvSignalType
            except ImportError:
                print("  ⚠️  advanced_strategies.py 미발견. 기존 전략만 사용합니다.")
                self.ensemble = None
        
        # 활성 손절/익절 추적 (종목별)
        self.active_stops: dict = {}  # {symbol: {"sl": price, "tp": price, "trail_high": price}}
        
        # v2.1: 리스크 방어 시스템 초기화
        self.risk_shield = None
        try:
            from risk_shield import RiskShield, RiskShieldConfig, RiskAction
            self.risk_shield = RiskShield(RiskShieldConfig())
            self._risk_action = RiskAction
        except ImportError:
            print("  ⚠️  risk_shield.py 미발견. 리스크 방어 없이 실행합니다.")
        
        # v2.2: 세금 최적화 시스템 초기화
        self.tax_optimizer = None
        try:
            from tax_optimizer import TaxOptimizer, TaxConfig
            self.tax_optimizer = TaxOptimizer(TaxConfig())
        except ImportError:
            print("  ⚠️  tax_optimizer.py 미발견. 세금 최적화 없이 실행합니다.")
        
        # v2.3: 시그널 모니터 브리지 초기화
        self.signal_bridge = None
        try:
            from signal_bridge import SignalBridge, SignalBridgeConfig
            self.signal_bridge = SignalBridge(SignalBridgeConfig())
        except ImportError:
            print("  ⚠️  signal_bridge.py 미발견. 시그널 모니터 없이 실행합니다.")
        
        # 로깅 설정
        self._setup_logging()
    
    # ── 로깅 ──────────────────────────────────────────────────
    
    def _setup_logging(self):
        """로깅 설정"""
        self.logger = logging.getLogger("SmartTrader")
        self.logger.setLevel(logging.INFO)
        
        # 파일 핸들러
        fh = logging.FileHandler(
            self.config.log_file, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        
        # 콘솔 핸들러
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S"
        ))
        
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)
    
    # ── IB 연결 ───────────────────────────────────────────────
    
    def connect(self) -> bool:
        """IB TWS에 연결"""
        self.logger.info("=" * 60)
        self.logger.info("  IB Smart Trader 시작")
        self.logger.info(f"  모드: {self.config.trade_mode.value.upper()}")
        self.logger.info("=" * 60)
        
        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id
            )
            self.logger.info(
                f"✅ TWS 연결 성공 "
                f"({self.config.ib_host}:{self.config.ib_port})"
            )
            
            # 계좌 정보 출력
            accounts = self.ib.managedAccounts()
            self.logger.info(f"📋 계좌: {accounts}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ TWS 연결 실패: {e}")
            self.logger.error(
                "   → TWS가 실행 중인지 확인하세요.\n"
                "   → TWS > Edit > Global Config > API > Settings 에서\n"
                "     'Enable ActiveX and Socket Clients' 체크\n"
                f"   → Socket port: {self.config.ib_port}"
            )
            return False
    
    def disconnect(self):
        """IB 연결 해제"""
        if self.ib.isConnected():
            self.ib.disconnect()
            self.logger.info("🔌 TWS 연결 해제됨")
    
    # ── 포트폴리오 & 워치리스트 ────────────────────────────────
    
    def load_portfolio(self) -> dict:
        """현재 보유 포지션 로드"""
        self.logger.info("📊 포트폴리오 로딩...")
        portfolio = self.ib.portfolio()
        
        self.positions = {}
        for item in portfolio:
            symbol = item.contract.symbol
            self.positions[symbol] = {
                "contract": item.contract,
                "quantity": item.position,
                "avg_cost": item.averageCost,
                "market_value": item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "realized_pnl": item.realizedPNL,
            }
            self.logger.info(
                f"  📌 {symbol}: {item.position}주 "
                f"| 평균가: ${item.averageCost:.2f} "
                f"| 미실현 P&L: ${item.unrealizedPNL:+,.2f}"
            )
        
        if not self.positions:
            self.logger.info("  (보유 종목 없음)")
        
        return self.positions
    
    def set_watchlist(self, symbols: list[str], exchange: str = "SMART", currency: str = "USD"):
        """
        모니터링할 종목 설정
        
        예시: trader.set_watchlist(["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"])
        """
        self.watchlist = []
        self.logger.info(f"👀 워치리스트 설정: {symbols}")
        
        for sym in symbols:
            contract = Stock(sym, exchange, currency)
            self.ib.qualifyContracts(contract)
            self.watchlist.append(contract)
            self.logger.info(f"  ✅ {sym} 추가됨")
        
        return self.watchlist
    
    # ── 시세 데이터 ───────────────────────────────────────────
    
    def get_historical_prices(self, contract: Contract) -> Optional[pd.DataFrame]:
        """과거 가격 데이터 가져오기"""
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self.config.history_duration,
                barSizeSetting=self.config.history_bar_size,
                whatToShow="ADJUSTED_LAST",
                useRTH=True,        # 정규 시간만
                formatDate=1,
            )
            
            if not bars:
                self.logger.warning(
                    f"⚠️  {contract.symbol}: 히스토리 데이터 없음"
                )
                return None
            
            df = util.df(bars)
            df.set_index("date", inplace=True)
            return df
            
        except Exception as e:
            self.logger.error(
                f"❌ {contract.symbol} 히스토리 요청 실패: {e}"
            )
            return None
    
    def get_current_price(self, contract: Contract) -> Optional[float]:
        """현재 가격 가져오기"""
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self.ib.sleep(2)  # 데이터 수신 대기
            
            price = ticker.marketPrice()
            if price and not pd.isna(price):
                return float(price)
            
            # 마지막 거래가 사용
            price = ticker.last
            if price and not pd.isna(price):
                return float(price)
            
            return None
        except Exception as e:
            self.logger.error(f"❌ {contract.symbol} 시세 요청 실패: {e}")
            return None
    
    # ── 신호 분석 ─────────────────────────────────────────────
    
    def analyze_stock(self, contract: Contract) -> list[TradeSignal]:
        """단일 종목 분석 → 매매 신호 리스트 반환 (v2.0 앙상블 통합)"""
        symbol = contract.symbol
        signals = []
        
        # 히스토리 데이터
        df = self.get_historical_prices(contract)
        if df is None or len(df) < self.config.ma_long_period + 2:
            return signals
        
        close_prices = df["close"]
        current_price = close_prices.iloc[-1]
        
        # ── 기존 전략 1: MA Crossover ──
        ma_signal = self.analyzer.check_ma_crossover(
            close_prices,
            self.config.ma_short_period,
            self.config.ma_long_period
        )
        
        # ── 기존 전략 2: % 변동 ──
        pct_signal, pct_change = self.analyzer.check_pct_change(
            close_prices,
            self.config.buy_drop_pct,
            self.config.sell_rise_pct,
            self.config.pct_lookback_days,
        )
        
        # ═══ v2.0: 앙상블 모드 ═══
        if self.ensemble is not None and self.config.use_ensemble:
            return self._analyze_ensemble(
                symbol, df, close_prices, current_price,
                ma_signal, pct_signal, pct_change,
            )
        
        # ═══ 레거시 모드: 기존 개별 전략 ═══
        if ma_signal and ma_signal != SignalType.HOLD:
            ma_info = self.analyzer.get_ma_values(
                close_prices,
                self.config.ma_short_period,
                self.config.ma_long_period
            )
            cross_type = "골든 크로스 ↑" if ma_signal == SignalType.BUY else "데드 크로스 ↓"
            reason = (
                f"{cross_type} | "
                f"MA{self.config.ma_short_period}={ma_info['ma_short']} "
                f"MA{self.config.ma_long_period}={ma_info['ma_long']} "
                f"(차이: {ma_info['spread']:+.2f})"
            )
            signals.append(TradeSignal(
                symbol=symbol, signal=ma_signal,
                strategy="MA_CROSSOVER", price=current_price, reason=reason,
            ))
        
        if pct_signal and pct_signal != SignalType.HOLD:
            direction = "하락 📉" if pct_change < 0 else "상승 📈"
            reason = (
                f"{self.config.pct_lookback_days}일간 {pct_change:+.2f}% {direction} | "
                f"현재가: ${current_price:.2f}"
            )
            signals.append(TradeSignal(
                symbol=symbol, signal=pct_signal,
                strategy="PCT_CHANGE", price=current_price, reason=reason,
            ))
        
        if not signals:
            self._log_hold(symbol, close_prices, current_price, pct_change)
        
        return signals
    
    def _analyze_ensemble(
        self, symbol, df, close_prices, current_price,
        ma_signal, pct_signal, pct_change,
    ) -> list[TradeSignal]:
        """v2.0 앙상블 분석 — 5개 전략 합의 기반"""
        signals = []
        
        # 앙상블에 필요한 OHLCV
        high = df["high"] if "high" in df.columns else close_prices
        low = df["low"] if "low" in df.columns else close_prices
        volume = df["volume"] if "volume" in df.columns else pd.Series(
            [1000000] * len(close_prices), index=close_prices.index
        )
        
        # MA/PCT 신호를 앙상블 SignalType으로 변환
        adv_ma = None
        adv_pct = None
        AST = self._adv_signal_type
        
        if ma_signal == SignalType.BUY:
            adv_ma = AST.BUY
        elif ma_signal == SignalType.SELL:
            adv_ma = AST.SELL
        elif ma_signal == SignalType.HOLD:
            adv_ma = AST.HOLD
        
        if pct_signal == SignalType.BUY:
            adv_pct = AST.BUY
        elif pct_signal == SignalType.SELL:
            adv_pct = AST.SELL
        elif pct_signal == SignalType.HOLD:
            adv_pct = AST.HOLD
        
        # 앙상블 실행!
        decision = self.ensemble.analyze(
            symbol=symbol,
            close=close_prices,
            high=high,
            low=low,
            volume=volume,
            ma_signal=adv_ma,
            pct_signal=adv_pct,
            pct_change=pct_change,
        )
        
        # 앙상블 결과 로깅
        buy_count = sum(1 for s in decision.individual_signals if s.signal == AST.BUY)
        sell_count = sum(1 for s in decision.individual_signals if s.signal == AST.SELL)
        hold_count = sum(1 for s in decision.individual_signals if s.signal == AST.HOLD)
        
        # ── v2.3: Signal Bridge — 6번째 전략 + 부스트 ──
        bridge_signal_str = ""
        if self.signal_bridge is not None:
            # 6번째 전략 신호
            bridge_sig = self.signal_bridge.get_ensemble_strategy_signal()
            bridge_signal_str = bridge_sig["signal"]
            
            # 앙상블 부스트 적용
            boost = self.signal_bridge.get_ensemble_boost()
            if boost != 0:
                decision.consensus_score += boost
                self.logger.info(
                    f"  📡 Signal Monitor: {bridge_sig['signal']} "
                    f"(신뢰도: {bridge_sig['confidence']:.0%}) | "
                    f"부스트: {boost:+.2f} → 합의: {decision.consensus_score:+.3f}"
                )
            else:
                self.logger.info(
                    f"  📡 Signal Monitor: {bridge_sig['signal']} (NEUTRAL)"
                )
            
            if bridge_sig["signal"] == "BUY":
                buy_count += 1
            elif bridge_sig["signal"] == "SELL":
                sell_count += 1
            else:
                hold_count += 1
        
        self.logger.info(
            f"  🎯 {symbol} 앙상블 | "
            f"합의: {decision.consensus_score:+.3f} | "
            f"BUY:{buy_count} SELL:{sell_count} HOLD:{hold_count}"
        )
        
        for sig in decision.individual_signals:
            self.logger.info(
                f"      {sig.strategy_name:18s} → {sig.signal.name:4s} "
                f"({sig.confidence:.0%}) {sig.reason}"
            )
        if bridge_signal_str:
            self.logger.info(
                f"      {'SIGNAL_MONITOR':18s} → {bridge_signal_str:4s} (6번째 전략)"
            )
        
        # 앙상블 결정 → TradeSignal 변환
        if decision.final_signal == AST.BUY:
            final_sig = SignalType.BUY
        elif decision.final_signal == AST.SELL:
            final_sig = SignalType.SELL
        else:
            final_sig = SignalType.HOLD
        
        if final_sig != SignalType.HOLD:
            # ── v2.1: Risk Shield 체크 (BUY 전에만) ──
            if final_sig == SignalType.BUY and self.risk_shield is not None:
                current_holdings = list(self.positions.keys())
                risk_result = self.risk_shield.full_check(symbol, current_holdings)
                
                if risk_result.action == self._risk_action.BLOCK:
                    self.logger.info(
                        f"  🛡️ RISK SHIELD 차단! {symbol} 매수 거부"
                    )
                    for reason in risk_result.reasons:
                        self.logger.info(f"      → {reason}")
                    
                    # BUY를 HOLD로 전환
                    final_sig = SignalType.HOLD
                    self.logger.info(
                        f"  ⚪ {symbol} HOLD (리스크 방어) | "
                        f"Beta: {risk_result.beta:.2f} | "
                        f"미스: {risk_result.earnings_miss_count}/4"
                    )
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals
                
                elif risk_result.action == self._risk_action.REDUCE:
                    self.logger.info(
                        f"  ⚠️ RISK SHIELD 경고: {symbol} 포지션 축소 권고"
                    )
                    for reason in risk_result.reasons:
                        self.logger.info(f"      → {reason}")
                
                # Beta 조정 투자금 로깅
                if risk_result.beta != 1.0:
                    self.logger.info(
                        f"  📏 Beta 조정: {symbol} Beta={risk_result.beta:.2f} → "
                        f"투자금 ${risk_result.adjusted_investment:,.0f} "
                        f"(기본 대비 {risk_result.adjusted_investment/10000:.0%})"
                    )
            
            # ── v2.3: Signal Bridge — BEAR 브레이크 (BUY 전) ──
            if final_sig == SignalType.BUY and self.signal_bridge is not None:
                blocked, brake_reason = self.signal_bridge.should_block_buy()
                if blocked:
                    self.logger.info(f"  {brake_reason}")
                    final_sig = SignalType.HOLD
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals
            
            # ── v2.3: Signal Bridge — Washout 방지 ──
            if final_sig != SignalType.HOLD and self.signal_bridge is not None:
                ma_vals = self.analyzer.get_ma_values(
                    close_prices,
                    self.config.ma_short_period,
                    self.config.ma_long_period
                )
                ma_short = ma_vals.get("ma_short", 0) if ma_vals else 0
                ma_long = ma_vals.get("ma_long", 0) if ma_vals else 0
                
                washout = self.signal_bridge.check_washout(
                    symbol, ma_short, ma_long, current_price
                )
                if not washout["allowed"]:
                    self.logger.info(
                        f"  🔄 Washout 차단! {symbol} | {washout['reason']}"
                    )
                    final_sig = SignalType.HOLD
                    self._check_stop_levels(symbol, current_price, signals)
                    return signals
            
            # 손절/익절 저장
            self.active_stops[symbol] = {
                "sl": decision.stop_loss_price,
                "tp": decision.take_profit_price,
                "trail_high": current_price,
                "atr": decision.atr_value,
            }
            
            signals.append(TradeSignal(
                symbol=symbol,
                signal=final_sig,
                strategy="ENSEMBLE",
                price=current_price,
                reason=decision.reason,
            ))
            
            self.logger.info(
                f"  🛡️ 리스크: SL=${decision.stop_loss_price:.2f} | "
                f"TP=${decision.take_profit_price:.2f} | "
                f"ATR=${decision.atr_value:.2f}"
            )
        else:
            self.logger.info(
                f"  ⚪ {symbol} HOLD (앙상블 합의 미달) | "
                f"${current_price:.2f} | {decision.reason}"
            )
        
        # 기존 포지션 손절/익절 체크
        self._check_stop_levels(symbol, current_price, signals)
        
        return signals
    
    def _check_stop_levels(self, symbol: str, current_price: float, signals: list):
        """활성 포지션의 손절/익절 도달 여부 확인"""
        if symbol not in self.active_stops:
            return
        
        stops = self.active_stops[symbol]
        sl = stops.get("sl", 0)
        tp = stops.get("tp", 0)
        trail_high = stops.get("trail_high", current_price)
        
        # 트레일링 스탑 업데이트
        if current_price > trail_high:
            stops["trail_high"] = current_price
            # 트레일링 SL도 따라 올림
            atr = stops.get("atr", 0)
            if atr > 0:
                new_sl = current_price - atr * 1.5
                if new_sl > sl:
                    stops["sl"] = new_sl
                    self.logger.info(
                        f"  📈 {symbol} 트레일링 SL 업데이트: "
                        f"${sl:.2f} → ${new_sl:.2f}"
                    )
        
        # 손절 도달
        if sl > 0 and current_price <= sl:
            signals.append(TradeSignal(
                symbol=symbol,
                signal=SignalType.SELL,
                strategy="ATR_STOP_LOSS",
                price=current_price,
                reason=f"🛑 손절 도달! ${current_price:.2f} ≤ SL ${sl:.2f}",
            ))
            del self.active_stops[symbol]
        
        # 익절 도달
        elif tp > 0 and current_price >= tp:
            signals.append(TradeSignal(
                symbol=symbol,
                signal=SignalType.SELL,
                strategy="ATR_TAKE_PROFIT",
                price=current_price,
                reason=f"🎯 익절 도달! ${current_price:.2f} ≥ TP ${tp:.2f}",
            ))
            del self.active_stops[symbol]
    
    def _log_hold(self, symbol, close_prices, current_price, pct_change):
        """HOLD 상태 로깅"""
        ma_info = self.analyzer.get_ma_values(
            close_prices,
            self.config.ma_short_period,
            self.config.ma_long_period
        )
        self.logger.info(
            f"  ⚪ {symbol} HOLD | "
            f"${current_price:.2f} | "
            f"MA스프레드: {ma_info.get('spread', 'N/A')} | "
            f"{self.config.pct_lookback_days}일 변동: {pct_change:+.2f}%"
        )
    
    # ── 주문 실행 ─────────────────────────────────────────────
    
    def execute_signal(self, signal: TradeSignal) -> bool:
        """
        신호에 따른 주문 실행
        - AUTO 모드: 실제 주문 전송
        - ALERT 모드: 로그만 기록
        """
        self.logger.info(f"  📡 신호 감지: {signal}")
        self.signals_history.append(signal)
        
        # ALERT 모드 → 주문 실행 안함
        if self.config.trade_mode == TradeMode.ALERT:
            self.logger.info(
                f"  ℹ️  [ALERT 모드] 주문 실행 안 함. "
                f"자동 매매를 원하시면 trade_mode='auto'로 변경하세요."
            )
            return False
        
        # AUTO 모드 → 실제 주문
        try:
            contract = Stock(signal.symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            
            # 포지션 크기 확인
            current_qty = self.positions.get(signal.symbol, {}).get("quantity", 0)
            
            if signal.signal == SignalType.BUY:
                # v2.2: Wash Sale 체크
                if self.tax_optimizer is not None:
                    wash = self.tax_optimizer.check_buy_allowed(signal.symbol)
                    if wash.get("blocked"):
                        self.logger.warning(
                            f"  🚫 Wash Sale 차단! {signal.symbol} 매수 불가 | "
                            f"{wash.get('reason', '')}"
                        )
                        return False
                    if wash.get("warning"):
                        self.logger.info(f"  ⚠️ {wash.get('reason', '')}")
                
                # 최대 보유량 초과 체크
                if current_qty + self.config.default_quantity > self.config.max_position_size:
                    self.logger.warning(
                        f"  ⚠️  {signal.symbol}: 최대 보유량 "
                        f"({self.config.max_position_size}) 초과! 주문 스킵"
                    )
                    return False
                
                order = MarketOrder("BUY", self.config.default_quantity)
                
            elif signal.signal == SignalType.SELL:
                # 보유량이 없으면 스킵
                if current_qty <= 0:
                    self.logger.warning(
                        f"  ⚠️  {signal.symbol}: 보유 수량 없음! 매도 스킵"
                    )
                    return False
                
                sell_qty = min(self.config.default_quantity, int(current_qty))
                order = MarketOrder("SELL", sell_qty)
            else:
                return False
            
            # 주문 전송
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)
            
            self.logger.info(
                f"  ✅ 주문 전송! {signal.signal.value} "
                f"{signal.symbol} x{order.totalQuantity} "
                f"| 주문 ID: {trade.order.orderId} "
                f"| 상태: {trade.orderStatus.status}"
            )
            
            # v2.2: 세금 기록
            if self.tax_optimizer is not None:
                if signal.signal == SignalType.BUY:
                    self.tax_optimizer.on_buy(
                        signal.symbol, signal.price, int(order.totalQuantity)
                    )
                elif signal.signal == SignalType.SELL:
                    tax_result = self.tax_optimizer.on_sell(
                        signal.symbol, signal.price, int(order.totalQuantity)
                    )
                    if tax_result.get("wash_sale_started"):
                        self.logger.info(
                            f"  ⏰ {signal.symbol} Wash Sale 30일 카운트다운 시작"
                        )
            
            # v2.3: Washout cooldown 기록
            if self.signal_bridge is not None:
                self.signal_bridge.record_trade(signal.symbol)
            
            signal.executed = True
            return True
            
        except Exception as e:
            self.logger.error(f"  ❌ 주문 실행 실패: {e}")
            return False
    
    # ── 대시보드 ──────────────────────────────────────────────
    
    def print_dashboard(self):
        """현재 상태 대시보드 출력"""
        now = datetime.now()
        
        print("\n")
        print("╔" + "═" * 68 + "╗")
        print(f"║  📊 IB Smart Trader Dashboard       {now:%Y-%m-%d %H:%M:%S}  ║")
        print(f"║  모드: {'🤖 AUTO (자동매매)' if self.config.trade_mode == TradeMode.AUTO else '🔔 ALERT (알림만)':42s}  ║")
        print("╠" + "═" * 68 + "╣")
        
        # 전략 설정
        print(f"║  📈 MA Crossover: MA{self.config.ma_short_period} / MA{self.config.ma_long_period}" + " " * 35 + "║")
        print(f"║  📉 % 변동: 매수 ≤ {self.config.buy_drop_pct}% | 매도 ≥ +{self.config.sell_rise_pct}% ({self.config.pct_lookback_days}일)" + " " * 11 + "║")
        print("╠" + "═" * 68 + "╣")
        
        # 보유 포지션
        print("║  💼 보유 포지션:" + " " * 51 + "║")
        if self.positions:
            for sym, pos in self.positions.items():
                pnl = pos.get("unrealized_pnl", 0)
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                line = (
                    f"║    {pnl_icon} {sym:6s} | "
                    f"{int(pos['quantity']):4d}주 | "
                    f"평균: ${pos['avg_cost']:8.2f} | "
                    f"P&L: ${pnl:+10,.2f}"
                )
                print(f"{line:<69s}║")
        else:
            print("║    (없음)" + " " * 58 + "║")
        
        print("╠" + "═" * 68 + "╣")
        
        # v2.3: Signal Monitor 상태
        if self.signal_bridge is not None:
            sig = self.signal_bridge.get_composite_signal()
            icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
            brake_str = ""
            blocked, _ = self.signal_bridge.should_block_buy()
            if blocked:
                brake_str = " | 🛑 매수중단"
            line = (
                f"║  📡 Signal Monitor: {icon.get(sig.composite, '⚪')} {sig.composite} "
                f"({sig.confidence:.0%}) | P/C: {sig.pc_ratio:.2f}{brake_str}"
            )
            print(f"{line:<69s}║")
            line2 = (
                f"║    COT: {sig.cot_signal} | 옵션: {sig.options_signal} | "
                f"부스트: {self.signal_bridge.get_ensemble_boost():+.2f}"
            )
            print(f"{line2:<69s}║")
        
        print("╠" + "═" * 68 + "╣")
        
        # 최근 신호
        print("║  📡 최근 신호 (최대 10개):" + " " * 41 + "║")
        recent = self.signals_history[-10:] if self.signals_history else []
        if recent:
            for sig in recent:
                status = "✅" if sig.executed else "⏳"
                line = (
                    f"║    {status} {sig.signal.value} {sig.symbol:6s} "
                    f"${sig.price:8.2f} | {sig.strategy:12s} "
                    f"| {sig.timestamp:%H:%M}"
                )
                print(f"{line:<69s}║")
        else:
            print("║    (신호 없음)" + " " * 53 + "║")
        
        print("╚" + "═" * 68 + "╝")
    
    # ── 메인 루프 ─────────────────────────────────────────────
    
    def run(self, symbols: list[str] = None):
        """
        메인 모니터링 루프 실행
        
        사용법:
            trader = SmartTrader(config)
            trader.connect()
            trader.run(["AAPL", "MSFT", "GOOGL", "TSLA"])
        """
        if not self.ib.isConnected():
            if not self.connect():
                return
        
        # 워치리스트 설정
        if symbols:
            self.set_watchlist(symbols)
        
        if not self.watchlist:
            self.logger.error("❌ 워치리스트가 비어있습니다!")
            return
        
        # 포트폴리오 로드
        self.load_portfolio()
        
        self.running = True
        self.logger.info(
            f"\n🚀 모니터링 시작! "
            f"({len(self.watchlist)}종목, "
            f"{self.config.check_interval_sec}초 간격)\n"
            f"   Ctrl+C로 중지\n"
        )
        
        cycle = 0
        try:
            while self.running:
                cycle += 1
                self.logger.info(f"\n{'─' * 50}")
                self.logger.info(f"🔄 사이클 #{cycle} 시작 [{datetime.now():%H:%M:%S}]")
                self.logger.info(f"{'─' * 50}")
                
                # 포지션 갱신
                self.load_portfolio()
                
                # 각 종목 분석
                all_signals = []
                for contract in self.watchlist:
                    self.logger.info(f"\n  🔍 분석 중: {contract.symbol}")
                    signals = self.analyze_stock(contract)
                    all_signals.extend(signals)
                    
                    # 신호 실행
                    for signal in signals:
                        self.execute_signal(signal)
                    
                    # API 속도 제한 방지
                    self.ib.sleep(1)
                
                # 대시보드 출력
                self.print_dashboard()
                
                # 다음 사이클까지 대기 (장중/장외 자동 조절)
                from signal_bridge import is_market_open
                if is_market_open():
                    interval = self.config.check_interval_sec
                    label = "장중"
                else:
                    interval = self.config.check_interval_off_sec
                    label = "장외"
                self.logger.info(
                    f"\n⏰ 다음 체크: {interval}초 후 ({label})..."
                )
                self.ib.sleep(interval)
                
        except KeyboardInterrupt:
            self.logger.info("\n\n🛑 사용자에 의해 중지됨")
        except Exception as e:
            self.logger.error(f"\n❌ 에러 발생: {e}", exc_info=True)
        finally:
            self.stop()
    
    def stop(self):
        """봇 중지 & 정리"""
        self.running = False
        self.print_dashboard()
        self.disconnect()
        
        # 신호 히스토리 저장
        if self.signals_history:
            history_file = f"signals_{datetime.now():%Y%m%d_%H%M%S}.json"
            history_data = [
                {
                    "symbol": s.symbol,
                    "signal": s.signal.name,
                    "strategy": s.strategy,
                    "price": s.price,
                    "reason": s.reason,
                    "timestamp": s.timestamp.isoformat(),
                    "executed": s.executed,
                }
                for s in self.signals_history
            ]
            with open(history_file, "w") as f:
                json.dump(history_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"📁 신호 히스토리 저장됨: {history_file}")
        
        self.logger.info("👋 Smart Trader 종료")


# ═══════════════════════════════════════════════════════════════
#  실행
# ═══════════════════════════════════════════════════════════════

def main():
    """메인 실행 함수"""
    
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║           🤖 IB Smart Trader v1.0                       ║
    ║                                                          ║
    ║  Interactive Brokers 자동 매매 시스템                    ║
    ║  전략: MA Crossover + % 변동 기반                       ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    # ── 설정 로드 또는 생성 ──
    config = TradingConfig(
        # IB 연결
        ib_host="127.0.0.1",
        ib_port=7497,            # Paper Trading (Live: 7496)
        client_id=1,
        
        # 모드 선택 ─ 처음에는 ALERT로 테스트 추천!
        trade_mode=TradeMode.ALERT,
        
        # MA Crossover 설정
        ma_short_period=10,      # 10일 이동평균
        ma_long_period=30,       # 30일 이동평균
        
        # % 변동 설정
        buy_drop_pct=-5.0,       # 5% 하락시 매수
        sell_rise_pct=5.0,       # 5% 상승시 매도
        pct_lookback_days=5,     # 5일 전 대비
        
        # 주문 설정
        default_quantity=10,     # 기본 10주
        max_position_size=100,   # 최대 100주
        
        # 모니터링
        check_interval_sec=60,   # 60초마다 체크
    )
    
    # 설정 저장
    config.save("config.json")
    
    # ── 모니터링할 종목 ──
    watchlist = [
        "AAPL",    # Apple
        "MSFT",    # Microsoft
        "GOOGL",   # Alphabet
        "TSLA",    # Tesla
        "AMZN",    # Amazon
        "NVDA",    # NVIDIA
        "META",    # Meta
    ]
    
    # ── 트레이더 실행 ──
    trader = SmartTrader(config)
    
    if trader.connect():
        trader.run(watchlist)
    else:
        print("\n  TWS 연결에 실패했습니다. 위의 안내를 참고하세요.")


if __name__ == "__main__":
    main()
