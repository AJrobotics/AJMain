"""
═══════════════════════════════════════════════════════════════════
  Auto Stock Screener - 자동 종목 스크리닝 & 일일 리밸런싱 모듈
  
  기능:
    1. 매일 장 마감 후 전체 유니버스 스캔
    2. 모멘텀 + 거래량 + 기술적 지표 기반 TOP 10 선별
    3. 섹터 로테이션 감지 (에너지, 방위, AI 등)
    4. Smart Trader와 연동하여 자동 매매
    5. 일일 리포트 생성 & 로깅
  
  사용법:
    python auto_screener.py          # 1회 스크리닝
    python auto_screener.py --daemon  # 매일 자동 실행
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
    ║  필수 패키지 설치 필요:                                 ║
    ║  pip install ib_insync pandas numpy                      ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  스크리닝 설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class ScreenerConfig:
    """스크리너 설정"""
    
    # ── IB 연결 ──
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    client_id: int = 2          # Smart Trader와 다른 ID 사용
    
    # ── 스크리닝 유니버스 ──
    # 대형주 + 중형주 + 섹터 ETF 기반 스크리닝 풀
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
    
    # ── 스크리닝 기준 ──
    top_picks: int = 10              # 최종 선별 종목 수
    min_avg_volume: int = 500000     # 최소 평균 거래량
    lookback_days: str = "30 D"      # 히스토리 기간
    bar_size: str = "1 day"          # 봉 크기
    
    # ── 가중치 (총합 = 1.0) ──
    weight_momentum_5d: float = 0.25   # 5일 모멘텀
    weight_momentum_10d: float = 0.20  # 10일 모멘텀
    weight_volume_surge: float = 0.15  # 거래량 급증
    weight_ma_trend: float = 0.20      # MA 트렌드 (가격 > MA)
    weight_volatility: float = 0.10    # 변동성 (적절한 수준 선호)
    weight_rsi_zone: float = 0.10      # RSI 구간 점수
    
    # ── 투자 설정 ──
    investment_per_stock: float = 10000.0  # 종목당 투자금
    max_total_investment: float = 100000.0 # 최대 총 투자금
    
    # ── 스케줄 ──
    run_time_hour: int = 16        # 실행 시각 (16 = 오후 4시, 장 마감 후)
    run_time_minute: int = 30
    
    # ── 파일 ──
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
#  종목 점수 계산
# ═══════════════════════════════════════════════════════════════

@dataclass
class StockScore:
    """개별 종목 분석 결과"""
    symbol: str
    name: str = ""
    sector: str = ""
    
    # 가격 정보
    current_price: float = 0.0
    price_5d_ago: float = 0.0
    price_10d_ago: float = 0.0
    
    # 개별 점수 (0~100)
    momentum_5d_score: float = 0.0
    momentum_10d_score: float = 0.0
    volume_surge_score: float = 0.0
    ma_trend_score: float = 0.0
    volatility_score: float = 0.0
    rsi_score: float = 0.0
    
    # 종합 점수
    total_score: float = 0.0
    
    # 추가 정보
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
    """종목 분석 & 점수 계산 엔진"""
    
    @staticmethod
    def calc_rsi(prices: pd.Series, period: int = 14) -> float:
        """RSI (Relative Strength Index) 계산"""
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
        """모멘텀 점수화 (0~100)"""
        # 강한 상승 = 높은 점수, 하지만 과도한 상승은 감점
        if pct_change > 20:
            return 70     # 과매수 가능성
        elif pct_change > 10:
            return 90
        elif pct_change > 5:
            return 100
        elif pct_change > 2:
            return 85
        elif pct_change > 0:
            return 70
        elif pct_change > -3:
            return 50     # 소폭 하락 = 매수 기회일 수도
        elif pct_change > -5:
            return 60     # 딥 매수 기회
        elif pct_change > -10:
            return 40
        else:
            return 20     # 급락 = 위험
    
    @staticmethod
    def score_volume(volume_ratio: float) -> float:
        """거래량 비율 점수 (최근 5일 / 30일 평균)"""
        if volume_ratio > 3.0:
            return 100    # 폭발적 관심
        elif volume_ratio > 2.0:
            return 90
        elif volume_ratio > 1.5:
            return 80
        elif volume_ratio > 1.2:
            return 70
        elif volume_ratio > 0.8:
            return 50     # 보통
        else:
            return 30     # 관심 감소
    
    @staticmethod
    def score_ma_trend(price: float, ma_10: float, ma_30: float) -> float:
        """이동평균 트렌드 점수"""
        score = 50
        if price > ma_10:
            score += 20
        if price > ma_30:
            score += 15
        if ma_10 > ma_30:
            score += 15   # 골든 크로스 구조
        return min(score, 100)
    
    @staticmethod
    def score_volatility(daily_returns_std: float) -> float:
        """변동성 점수 (적절한 변동성 선호)"""
        # 너무 낮으면 모멘텀 없음, 너무 높으면 위험
        if daily_returns_std < 0.005:
            return 30
        elif daily_returns_std < 0.01:
            return 50
        elif daily_returns_std < 0.02:
            return 80
        elif daily_returns_std < 0.03:
            return 90     # 적절한 변동성
        elif daily_returns_std < 0.05:
            return 70
        else:
            return 40     # 과도한 변동성
    
    @staticmethod
    def score_rsi(rsi: float) -> float:
        """RSI 구간 점수"""
        if rsi < 30:
            return 90     # 과매도 → 반등 기대
        elif rsi < 40:
            return 80
        elif rsi < 50:
            return 65
        elif rsi < 60:
            return 70     # 중립~강세 초입
        elif rsi < 70:
            return 75     # 강세 구간
        elif rsi < 80:
            return 50     # 과매수 근접
        else:
            return 25     # 과매수 → 조정 위험


# ═══════════════════════════════════════════════════════════════
#  메인 스크리너
# ═══════════════════════════════════════════════════════════════

class AutoScreener:
    """자동 종목 스크리닝 엔진"""
    
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
    
    # ── IB 연결 ───────────────────────────────────────────────
    
    def connect(self) -> bool:
        try:
            self.ib.connect(
                self.config.ib_host,
                self.config.ib_port,
                clientId=self.config.client_id
            )
            self.logger.info("✅ TWS 연결 성공 (Screener)")
            return True
        except Exception as e:
            self.logger.error(f"❌ TWS 연결 실패: {e}")
            return False
    
    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
    
    # ── 데이터 수집 ───────────────────────────────────────────
    
    def fetch_stock_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """종목 히스토리 데이터 가져오기"""
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
            self.logger.warning(f"  ⚠️ {symbol} 데이터 실패: {e}")
            return None
    
    # ── 개별 종목 분석 ────────────────────────────────────────
    
    def analyze_stock(self, symbol: str) -> Optional[StockScore]:
        """단일 종목 분석 → 점수 반환"""
        df = self.fetch_stock_data(symbol)
        if df is None:
            return None
        
        close = df["close"]
        volume = df["volume"]
        
        score = StockScore(symbol=symbol)
        score.timestamp = datetime.now().isoformat()
        
        # 가격 정보
        score.current_price = float(close.iloc[-1])
        score.price_5d_ago = float(close.iloc[-6]) if len(close) >= 6 else score.current_price
        score.price_10d_ago = float(close.iloc[-11]) if len(close) >= 11 else score.current_price
        
        # ── 1. 모멘텀 (5일) ──
        score.momentum_5d_pct = (
            (score.current_price - score.price_5d_ago) / score.price_5d_ago * 100
        )
        score.momentum_5d_score = self.analyzer.score_momentum(score.momentum_5d_pct)
        
        # ── 2. 모멘텀 (10일) ──
        score.momentum_10d_pct = (
            (score.current_price - score.price_10d_ago) / score.price_10d_ago * 100
        )
        score.momentum_10d_score = self.analyzer.score_momentum(score.momentum_10d_pct)
        
        # ── 3. 거래량 급증 ──
        score.avg_volume = float(volume.iloc[-30:].mean()) if len(volume) >= 30 else float(volume.mean())
        score.recent_volume = float(volume.iloc[-5:].mean())
        score.volume_ratio = (
            score.recent_volume / score.avg_volume 
            if score.avg_volume > 0 else 1.0
        )
        score.volume_surge_score = self.analyzer.score_volume(score.volume_ratio)
        
        # 최소 거래량 필터
        if score.avg_volume < self.config.min_avg_volume:
            return None
        
        # ── 4. MA 트렌드 ──
        score.ma_10 = float(close.rolling(10).mean().iloc[-1])
        score.ma_30 = float(close.rolling(min(30, len(close))).mean().iloc[-1])
        score.ma_trend_score = self.analyzer.score_ma_trend(
            score.current_price, score.ma_10, score.ma_30
        )
        
        # ── 5. 변동성 ──
        daily_returns = close.pct_change().dropna()
        volatility = float(daily_returns.std()) if len(daily_returns) > 5 else 0.02
        score.volatility_score = self.analyzer.score_volatility(volatility)
        
        # ── 6. RSI ──
        score.rsi = self.analyzer.calc_rsi(close)
        score.rsi_score = self.analyzer.score_rsi(score.rsi)
        
        # ── 종합 점수 계산 ──
        cfg = self.config
        score.total_score = (
            score.momentum_5d_score * cfg.weight_momentum_5d +
            score.momentum_10d_score * cfg.weight_momentum_10d +
            score.volume_surge_score * cfg.weight_volume_surge +
            score.ma_trend_score * cfg.weight_ma_trend +
            score.volatility_score * cfg.weight_volatility +
            score.rsi_score * cfg.weight_rsi_zone
        )
        
        # ── 신호 결정 ──
        if score.total_score >= 75:
            score.signal = "🟢 BUY"
            score.reason = self._generate_reason(score)
        elif score.total_score >= 55:
            score.signal = "🟡 WATCH"
            score.reason = "관망 - 추가 확인 필요"
        else:
            score.signal = "🔴 AVOID"
            score.reason = "부정적 지표"
        
        return score
    
    def _generate_reason(self, score: StockScore) -> str:
        """매수 사유 생성"""
        reasons = []
        if score.momentum_5d_pct > 3:
            reasons.append(f"5일 +{score.momentum_5d_pct:.1f}% 상승")
        if score.momentum_5d_pct < -3:
            reasons.append(f"5일 {score.momentum_5d_pct:.1f}% 하락 (딥매수)")
        if score.volume_ratio > 1.5:
            reasons.append(f"거래량 {score.volume_ratio:.1f}배 급증")
        if score.current_price > score.ma_10 > score.ma_30:
            reasons.append("골든크로스 구조")
        if score.rsi < 40:
            reasons.append(f"RSI {score.rsi:.0f} 과매도")
        elif 50 < score.rsi < 70:
            reasons.append(f"RSI {score.rsi:.0f} 강세 구간")
        
        return " | ".join(reasons) if reasons else "종합 점수 우수"
    
    # ── 전체 스크리닝 실행 ────────────────────────────────────
    
    def run_screening(self) -> list[StockScore]:
        """전체 유니버스 스캔 → TOP N 선별"""
        self.logger.info("\n" + "═" * 60)
        self.logger.info(f"  🔍 자동 스크리닝 시작 [{datetime.now():%Y-%m-%d %H:%M}]")
        self.logger.info(f"  유니버스: {len(self.config.universe)}종목")
        self.logger.info("═" * 60)
        
        self.scores = []
        total = len(self.config.universe)
        
        for i, symbol in enumerate(self.config.universe):
            self.logger.info(
                f"  [{i+1}/{total}] 분석 중: {symbol}..."
            )
            
            score = self.analyze_stock(symbol)
            if score:
                self.scores.append(score)
                self.logger.info(
                    f"    → 점수: {score.total_score:.1f} | "
                    f"5일: {score.momentum_5d_pct:+.1f}% | "
                    f"RSI: {score.rsi:.0f} | "
                    f"볼륨: {score.volume_ratio:.1f}x | "
                    f"{score.signal}"
                )
            
            # API 속도 제한 방지
            self.ib.sleep(0.5)
        
        # 점수 기준 정렬
        self.scores.sort(key=lambda s: s.total_score, reverse=True)
        
        # TOP N 선별
        self.picks = self.scores[:self.config.top_picks]
        
        self.logger.info(f"\n{'─' * 60}")
        self.logger.info(f"  📊 스크리닝 완료! 분석: {len(self.scores)}종목")
        self.logger.info(f"  🏆 TOP {self.config.top_picks} 선별:")
        self.logger.info(f"{'─' * 60}")
        
        for i, pick in enumerate(self.picks):
            self.logger.info(
                f"  #{i+1:2d} {pick.symbol:6s} | "
                f"점수: {pick.total_score:5.1f} | "
                f"${pick.current_price:8.2f} | "
                f"5일: {pick.momentum_5d_pct:+6.1f}% | "
                f"RSI: {pick.rsi:5.1f} | "
                f"{pick.signal} | {pick.reason}"
            )
        
        # 결과 저장
        self._save_picks()
        self._save_report()
        self._save_history()
        
        return self.picks
    
    # ── 결과 저장 ─────────────────────────────────────────────
    
    def _save_picks(self):
        """오늘의 추천 종목 저장"""
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
                for s in self.scores[:30]  # 상위 30개만
            ],
        }
        
        with open(self.config.picks_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"  💾 추천 종목 저장: {self.config.picks_file}")
    
    def _save_report(self):
        """일일 리포트 생성"""
        date_str = datetime.now().strftime("%Y%m%d")
        report_path = os.path.join(
            self.config.report_dir, f"report_{date_str}.txt"
        )
        
        lines = []
        lines.append("=" * 70)
        lines.append(f"  📊 IB Smart Trader - 일일 스크리닝 리포트")
        lines.append(f"  날짜: {datetime.now():%Y-%m-%d %H:%M}")
        lines.append(f"  분석 종목: {len(self.scores)}개")
        lines.append("=" * 70)
        lines.append("")
        
        lines.append("  🏆 오늘의 TOP 10 추천 종목:")
        lines.append("─" * 70)
        lines.append(
            f"  {'#':>3s} {'종목':6s} {'점수':>6s} {'현재가':>10s} "
            f"{'5일%':>7s} {'10일%':>7s} {'RSI':>5s} {'볼륨':>5s} 신호"
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
        lines.append(f"  투자 계획: 종목당 ${self.config.investment_per_stock:,.0f}")
        lines.append(
            f"  총 투자금: ${self.config.investment_per_stock * len(self.picks):,.0f}"
        )
        lines.append("")
        
        # 섹터 분석
        lines.append("  📈 섹터 강도 분석:")
        sector_scores = {}
        for s in self.scores:
            # 간단한 섹터 분류
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
        
        # 콘솔에도 출력
        print(report_text)
        self.logger.info(f"  📄 리포트 저장: {report_path}")
    
    def _save_history(self):
        """스크리닝 히스토리 누적"""
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
        
        # 최근 90일만 보관
        history = history[-90:]
        
        with open(self.config.history_file, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    
    def _classify_sector(self, symbol: str) -> str:
        """간단한 섹터 분류"""
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
        return "기타"
    
    # ── Smart Trader 연동 ─────────────────────────────────────
    
    def get_watchlist_for_trader(self) -> list[str]:
        """Smart Trader에 전달할 워치리스트"""
        return [p.symbol for p in self.picks]
    
    def get_picks_with_allocation(self) -> list[dict]:
        """투자 금액 배분 포함한 추천 목록"""
        total_score = sum(p.total_score for p in self.picks)
        result = []
        
        for pick in self.picks:
            # 점수 비례 배분 (가중 투자)
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
    
    # ── 전일 성과 평가 ────────────────────────────────────────
    
    def evaluate_previous_picks(self) -> Optional[dict]:
        """전일 추천 종목의 실제 성과 평가"""
        if not os.path.exists(self.config.picks_file):
            return None
        
        with open(self.config.picks_file, "r") as f:
            prev_data = json.load(f)
        
        prev_date = prev_data.get("date", "")
        today = datetime.now().strftime("%Y-%m-%d")
        
        if prev_date == today:
            self.logger.info("  ℹ️  오늘 이미 스크리닝됨. 전일 평가 스킵.")
            return None
        
        self.logger.info(f"\n📋 전일({prev_date}) 추천 종목 성과 평가:")
        self.logger.info("─" * 60)
        
        results = []
        total_pnl = 0
        
        for pick_data in prev_data.get("picks", []):
            symbol = pick_data["symbol"]
            prev_price = pick_data["current_price"]
            
            # 현재 가격 조회
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
        self.logger.info(f"  📊 총 P&L: ${total_pnl:+,.2f}")
        self.logger.info("─" * 60)
        
        return {
            "date": prev_date,
            "eval_date": today,
            "results": results,
            "total_pnl": round(total_pnl, 2),
        }
    
    # ── 데몬 모드 (매일 자동 실행) ────────────────────────────
    
    def run_daemon(self):
        """
        데몬 모드 - 매일 지정 시간에 자동 스크리닝
        
        프로세스:
        1. 전일 추천 종목 성과 평가
        2. 새로운 스크리닝 실행
        3. TOP 10 선별 & 리포트 생성
        4. Smart Trader에 워치리스트 전달
        5. 다음날까지 대기
        """
        self.logger.info("\n" + "╔" + "═" * 58 + "╗")
        self.logger.info("║  🤖 Auto Screener - 데몬 모드 시작                      ║")
        self.logger.info(f"║  실행 시간: 매일 {self.config.run_time_hour:02d}:{self.config.run_time_minute:02d}                                ║")
        self.logger.info("║  Ctrl+C로 종료                                           ║")
        self.logger.info("╚" + "═" * 58 + "╝\n")
        
        while True:
            now = datetime.now()
            target = now.replace(
                hour=self.config.run_time_hour,
                minute=self.config.run_time_minute,
                second=0,
                microsecond=0,
            )
            
            # 오늘 시간이 지났으면 내일로
            if now >= target:
                target += timedelta(days=1)
            
            wait_seconds = (target - now).total_seconds()
            self.logger.info(
                f"⏰ 다음 스크리닝: {target:%Y-%m-%d %H:%M} "
                f"({wait_seconds/3600:.1f}시간 후)"
            )
            
            # 대기
            try:
                time.sleep(wait_seconds)
            except KeyboardInterrupt:
                self.logger.info("\n🛑 데몬 모드 종료")
                break
            
            # 실행
            try:
                if not self.ib.isConnected():
                    if not self.connect():
                        self.logger.error("❌ TWS 연결 실패. 1시간 후 재시도.")
                        time.sleep(3600)
                        continue
                
                # 1) 전일 성과 평가
                self.evaluate_previous_picks()
                
                # 2) 새 스크리닝
                picks = self.run_screening()
                
                # 3) 워치리스트 파일 생성 (Smart Trader 연동용)
                watchlist = self.get_picks_with_allocation()
                with open("today_watchlist.json", "w") as f:
                    json.dump({
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "watchlist": watchlist,
                    }, f, indent=2, ensure_ascii=False)
                
                self.logger.info(
                    f"\n✅ 스크리닝 완료! "
                    f"TOP {len(picks)}: "
                    f"{', '.join(p.symbol for p in picks)}"
                )
                
            except Exception as e:
                self.logger.error(f"❌ 스크리닝 에러: {e}", exc_info=True)
            
            finally:
                self.disconnect()


# ═══════════════════════════════════════════════════════════════
#  Smart Trader 통합 실행기
# ═══════════════════════════════════════════════════════════════

def run_integrated(trade_mode: str = "alert"):
    """
    스크리너 + Smart Trader 통합 실행
    
    1. 스크리너로 TOP 10 선별
    2. 결과를 Smart Trader에 전달
    3. Smart Trader가 모니터링 & 매매 실행
    """
    from smart_trader import SmartTrader, TradingConfig, TradeMode
    
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🤖 IB Smart Trader + Auto Screener                     ║
    ║     통합 자동 매매 시스템                                ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    # 1) 스크리닝
    screener_config = ScreenerConfig()
    screener = AutoScreener(screener_config)
    
    if not screener.connect():
        return
    
    # 전일 성과 평가
    screener.evaluate_previous_picks()
    
    # 새 스크리닝
    picks = screener.run_screening()
    watchlist = [p.symbol for p in picks]
    screener.disconnect()
    
    if not watchlist:
        print("❌ 추천 종목이 없습니다.")
        return
    
    # 2) Smart Trader 설정
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
    
    # 3) Smart Trader 실행
    trader = SmartTrader(trader_config)
    trader.run(watchlist)


# ═══════════════════════════════════════════════════════════════
#  CLI 실행
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IB Smart Trader - Auto Stock Screener"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="데몬 모드 (매일 자동 실행)"
    )
    parser.add_argument(
        "--integrated", action="store_true",
        help="스크리너 + Smart Trader 통합 실행"
    )
    parser.add_argument(
        "--mode", choices=["alert", "auto"], default="alert",
        help="매매 모드: alert (알림만) | auto (자동매매)"
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help="전일 추천 종목 성과만 평가"
    )
    
    args = parser.parse_args()
    
    if args.integrated:
        run_integrated(args.mode)
        return
    
    config = ScreenerConfig()
    config.save()
    screener = AutoScreener(config)
    
    if args.daemon:
        # 데몬 모드
        screener.run_daemon()
    else:
        # 1회 실행
        if not screener.connect():
            return
        
        try:
            if args.evaluate:
                screener.evaluate_previous_picks()
            else:
                screener.evaluate_previous_picks()
                picks = screener.run_screening()
                
                # 투자 배분 출력
                allocations = screener.get_picks_with_allocation()
                print("\n  💰 투자 배분:")
                print("─" * 60)
                for a in allocations:
                    print(
                        f"    {a['symbol']:6s} | "
                        f"${a['allocation']:9,.0f} | "
                        f"{a['shares']:4d}주 @ ${a['price']:.2f} | "
                        f"점수: {a['score']}"
                    )
                total_alloc = sum(a["allocation"] for a in allocations)
                print("─" * 60)
                print(f"    총 투자금: ${total_alloc:,.0f}")
        finally:
            screener.disconnect()


if __name__ == "__main__":
    main()
