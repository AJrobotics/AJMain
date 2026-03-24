"""
═══════════════════════════════════════════════════════════════════
  Day Trading Risk Manager - 데이 트레이딩 리스크 관리 시스템

  기능:
    1. 일일 손실 한도 — 단계별 브레이크
    2. 종목당 손실 한도 — 개별 포지션 자동 청산
    3. 동시 포지션 제한 — 과도한 분산 방지
    4. 포지션 사이징 — ATR 기반 동적 조정
    5. EOD 강제 청산 — 15:50 ET 전 포지션 전량 청산
    6. PDT 규정 추적 — Pattern Day Trader 매매 횟수 관리
═══════════════════════════════════════════════════════════════════
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from collections import defaultdict

logger = logging.getLogger("DayRiskManager")


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class DayRiskConfig:
    """데이 트레이딩 리스크 설정"""

    # ── 자본 설정 ──
    capital: float = 75_000.0               # 데이 트레이딩 전용 자본

    # ── 일일 손실 한도 ──
    daily_loss_soft_limit: float = -1_500.0   # 1단계: 신규 진입 중단 ($)
    daily_loss_hard_limit: float = -2_000.0   # 2단계: 전 포지션 강제 청산 ($)
    daily_profit_target: float = 3_000.0      # 일일 목표 수익 ($) — 정보용

    # ── 종목당 손실 한도 ──
    per_stock_loss_limit: float = -500.0      # 종목당 최대 손실 ($)
    per_stock_loss_pct: float = -1.5          # 종목당 최대 손실 (%)

    # ── 포지션 제한 ──
    max_positions: int = 5                    # 동시 보유 최대 종목
    max_position_pct: float = 20.0            # 종목당 최대 자본 비중 (%)
    max_position_dollar: float = 15_000.0     # 종목당 최대 투자금 ($)

    # ── 포지션 사이징 ──
    risk_per_trade_pct: float = 1.0           # 매매당 위험 자본 (%)
    use_atr_sizing: bool = True               # ATR 기반 동적 사이징

    # ── EOD 강제 청산 ──
    eod_liquidation_enabled: bool = True
    eod_liquidation_time: str = "15:50"       # ET 기준 강제 청산 시간

    # ── PDT 규정 ──
    pdt_tracking_enabled: bool = True
    pdt_min_equity: float = 25_000.0          # PDT 최소 자본 ($)
    pdt_max_day_trades_5d: int = 3            # 5영업일 내 최대 데이 트레이드 수 (PDT 미만)

    # ── 쿨다운 ──
    cooldown_after_loss_min: int = 10         # 손절 후 N분 쿨다운
    max_trades_per_hour: int = 10             # 시간당 최대 매매 수


# ═══════════════════════════════════════════════════════════════
#  리스크 상태
# ═══════════════════════════════════════════════════════════════

class RiskLevel(Enum):
    NORMAL = "✅ 정상"
    CAUTION = "⚠️ 주의"
    SOFT_BRAKE = "🟡 신규진입 중단"
    HARD_BRAKE = "🔴 전량 청산"
    EOD_LIQUIDATION = "⏰ EOD 청산"


@dataclass
class DayPosition:
    """데이 트레이딩 포지션"""
    symbol: str
    side: str               # "LONG" or "SHORT"
    entry_price: float
    quantity: int
    entry_time: datetime
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.entry_price

    def update_pnl(self, current_price: float):
        self.current_price = current_price
        if self.side == "LONG":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity


@dataclass
class RiskCheckResult:
    """리스크 체크 결과"""
    level: RiskLevel = RiskLevel.NORMAL
    can_open_new: bool = True
    must_close_all: bool = False
    must_close_symbols: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    suggested_size: int = 0         # 권장 수량
    suggested_dollar: float = 0.0   # 권장 투자금


# ═══════════════════════════════════════════════════════════════
#  데이 리스크 매니저
# ═══════════════════════════════════════════════════════════════

class DayRiskManager:
    """데이 트레이딩 리스크 관리"""

    def __init__(self, config: DayRiskConfig = None):
        self.config = config or DayRiskConfig()
        self.positions: dict[str, DayPosition] = {}
        self.daily_pnl: float = 0.0
        self.realized_pnl: float = 0.0
        self.trade_count: int = 0
        self.trade_timestamps: list[datetime] = []
        self.loss_cooldowns: dict[str, datetime] = {}  # {symbol: cooldown_until}
        self._day_trades_5d: list[str] = []  # 최근 5일 데이 트레이드 날짜

    def reset_daily(self):
        """일일 리셋 (매일 장 시작 시 호출)"""
        self.positions.clear()
        self.daily_pnl = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.trade_timestamps.clear()
        self.loss_cooldowns.clear()
        logger.info("📋 일일 리스크 카운터 리셋")

    # ── 포지션 관리 ──────────────────────────────────────────

    def open_position(self, symbol: str, side: str, price: float,
                      quantity: int, stop_loss: float = 0, take_profit: float = 0):
        """포지션 오픈 기록"""
        self.positions[symbol] = DayPosition(
            symbol=symbol, side=side, entry_price=price,
            quantity=quantity, entry_time=datetime.now(),
            stop_loss=stop_loss, take_profit=take_profit,
        )
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())
        logger.info(
            f"📥 포지션 오픈: {side} {symbol} x{quantity} @ ${price:.2f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

    def close_position(self, symbol: str, exit_price: float) -> float:
        """포지션 클로즈 기록 → 실현 PnL 반환"""
        if symbol not in self.positions:
            return 0.0

        pos = self.positions[symbol]
        pos.update_pnl(exit_price)
        pnl = pos.unrealized_pnl

        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trade_count += 1
        self.trade_timestamps.append(datetime.now())

        # 데이 트레이드 기록 (PDT)
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self._day_trades_5d:
            self._day_trades_5d.append(today)
        # 최근 5일만 유지
        if len(self._day_trades_5d) > 5:
            self._day_trades_5d = self._day_trades_5d[-5:]

        # 손절 쿨다운
        if pnl < 0:
            cooldown_until = datetime.now() + timedelta(
                minutes=self.config.cooldown_after_loss_min
            )
            self.loss_cooldowns[symbol] = cooldown_until

        del self.positions[symbol]

        icon = "🟢" if pnl >= 0 else "🔴"
        logger.info(
            f"📤 포지션 클로즈: {symbol} @ ${exit_price:.2f} | "
            f"{icon} PnL: ${pnl:+,.2f} | 일일 누적: ${self.daily_pnl:+,.2f}"
        )
        return pnl

    def update_prices(self, price_map: dict[str, float]):
        """현재가 업데이트 → 미실현 PnL 재계산"""
        total_unrealized = 0.0
        for symbol, pos in self.positions.items():
            if symbol in price_map:
                pos.update_pnl(price_map[symbol])
            total_unrealized += pos.unrealized_pnl
        self.daily_pnl = self.realized_pnl + total_unrealized

    # ── 리스크 체크 ──────────────────────────────────────────

    def check_risk(self, symbol: str = "", current_price: float = 0.0) -> RiskCheckResult:
        """종합 리스크 체크"""
        result = RiskCheckResult()

        # 1. 일일 손실 한도
        self._check_daily_limits(result)

        # 2. 종목당 손실 한도
        self._check_per_stock_limits(result)

        # 3. 동시 포지션 제한
        self._check_position_count(result)

        # 4. EOD 청산 시간
        self._check_eod_time(result)

        # 5. 쿨다운
        if symbol:
            self._check_cooldown(symbol, result)

        # 6. 매매 빈도
        self._check_trade_frequency(result)

        # 7. PDT 규정
        self._check_pdt(result)

        # 최종 리스크 레벨 결정
        if result.must_close_all:
            result.level = RiskLevel.HARD_BRAKE
            result.can_open_new = False
        elif not result.can_open_new and any("EOD" in r for r in result.reasons):
            result.level = RiskLevel.EOD_LIQUIDATION
        elif not result.can_open_new:
            result.level = RiskLevel.SOFT_BRAKE
        elif result.reasons:
            result.level = RiskLevel.CAUTION

        return result

    def _check_daily_limits(self, result: RiskCheckResult):
        """일일 손실 한도 체크"""
        cfg = self.config

        if self.daily_pnl <= cfg.daily_loss_hard_limit:
            result.can_open_new = False
            result.must_close_all = True
            result.reasons.append(
                f"🔴 일일 손실 ${self.daily_pnl:+,.2f} ≤ "
                f"Hard limit ${cfg.daily_loss_hard_limit:,.2f} → 전량 청산"
            )
        elif self.daily_pnl <= cfg.daily_loss_soft_limit:
            result.can_open_new = False
            result.reasons.append(
                f"🟡 일일 손실 ${self.daily_pnl:+,.2f} ≤ "
                f"Soft limit ${cfg.daily_loss_soft_limit:,.2f} → 신규 진입 중단"
            )

    def _check_per_stock_limits(self, result: RiskCheckResult):
        """종목당 손실 한도 체크"""
        cfg = self.config

        for symbol, pos in self.positions.items():
            # 절대 금액 체크
            if pos.unrealized_pnl <= cfg.per_stock_loss_limit:
                result.must_close_symbols.append(symbol)
                result.reasons.append(
                    f"🔴 {symbol} 손실 ${pos.unrealized_pnl:+,.2f} ≤ "
                    f"한도 ${cfg.per_stock_loss_limit:,.2f}"
                )
                continue

            # 비율 체크
            if pos.entry_price > 0:
                pnl_pct = pos.unrealized_pnl / (pos.entry_price * pos.quantity) * 100
                if pnl_pct <= cfg.per_stock_loss_pct:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🔴 {symbol} 손실 {pnl_pct:.1f}% ≤ 한도 {cfg.per_stock_loss_pct}%"
                    )

            # 손절가 도달 체크
            if pos.stop_loss > 0 and pos.current_price > 0:
                if pos.side == "LONG" and pos.current_price <= pos.stop_loss:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🛑 {symbol} 손절가 도달 "
                        f"${pos.current_price:.2f} ≤ SL ${pos.stop_loss:.2f}"
                    )

            # 익절가 도달 체크
            if pos.take_profit > 0 and pos.current_price > 0:
                if pos.side == "LONG" and pos.current_price >= pos.take_profit:
                    result.must_close_symbols.append(symbol)
                    result.reasons.append(
                        f"🎯 {symbol} 익절가 도달 "
                        f"${pos.current_price:.2f} ≥ TP ${pos.take_profit:.2f}"
                    )

    def _check_position_count(self, result: RiskCheckResult):
        """동시 포지션 수 체크"""
        if len(self.positions) >= self.config.max_positions:
            result.can_open_new = False
            result.reasons.append(
                f"📊 동시 포지션 {len(self.positions)}/{self.config.max_positions} 최대"
            )

    def _check_eod_time(self, result: RiskCheckResult):
        """장 마감 전 강제 청산 시간 체크"""
        if not self.config.eod_liquidation_enabled:
            return

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = datetime.now()

        h, m = map(int, self.config.eod_liquidation_time.split(":"))
        liquidation_time = now_et.replace(hour=h, minute=m, second=0, microsecond=0)

        if now_et >= liquidation_time and self.positions:
            result.can_open_new = False
            result.must_close_all = True
            result.reasons.append(
                f"⏰ EOD 강제 청산 시간 ({self.config.eod_liquidation_time} ET) 도달"
            )
        elif now_et >= liquidation_time - timedelta(minutes=15):
            result.can_open_new = False
            result.reasons.append(
                f"⏰ EOD 청산 15분 전 — 신규 진입 중단"
            )

    def _check_cooldown(self, symbol: str, result: RiskCheckResult):
        """손절 후 쿨다운 체크"""
        if symbol in self.loss_cooldowns:
            until = self.loss_cooldowns[symbol]
            if datetime.now() < until:
                remaining = int((until - datetime.now()).total_seconds() / 60)
                result.can_open_new = False
                result.reasons.append(
                    f"⏱️ {symbol} 쿨다운 {remaining}분 남음 "
                    f"(손절 후 {self.config.cooldown_after_loss_min}분 대기)"
                )
            else:
                del self.loss_cooldowns[symbol]

    def _check_trade_frequency(self, result: RiskCheckResult):
        """매매 빈도 체크"""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_trades = sum(1 for t in self.trade_timestamps if t > one_hour_ago)

        if recent_trades >= self.config.max_trades_per_hour:
            result.can_open_new = False
            result.reasons.append(
                f"⚡ 시간당 매매 {recent_trades}/{self.config.max_trades_per_hour} 초과"
            )

    def _check_pdt(self, result: RiskCheckResult):
        """PDT 규정 체크"""
        if not self.config.pdt_tracking_enabled:
            return

        # 자본금이 PDT 미만이면 경고
        if self.config.capital < self.config.pdt_min_equity:
            day_trade_count = len(self._day_trades_5d)
            if day_trade_count >= self.config.pdt_max_day_trades_5d:
                result.can_open_new = False
                result.reasons.append(
                    f"⚠️ PDT 규정! 5일 내 데이트레이드 "
                    f"{day_trade_count}/{self.config.pdt_max_day_trades_5d}회 "
                    f"(자본 ${self.config.capital:,.0f} < ${self.config.pdt_min_equity:,.0f})"
                )

    # ── 포지션 사이징 ────────────────────────────────────────

    def calculate_position_size(
        self, symbol: str, price: float, atr: float = 0.0,
        stop_distance: float = 0.0,
    ) -> dict:
        """
        포지션 크기 계산

        Returns:
            {
                "shares": 수량,
                "dollar_amount": 투자금,
                "risk_amount": 위험 금액,
                "method": 사이징 방법,
            }
        """
        cfg = self.config

        # 최대 투자금 (자본의 N% 또는 고정 한도 중 작은 값)
        max_dollar = min(
            cfg.capital * cfg.max_position_pct / 100,
            cfg.max_position_dollar,
        )

        # ATR 기반 사이징
        if cfg.use_atr_sizing and atr > 0 and stop_distance > 0:
            risk_amount = cfg.capital * cfg.risk_per_trade_pct / 100
            shares = int(risk_amount / stop_distance)
            dollar_amount = shares * price
            method = f"ATR (위험 ${risk_amount:.0f} / SL거리 ${stop_distance:.2f})"
        else:
            # 고정 비율 사이징
            dollar_amount = max_dollar
            shares = int(dollar_amount / price) if price > 0 else 0
            method = f"고정 (자본의 {cfg.max_position_pct}%)"

        # 최대 투자금 제한 적용
        if shares * price > max_dollar:
            shares = int(max_dollar / price)
            dollar_amount = shares * price

        # 최소 1주
        shares = max(1, shares)
        dollar_amount = shares * price

        return {
            "shares": shares,
            "dollar_amount": round(dollar_amount, 2),
            "risk_amount": round(stop_distance * shares, 2) if stop_distance > 0 else 0,
            "method": method,
        }

    # ── 상태 출력 ────────────────────────────────────────────

    def get_status(self) -> dict:
        """현재 리스크 상태 요약"""
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "position_count": len(self.positions),
            "max_positions": self.config.max_positions,
            "trade_count": self.trade_count,
            "capital": self.config.capital,
            "daily_pnl_pct": round(self.daily_pnl / self.config.capital * 100, 2),
        }

    def print_dashboard(self):
        """리스크 대시보드 출력"""
        status = self.get_status()
        risk = self.check_risk()

        icon = "🟢" if status["daily_pnl"] >= 0 else "🔴"

        print(f"\n  {risk.level.value}")
        print(f"  {icon} 일일 PnL: ${status['daily_pnl']:+,.2f} ({status['daily_pnl_pct']:+.2f}%)")
        print(f"    실현: ${status['realized_pnl']:+,.2f} | 미실현: ${status['unrealized_pnl']:+,.2f}")
        print(f"    포지션: {status['position_count']}/{status['max_positions']} | 매매: {status['trade_count']}회")

        if self.positions:
            print(f"    ── 포지션 상세 ──")
            for sym, pos in self.positions.items():
                pnl_icon = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
                print(
                    f"      {pnl_icon} {sym:6s} {pos.side:5s} "
                    f"x{pos.quantity:4d} @ ${pos.entry_price:.2f} "
                    f"→ ${pos.current_price:.2f} | "
                    f"PnL: ${pos.unrealized_pnl:+,.2f}"
                )

        if risk.reasons:
            print(f"    ── 경고 ──")
            for reason in risk.reasons:
                print(f"      {reason}")


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  🛡️ Day Risk Manager 데모                                ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    rm = DayRiskManager(DayRiskConfig(capital=75_000))

    # 1) 포지션 오픈
    rm.open_position("NVDA", "LONG", 180.00, 50, stop_loss=178.50, take_profit=183.00)
    rm.open_position("AAPL", "LONG", 215.00, 30, stop_loss=213.50, take_profit=218.00)
    rm.open_position("TSLA", "LONG", 250.00, 20, stop_loss=247.00, take_profit=256.00)

    # 2) 가격 업데이트
    rm.update_prices({"NVDA": 181.50, "AAPL": 214.00, "TSLA": 248.00})
    rm.print_dashboard()

    # 3) 포지션 사이징 예시
    print("\n  📏 포지션 사이징 (AMD, $160, ATR=$2.5):")
    sizing = rm.calculate_position_size("AMD", 160.0, atr=2.5, stop_distance=3.75)
    print(f"    수량: {sizing['shares']}주 | 투자금: ${sizing['dollar_amount']:,.2f}")
    print(f"    위험금액: ${sizing['risk_amount']:,.2f} | 방법: {sizing['method']}")

    # 4) 손실 시나리오
    print("\n  📉 TSLA 손절 시나리오:")
    rm.update_prices({"NVDA": 181.50, "AAPL": 214.00, "TSLA": 246.00})
    risk = rm.check_risk("TSLA", 246.00)
    print(f"    리스크: {risk.level.value}")
    for r in risk.reasons:
        print(f"    {r}")

    # 5) 클로즈
    pnl = rm.close_position("TSLA", 246.00)
    print(f"    TSLA 청산 PnL: ${pnl:+,.2f}")
    rm.print_dashboard()


if __name__ == "__main__":
    demo()
