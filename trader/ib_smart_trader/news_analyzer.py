"""
═══════════════════════════════════════════════════════════════════
  News Analyzer - 뉴스 분석 모듈

  기능:
    - 시장 시간대별 자동 주기 조절
    - 뉴스 수집 (Finnhub / Yahoo Finance / RSS)
    - 키워드 기반 센티먼트 분석
    - Smart Trader 앙상블 7번째 전략으로 통합 가능

  주기:
    - Pre-market  (04:00~09:30 ET): 30분
    - Market open (09:30~10:30 ET): 15분
    - Regular     (10:30~16:00 ET): 30분
    - After hours (16:00~04:00 ET): 3시간
═══════════════════════════════════════════════════════════════════
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional
from collections import deque

logger = logging.getLogger("NewsAnalyzer")


# ═══════════════════════════════════════════════════════════════
#  시장 시간 유틸
# ═══════════════════════════════════════════════════════════════

def _now_et():
    """현재 시간 (Eastern Time)"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def get_market_phase() -> str:
    """
    현재 시장 단계 반환

    Returns: "premarket" | "open_early" | "regular" | "after_hours" | "weekend"
    """
    now = _now_et()
    if now.weekday() >= 5:
        return "weekend"

    h, m = now.hour, now.minute
    t = h * 60 + m  # 분 단위

    if t < 4 * 60:           # 00:00~04:00
        return "after_hours"
    elif t < 9 * 60 + 30:    # 04:00~09:30
        return "premarket"
    elif t < 10 * 60 + 30:   # 09:30~10:30
        return "open_early"
    elif t < 16 * 60:        # 10:30~16:00
        return "regular"
    else:                     # 16:00~24:00
        return "after_hours"


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class NewsConfig:
    """뉴스 분석 설정"""

    # ── 주기 (초) ──
    interval_premarket: int = 1800     # 30분
    interval_open_early: int = 900     # 15분
    interval_regular: int = 1800       # 30분
    interval_after_hours: int = 10800  # 3시간
    interval_weekend: int = 10800      # 3시간

    # ── 뉴스 소스 ──
    finnhub_api_key: str = ""          # Finnhub API 키 (무료 60콜/분)
    use_finnhub: bool = True
    use_yahoo: bool = True
    use_rss: bool = True

    # ── RSS 피드 ──
    rss_feeds: list = field(default_factory=lambda: [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^DJI&region=US&lang=en-US",
    ])

    # ── 센티먼트 설정 ──
    bull_threshold: float = 0.3        # 이 이상이면 BULL
    bear_threshold: float = -0.3       # 이 이하이면 BEAR

    # ── 앙상블 가중치 ──
    ensemble_weight: float = 0.10      # 뉴스 전략 가중치

    # ── 관심 종목 (빈 리스트면 시장 전체 뉴스) ──
    watch_symbols: list = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "TSLA", "AMZN", "NVDA", "META",
    ])

    # ── 저장 ──
    log_file: str = "news_analysis.log"
    history_file: str = "news_history.json"

    def get_interval(self) -> int:
        """현재 시장 단계에 맞는 주기 반환"""
        phase = get_market_phase()
        intervals = {
            "premarket": self.interval_premarket,
            "open_early": self.interval_open_early,
            "regular": self.interval_regular,
            "after_hours": self.interval_after_hours,
            "weekend": self.interval_weekend,
        }
        return intervals.get(phase, self.interval_after_hours)

    def save(self, path="news_config.json"):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path="news_config.json"):
        if not os.path.exists(path):
            return cls()
        with open(path, "r") as f:
            return cls(**json.load(f))


# ═══════════════════════════════════════════════════════════════
#  센티먼트 키워드 사전
# ═══════════════════════════════════════════════════════════════

BULLISH_KEYWORDS = {
    # 강한 긍정 (+2)
    "surge": 2, "soar": 2, "skyrocket": 2, "rally": 2,
    "breakout": 2, "record high": 2, "all-time high": 2,
    "blowout earnings": 2, "beat expectations": 2,
    # 일반 긍정 (+1)
    "rise": 1, "gain": 1, "climb": 1, "jump": 1, "up": 1,
    "bullish": 1, "upgrade": 1, "buy rating": 1, "outperform": 1,
    "growth": 1, "profit": 1, "revenue beat": 1, "strong": 1,
    "recovery": 1, "rebound": 1, "positive": 1, "optimistic": 1,
    "rate cut": 1, "stimulus": 1, "dovish": 1,
}

BEARISH_KEYWORDS = {
    # 강한 부정 (-2)
    "crash": -2, "plunge": -2, "collapse": -2, "freefall": -2,
    "recession": -2, "crisis": -2, "bankruptcy": -2,
    "miss expectations": -2, "earnings miss": -2,
    # 일반 부정 (-1)
    "fall": -1, "drop": -1, "decline": -1, "slip": -1, "down": -1,
    "bearish": -1, "downgrade": -1, "sell rating": -1, "underperform": -1,
    "loss": -1, "warning": -1, "weak": -1, "concern": -1,
    "layoff": -1, "tariff": -1, "sanction": -1, "war": -1,
    "rate hike": -1, "hawkish": -1, "inflation": -1,
    "investigation": -1, "lawsuit": -1, "fraud": -1,
}


# ═══════════════════════════════════════════════════════════════
#  뉴스 수집기
# ═══════════════════════════════════════════════════════════════

@dataclass
class NewsArticle:
    """뉴스 기사"""
    title: str
    source: str
    symbol: str = ""         # 관련 종목 (빈 문자열이면 시장 전체)
    url: str = ""
    published: str = ""      # ISO format
    sentiment_score: float = 0.0   # -1.0 ~ +1.0
    sentiment_label: str = "NEUTRAL"
    matched_keywords: list = field(default_factory=list)


class NewsFetcher:
    """뉴스 수집 (다중 소스)"""

    def __init__(self, config: NewsConfig):
        self.config = config

    def fetch_all(self, symbols: list[str] = None) -> list[NewsArticle]:
        """모든 소스에서 뉴스 수집"""
        symbols = symbols or self.config.watch_symbols
        articles = []

        if self.config.use_finnhub and self.config.finnhub_api_key:
            articles.extend(self._fetch_finnhub(symbols))

        if self.config.use_rss:
            articles.extend(self._fetch_rss())

        if self.config.use_yahoo:
            articles.extend(self._fetch_yahoo(symbols))

        logger.info(f"뉴스 {len(articles)}건 수집 완료")
        return articles

    def _fetch_finnhub(self, symbols: list[str]) -> list[NewsArticle]:
        """Finnhub API에서 뉴스 수집"""
        articles = []
        try:
            import urllib.request
            today = datetime.now().strftime("%Y-%m-%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            for symbol in symbols[:10]:  # API 제한 고려
                url = (
                    f"https://finnhub.io/api/v1/company-news"
                    f"?symbol={symbol}&from={yesterday}&to={today}"
                    f"&token={self.config.finnhub_api_key}"
                )
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "SmartTrader/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode())

                    for item in data[:5]:  # 종목당 최근 5건
                        articles.append(NewsArticle(
                            title=item.get("headline", ""),
                            source="finnhub",
                            symbol=symbol,
                            url=item.get("url", ""),
                            published=datetime.fromtimestamp(
                                item.get("datetime", 0)
                            ).isoformat(),
                        ))
                    time.sleep(0.5)  # rate limit
                except Exception as e:
                    logger.debug(f"Finnhub {symbol}: {e}")

        except Exception as e:
            logger.warning(f"Finnhub 수집 실패: {e}")
        return articles

    def _fetch_rss(self) -> list[NewsArticle]:
        """RSS 피드에서 뉴스 수집"""
        articles = []
        try:
            import xml.etree.ElementTree as ET
            import urllib.request

            for feed_url in self.config.rss_feeds:
                try:
                    req = urllib.request.Request(
                        feed_url,
                        headers={"User-Agent": "SmartTrader/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        root = ET.fromstring(resp.read())

                    for item in root.iter("item"):
                        title = item.findtext("title", "")
                        link = item.findtext("link", "")
                        pub = item.findtext("pubDate", "")
                        if title:
                            articles.append(NewsArticle(
                                title=title,
                                source="rss",
                                url=link,
                                published=pub,
                            ))
                except Exception as e:
                    logger.debug(f"RSS {feed_url}: {e}")

        except Exception as e:
            logger.warning(f"RSS 수집 실패: {e}")
        return articles

    def _fetch_yahoo(self, symbols: list[str]) -> list[NewsArticle]:
        """Yahoo Finance RSS에서 종목별 뉴스 수집"""
        articles = []
        try:
            import xml.etree.ElementTree as ET
            import urllib.request

            for symbol in symbols[:10]:
                url = (
                    f"https://feeds.finance.yahoo.com/rss/2.0/headline"
                    f"?s={symbol}&region=US&lang=en-US"
                )
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "SmartTrader/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        root = ET.fromstring(resp.read())

                    for item in root.iter("item"):
                        title = item.findtext("title", "")
                        link = item.findtext("link", "")
                        pub = item.findtext("pubDate", "")
                        if title:
                            articles.append(NewsArticle(
                                title=title,
                                source="yahoo",
                                symbol=symbol,
                                url=link,
                                published=pub,
                            ))
                except Exception as e:
                    logger.debug(f"Yahoo {symbol}: {e}")

        except Exception as e:
            logger.warning(f"Yahoo 수집 실패: {e}")
        return articles


# ═══════════════════════════════════════════════════════════════
#  센티먼트 분석기
# ═══════════════════════════════════════════════════════════════

class SentimentAnalyzer:
    """키워드 기반 센티먼트 분석"""

    def analyze(self, article: NewsArticle) -> NewsArticle:
        """기사 센티먼트 분석"""
        text = article.title.lower()
        score = 0
        matched = []

        for keyword, weight in BULLISH_KEYWORDS.items():
            if keyword in text:
                score += weight
                matched.append(f"+{keyword}")

        for keyword, weight in BEARISH_KEYWORDS.items():
            if keyword in text:
                score += weight  # weight is already negative
                matched.append(f"{keyword}")

        # -1.0 ~ +1.0 범위로 정규화
        max_possible = 6  # 대략적 최대 점수
        normalized = max(-1.0, min(1.0, score / max_possible))

        article.sentiment_score = normalized
        article.matched_keywords = matched

        if normalized > 0.15:
            article.sentiment_label = "BULLISH"
        elif normalized < -0.15:
            article.sentiment_label = "BEARISH"
        else:
            article.sentiment_label = "NEUTRAL"

        return article

    def analyze_batch(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """여러 기사 일괄 분석"""
        return [self.analyze(a) for a in articles]


# ═══════════════════════════════════════════════════════════════
#  뉴스 분석 결과
# ═══════════════════════════════════════════════════════════════

@dataclass
class NewsSignal:
    """뉴스 분석 종합 결과"""
    signal: str = "NEUTRAL"        # BULL / BEAR / NEUTRAL
    confidence: float = 0.5
    avg_sentiment: float = 0.0
    total_articles: int = 0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    top_bullish: list = field(default_factory=list)   # 가장 긍정적 기사 제목
    top_bearish: list = field(default_factory=list)    # 가장 부정적 기사 제목
    market_phase: str = ""
    timestamp: str = ""
    reason: str = ""


# ═══════════════════════════════════════════════════════════════
#  통합 뉴스 분석기
# ═══════════════════════════════════════════════════════════════

class NewsAnalyzer:
    """
    뉴스 수집 + 분석 + 시그널 생성

    사용법:
        from news_analyzer import NewsAnalyzer, NewsConfig

        config = NewsConfig(finnhub_api_key="YOUR_KEY")
        analyzer = NewsAnalyzer(config)

        # 1회 분석
        signal = analyzer.analyze_now()
        print(signal.signal, signal.confidence)

        # 데몬 모드 (자동 주기 조절)
        analyzer.start_daemon()
    """

    def __init__(self, config: NewsConfig = None):
        self.config = config or NewsConfig()
        self.fetcher = NewsFetcher(self.config)
        self.sentiment = SentimentAnalyzer()
        self._latest_signal = NewsSignal()
        self._history: deque[NewsSignal] = deque(maxlen=100)
        self._articles_cache: list[NewsArticle] = []
        self._daemon_thread: Optional[threading.Thread] = None
        self._running = False

        # 로깅
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-5s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

    def analyze_now(self, symbols: list[str] = None) -> NewsSignal:
        """뉴스 수집 → 분석 → 시그널 생성 (1회)"""
        phase = get_market_phase()
        logger.info(f"뉴스 분석 시작 (시장: {phase})")

        # 수집
        articles = self.fetcher.fetch_all(symbols)
        if not articles:
            logger.warning("수집된 뉴스 없음")
            self._latest_signal = NewsSignal(
                market_phase=phase,
                timestamp=datetime.now().isoformat(),
                reason="뉴스 없음",
            )
            return self._latest_signal

        # 분석
        analyzed = self.sentiment.analyze_batch(articles)
        self._articles_cache = analyzed

        # 종합
        scores = [a.sentiment_score for a in analyzed]
        avg = sum(scores) / len(scores)

        bullish = [a for a in analyzed if a.sentiment_label == "BULLISH"]
        bearish = [a for a in analyzed if a.sentiment_label == "BEARISH"]
        neutral = [a for a in analyzed if a.sentiment_label == "NEUTRAL"]

        # 시그널 결정
        if avg > self.config.bull_threshold:
            signal = "BULL"
            confidence = min(0.85, 0.5 + abs(avg))
        elif avg < self.config.bear_threshold:
            signal = "BEAR"
            confidence = min(0.85, 0.5 + abs(avg))
        else:
            signal = "NEUTRAL"
            confidence = 0.4

        # 기사 비율 보정
        total = len(analyzed)
        if total >= 5:
            bull_ratio = len(bullish) / total
            bear_ratio = len(bearish) / total
            if bull_ratio > 0.6:
                confidence = min(confidence + 0.1, 0.9)
            elif bear_ratio > 0.6:
                confidence = min(confidence + 0.1, 0.9)

        # 상위 기사
        sorted_bull = sorted(bullish, key=lambda a: a.sentiment_score, reverse=True)
        sorted_bear = sorted(bearish, key=lambda a: a.sentiment_score)

        result = NewsSignal(
            signal=signal,
            confidence=confidence,
            avg_sentiment=round(avg, 3),
            total_articles=total,
            bullish_count=len(bullish),
            bearish_count=len(bearish),
            neutral_count=len(neutral),
            top_bullish=[a.title for a in sorted_bull[:3]],
            top_bearish=[a.title for a in sorted_bear[:3]],
            market_phase=phase,
            timestamp=datetime.now().isoformat(),
            reason=(
                f"뉴스 {total}건 분석: "
                f"긍정 {len(bullish)} / 중립 {len(neutral)} / 부정 {len(bearish)} "
                f"(평균: {avg:+.3f})"
            ),
        )

        self._latest_signal = result
        self._history.append(result)
        self._save_history()

        logger.info(
            f"분석 완료: {signal} (신뢰도: {confidence:.0%}) | "
            f"긍정 {len(bullish)} / 부정 {len(bearish)} / 중립 {len(neutral)}"
        )
        return result

    @property
    def latest_signal(self) -> NewsSignal:
        return self._latest_signal

    def get_ensemble_strategy_signal(self) -> dict:
        """
        앙상블 전략 신호 (smart_trader 통합용)

        signal_bridge.py의 get_ensemble_strategy_signal()과 같은 형태
        """
        sig = self._latest_signal
        signal_map = {"BULL": "BUY", "BEAR": "SELL", "NEUTRAL": "HOLD"}

        return {
            "strategy_name": "NEWS_SENTIMENT",
            "signal": signal_map.get(sig.signal, "HOLD"),
            "confidence": sig.confidence,
            "reason": f"뉴스 {sig.signal} — {sig.reason}",
            "weight": self.config.ensemble_weight,
        }

    # ── 데몬 모드 ──────────────────────────────────────────────

    def start_daemon(self):
        """백그라운드 데몬 시작 (시장 시간대별 자동 주기 조절)"""
        if self._running:
            logger.warning("이미 실행 중")
            return

        self._running = True
        self._daemon_thread = threading.Thread(
            target=self._daemon_loop, daemon=True
        )
        self._daemon_thread.start()
        logger.info("뉴스 분석 데몬 시작")

    def stop_daemon(self):
        """데몬 중지"""
        self._running = False
        logger.info("뉴스 분석 데몬 중지")

    def _daemon_loop(self):
        """데몬 메인 루프"""
        while self._running:
            try:
                self.analyze_now()
            except Exception as e:
                logger.error(f"분석 에러: {e}")

            interval = self.config.get_interval()
            phase = get_market_phase()
            logger.info(
                f"다음 분석: {interval // 60}분 후 (시장: {phase})"
            )
            # 10초 단위로 체크하면서 대기 (빠른 종료 가능)
            waited = 0
            while waited < interval and self._running:
                time.sleep(10)
                waited += 10

    # ── 상태 출력 ──────────────────────────────────────────────

    def print_status(self):
        """현재 뉴스 분석 상태 출력"""
        sig = self._latest_signal
        icon = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
        phase_kr = {
            "premarket": "프리마켓",
            "open_early": "장 초반",
            "regular": "장중",
            "after_hours": "장외",
            "weekend": "주말",
        }

        phase = get_market_phase()
        interval = self.config.get_interval()

        print(f"\n  {icon.get(sig.signal, '🟡')} 뉴스 센티먼트: {sig.signal} "
              f"(신뢰도: {sig.confidence:.0%})")
        print(f"    기사 {sig.total_articles}건: "
              f"긍정 {sig.bullish_count} / 중립 {sig.neutral_count} / "
              f"부정 {sig.bearish_count}")
        print(f"    평균 센티먼트: {sig.avg_sentiment:+.3f}")
        print(f"    시장: {phase_kr.get(phase, phase)} | "
              f"분석 주기: {interval // 60}분")

        if sig.top_bullish:
            print(f"    📈 긍정: {sig.top_bullish[0][:60]}")
        if sig.top_bearish:
            print(f"    📉 부정: {sig.top_bearish[0][:60]}")

    # ── 히스토리 ──────────────────────────────────────────────

    def _save_history(self):
        """분석 히스토리 저장"""
        try:
            data = []
            for h in self._history:
                data.append({
                    "signal": h.signal,
                    "confidence": h.confidence,
                    "avg_sentiment": h.avg_sentiment,
                    "total_articles": h.total_articles,
                    "bullish_count": h.bullish_count,
                    "bearish_count": h.bearish_count,
                    "market_phase": h.market_phase,
                    "timestamp": h.timestamp,
                    "reason": h.reason,
                })
            with open(self.config.history_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"히스토리 저장 실패: {e}")


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  📰 News Analyzer 데모                                   ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    config = NewsConfig()
    analyzer = NewsAnalyzer(config)

    phase = get_market_phase()
    interval = config.get_interval()
    phase_kr = {
        "premarket": "프리마켓 (04:00~09:30)",
        "open_early": "장 초반 (09:30~10:30)",
        "regular": "장중 (10:30~16:00)",
        "after_hours": "장외 (16:00~04:00)",
        "weekend": "주말",
    }

    print(f"  📅 현재 시장 단계: {phase_kr.get(phase, phase)}")
    print(f"  ⏱️  분석 주기: {interval // 60}분 ({interval}초)")
    print()

    # 주기 테이블
    print("  ┌──────────────────────┬──────────┐")
    print("  │ 시장 단계            │ 분석 주기│")
    print("  ├──────────────────────┼──────────┤")
    print(f"  │ 프리마켓 (04~09:30)  │ {config.interval_premarket // 60:4d}분   │")
    print(f"  │ 장 초반  (09:30~10:30)│ {config.interval_open_early // 60:4d}분   │")
    print(f"  │ 장중     (10:30~16:00)│ {config.interval_regular // 60:4d}분   │")
    print(f"  │ 장외     (16:00~04:00)│ {config.interval_after_hours // 60:4d}분   │")
    print(f"  │ 주말                  │ {config.interval_weekend // 60:4d}분   │")
    print("  └──────────────────────┴──────────┘")
    print()

    # 센티먼트 분석 테스트
    print("  🧪 센티먼트 분석 테스트:")
    sa = SentimentAnalyzer()
    test_headlines = [
        "NVDA shares surge on record earnings beat, AI demand soars",
        "Fed signals rate cut likely in June, markets rally",
        "Tesla stock drops amid weak delivery numbers and tariff concerns",
        "Apple announces new product line, analysts upgrade to buy rating",
        "Recession fears grow as unemployment data misses expectations",
        "Microsoft cloud revenue beats estimates, stock climbs",
    ]
    for headline in test_headlines:
        article = NewsArticle(title=headline, source="test")
        result = sa.analyze(article)
        icon = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➖"}
        print(f"    {icon[result.sentiment_label]} [{result.sentiment_score:+.2f}] "
              f"{headline[:55]}")

    # 실제 뉴스 수집 (API 키 없어도 RSS는 동작)
    print(f"\n  📡 실제 뉴스 수집 시도 (RSS)...")
    signal = analyzer.analyze_now()
    analyzer.print_status()


if __name__ == "__main__":
    demo()
