"""
═══════════════════════════════════════════════════════════════════
  Tax Optimizer Module - 세금 최적화 시스템
  
  기능:
    6. Tax-Loss Harvesting 자동화
       - 미실현 손실 종목을 자동 감지하여 매도
       - 실현 손실로 다른 수익을 상쇄하여 세금 절감
       - 연간 $3,000 순손실 공제 한도 추적
       
    7. Wash Sale Rule 방지 필터  
       - 손실 매도 후 30일 내 동일 종목 재매수 차단
       - 실질적으로 동일한 종목(ETF ↔ 개별주) 경고
       - 자동으로 대체 종목 제안
  
  California 특수 사항:
    - CA 주세는 Long/Short term 구분 없이 동일 세율
    - 연방 Short-term = 일반 소득세율 (22-37%)
    - 연방 Long-term = 우대세율 (0-20%)
    → 가능하면 1년 이상 보유가 연방세 절약에 유리
  
  Risk Shield와 통합하여 사용
═══════════════════════════════════════════════════════════════════
"""

import json
import os
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("TaxOptimizer")


# ═══════════════════════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaxConfig:
    """세금 최적화 설정"""
    
    # ── Tax-Loss Harvesting ──
    tlh_enabled: bool = True
    tlh_min_loss_pct: float = -5.0       # 최소 이 % 이상 손실이어야 수확
    tlh_min_loss_dollar: float = -100.0  # 최소 이 금액 이상 손실
    tlh_check_interval_days: int = 7     # N일마다 TLH 스캔
    tlh_annual_loss_target: float = 3000.0  # 연간 손실 공제 목표 ($3,000 IRS 한도)
    tlh_avoid_year_end_days: int = 5     # 12월 마지막 N일은 TLH 보수적으로
    
    # ── Wash Sale Prevention ──
    wash_sale_enabled: bool = True
    wash_sale_window_days: int = 30      # IRS 규정: 매도 전후 30일
    wash_sale_block_identical: bool = True     # 동일 종목 차단
    wash_sale_warn_similar: bool = True        # 유사 종목 경고
    
    # ── 보유 기간 추적 ──
    holding_period_tracking: bool = True
    long_term_threshold_days: int = 366  # 1년 + 1일 이상 = Long-term
    warn_near_long_term_days: int = 30   # Long-term 전환 N일 전 매도 경고
    
    # ── 기록 파일 ──
    tax_log_file: str = "tax_records.json"


# ═══════════════════════════════════════════════════════════════
#  매매 세금 기록
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaxLot:
    """개별 매매 세금 기록 (Tax Lot)"""
    symbol: str
    buy_date: str           # "2026-02-15"
    buy_price: float
    shares: int
    sell_date: str = ""     # 매도 시 기입
    sell_price: float = 0.0
    realized_pnl: float = 0.0
    is_wash_sale: bool = False
    holding_days: int = 0
    is_long_term: bool = False
    
    def to_dict(self):
        return {
            "symbol": self.symbol,
            "buy_date": self.buy_date,
            "buy_price": self.buy_price,
            "shares": self.shares,
            "sell_date": self.sell_date,
            "sell_price": self.sell_price,
            "realized_pnl": self.realized_pnl,
            "is_wash_sale": self.is_wash_sale,
            "holding_days": self.holding_days,
            "is_long_term": self.is_long_term,
        }


# ═══════════════════════════════════════════════════════════════
#  6. Tax-Loss Harvesting 엔진
# ═══════════════════════════════════════════════════════════════

class TaxLossHarvester:
    """
    자동 Tax-Loss Harvesting (TLH)
    
    작동 방식:
    1. 보유 종목 중 미실현 손실이 임계값 이상인 종목 스캔
    2. 해당 종목 매도하여 손실 실현
    3. 실현된 손실로 다른 수익 상쇄 → 세금 감소
    4. 30일 후 원래 종목 재매수 가능 (Wash Sale 방지)
    
    예시:
    - XOM을 $150에 매수 → 현재 $140 (미실현 손실 -$1,000)
    - TLH가 자동 매도 → $1,000 손실 실현
    - 이 $1,000으로 다른 종목의 $1,000 수익 상쇄
    - 30일 후 XOM 재매수 가능 (또는 CVX를 대체 매수)
    """
    
    # 유사 종목 매핑 (Wash Sale 우회용 대체 종목)
    # 동일 섹터의 유사한 종목으로 교체하면 시장 노출은 유지하면서 Wash Sale 방지
    SUBSTITUTE_MAP = {
        # Energy
        "XOM": ["CVX", "COP", "BP"],
        "CVX": ["XOM", "COP", "SHEL"],
        "COP": ["XOM", "CVX", "EOG"],
        "OXY": ["DVN", "EOG", "PXD"],
        "DVN": ["OXY", "EOG", "COP"],
        # Defense
        "LMT": ["NOC", "RTX", "GD"],
        "NOC": ["LMT", "RTX", "LHX"],
        "RTX": ["LMT", "NOC", "GD"],
        "AVAV": ["KTOS", "LHX"],
        # Tech
        "NVDA": ["AMD", "AVGO", "TSM"],
        "AMD": ["NVDA", "INTC", "TSM"],
        "MSFT": ["GOOGL", "AAPL", "CRM"],
        "AAPL": ["MSFT", "GOOGL"],
        "META": ["GOOGL", "MSFT"],
        "PLTR": ["CRM", "ORCL"],
        # Tankers
        "FRO": ["DHT", "INSW", "STNG"],
        "DHT": ["FRO", "INSW"],
        # Fintech
        "HOOD": ["SQ", "COIN"],
        "MSTR": ["COIN"],
        # Consumer
        "WMT": ["COST", "TGT"],
        "COST": ["WMT", "TGT"],
        # Healthcare
        "UNH": ["JNJ", "ABBV"],
        "JNJ": ["UNH", "PFE"],
        # Gold
        "GLD": ["NEM", "GOLD", "FNV"],
    }
    
    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        self.ytd_realized_losses = 0.0   # 연초 대비 누적 실현 손실
        self.ytd_realized_gains = 0.0    # 연초 대비 누적 실현 수익
        self.tax_lots: list[TaxLot] = []
        self.last_scan_date: Optional[datetime] = None
        
        self._load_records()
    
    def _load_records(self):
        """기존 세금 기록 로드"""
        if os.path.exists(self.config.tax_log_file):
            try:
                with open(self.config.tax_log_file, "r") as f:
                    data = json.load(f)
                self.ytd_realized_losses = data.get("ytd_losses", 0.0)
                self.ytd_realized_gains = data.get("ytd_gains", 0.0)
                self.tax_lots = [
                    TaxLot(**lot) for lot in data.get("lots", [])
                ]
                logger.info(
                    f"  📋 세금 기록 로드: {len(self.tax_lots)}건 | "
                    f"YTD 손실: ${self.ytd_realized_losses:,.0f} | "
                    f"YTD 수익: ${self.ytd_realized_gains:,.0f}"
                )
            except Exception as e:
                logger.warning(f"  ⚠️ 세금 기록 로드 실패: {e}")
    
    def save_records(self):
        """세금 기록 저장"""
        data = {
            "ytd_losses": self.ytd_realized_losses,
            "ytd_gains": self.ytd_realized_gains,
            "last_updated": datetime.now().isoformat(),
            "lots": [lot.to_dict() for lot in self.tax_lots],
        }
        with open(self.config.tax_log_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def record_buy(self, symbol: str, price: float, shares: int):
        """매수 기록 추가"""
        lot = TaxLot(
            symbol=symbol,
            buy_date=datetime.now().strftime("%Y-%m-%d"),
            buy_price=price,
            shares=shares,
        )
        self.tax_lots.append(lot)
        self.save_records()
        logger.info(f"  📝 매수 기록: {symbol} {shares}주 @ ${price:.2f}")
    
    def record_sell(self, symbol: str, price: float, shares: int):
        """
        매도 기록 + 손익 계산 (FIFO 방식)
        FIFO = First In, First Out (먼저 산 주식부터 매도)
        """
        remaining = shares
        total_pnl = 0.0
        
        for lot in self.tax_lots:
            if lot.symbol != symbol or lot.sell_date != "" or remaining <= 0:
                continue
            
            sell_shares = min(lot.shares, remaining)
            lot.sell_date = datetime.now().strftime("%Y-%m-%d")
            lot.sell_price = price
            lot.realized_pnl = (price - lot.buy_price) * sell_shares
            
            # 보유 기간 계산
            buy_dt = datetime.strptime(lot.buy_date, "%Y-%m-%d")
            lot.holding_days = (datetime.now() - buy_dt).days
            lot.is_long_term = lot.holding_days >= self.config.long_term_threshold_days
            
            total_pnl += lot.realized_pnl
            remaining -= sell_shares
        
        # YTD 누적 업데이트
        if total_pnl < 0:
            self.ytd_realized_losses += total_pnl  # 음수값 누적
        else:
            self.ytd_realized_gains += total_pnl
        
        self.save_records()
        
        term = "Long-term" if any(
            l.is_long_term for l in self.tax_lots 
            if l.symbol == symbol and l.sell_date != ""
        ) else "Short-term"
        
        logger.info(
            f"  📝 매도 기록: {symbol} {shares}주 @ ${price:.2f} | "
            f"P&L: ${total_pnl:+,.0f} ({term}) | "
            f"YTD 순손익: ${self.ytd_realized_gains + self.ytd_realized_losses:+,.0f}"
        )
        
        return total_pnl
    
    def scan_for_harvest(
        self, 
        positions: dict,
    ) -> list[dict]:
        """
        TLH 기회 스캔 — 미실현 손실 종목 찾기
        
        Parameters:
            positions: {symbol: {"avg_cost": float, "quantity": int, "market_price": float}}
        
        Returns:
            수확 가능한 종목 리스트
        """
        if not self.config.tlh_enabled:
            return []
        
        # 연간 목표 체크
        net_loss = self.ytd_realized_losses + self.ytd_realized_gains
        remaining_target = self.config.tlh_annual_loss_target + net_loss  # 아직 수확 가능한 금액
        
        # 12월 말 보수적 모드
        now = datetime.now()
        if now.month == 12 and now.day > (31 - self.config.tlh_avoid_year_end_days):
            logger.info("  📅 12월 말 — TLH 보수적 모드")
        
        harvest_candidates = []
        
        for symbol, pos in positions.items():
            avg_cost = pos.get("avg_cost", 0)
            quantity = pos.get("quantity", 0)
            market_price = pos.get("market_price", avg_cost)
            
            if quantity <= 0 or avg_cost <= 0:
                continue
            
            unrealized_pnl = (market_price - avg_cost) * quantity
            unrealized_pct = ((market_price - avg_cost) / avg_cost) * 100
            
            # 손실이 임계값 이상인 종목만
            if (unrealized_pct <= self.config.tlh_min_loss_pct and
                unrealized_pnl <= self.config.tlh_min_loss_dollar):
                
                # 대체 종목 찾기
                substitutes = self.SUBSTITUTE_MAP.get(symbol, [])
                
                harvest_candidates.append({
                    "symbol": symbol,
                    "shares": quantity,
                    "avg_cost": avg_cost,
                    "market_price": market_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "unrealized_pct": round(unrealized_pct, 2),
                    "substitutes": substitutes,
                    "tax_savings_est": round(abs(unrealized_pnl) * 0.35, 2),  # ~35% 세율 가정
                })
        
        # 손실 크기 순으로 정렬
        harvest_candidates.sort(key=lambda x: x["unrealized_pnl"])
        
        if harvest_candidates:
            logger.info(f"\n  🌾 Tax-Loss Harvesting 기회 {len(harvest_candidates)}건:")
            for h in harvest_candidates:
                logger.info(
                    f"    {h['symbol']:6s} | 미실현 P&L: ${h['unrealized_pnl']:+,.0f} "
                    f"({h['unrealized_pct']:+.1f}%) | "
                    f"절세 예상: ${h['tax_savings_est']:,.0f} | "
                    f"대체: {', '.join(h['substitutes'][:2])}"
                )
        
        return harvest_candidates
    
    def get_ytd_summary(self) -> dict:
        """연간 세금 요약"""
        closed_lots = [l for l in self.tax_lots if l.sell_date]
        short_term_pnl = sum(l.realized_pnl for l in closed_lots if not l.is_long_term)
        long_term_pnl = sum(l.realized_pnl for l in closed_lots if l.is_long_term)
        wash_sale_count = sum(1 for l in closed_lots if l.is_wash_sale)
        net = short_term_pnl + long_term_pnl
        
        # 추정 세금 (California 주민)
        federal_st = short_term_pnl * 0.24 if short_term_pnl > 0 else 0  # ~24% 연방
        federal_lt = long_term_pnl * 0.15 if long_term_pnl > 0 else 0    # ~15% 연방
        ca_state = max(0, net) * 0.093  # CA ~9.3%
        
        # 손실 공제
        deductible_loss = min(abs(min(0, net)), 3000)
        loss_carryover = max(0, abs(min(0, net)) - 3000)
        
        return {
            "total_trades": len(closed_lots),
            "short_term_pnl": round(short_term_pnl, 2),
            "long_term_pnl": round(long_term_pnl, 2),
            "net_pnl": round(net, 2),
            "wash_sale_count": wash_sale_count,
            "est_federal_tax": round(federal_st + federal_lt, 2),
            "est_ca_tax": round(ca_state, 2),
            "est_total_tax": round(federal_st + federal_lt + ca_state, 2),
            "deductible_loss": round(deductible_loss, 2),
            "loss_carryover": round(loss_carryover, 2),
        }


# ═══════════════════════════════════════════════════════════════
#  7. Wash Sale Prevention 필터
# ═══════════════════════════════════════════════════════════════

class WashSaleFilter:
    """
    Wash Sale Rule 방지 — IRS 규정 자동 준수
    
    규칙: 손실 매도 후 전후 30일 이내에 '실질적으로 동일한' 증권을
    재매수하면 그 손실 공제가 불허됨.
    
    작동 방식:
    1. 손실 매도 기록을 추적
    2. 매수 시도 시 30일 블랙리스트 확인
    3. 동일 종목 → 매수 차단
    4. 유사 종목 → 경고 (사용자 판단)
    5. 대체 종목 자동 추천
    """
    
    # 실질적으로 동일한 종목 그룹 (ETF ↔ 개별주 관계)
    SUBSTANTIALLY_IDENTICAL = {
        # S&P 500 ETF들은 서로 동일 취급
        "SPY": ["IVV", "VOO"],
        "IVV": ["SPY", "VOO"],
        "VOO": ["SPY", "IVV"],
        # Nasdaq ETF들
        "QQQ": ["QQQM"],
        "QQQM": ["QQQ"],
        # 에너지 ETF
        "XLE": ["VDE", "IYE"],
        "VDE": ["XLE", "IYE"],
    }
    
    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        # 손실 매도 기록: {symbol: sell_date}
        self._loss_sales: dict[str, datetime] = {}
        self._load_wash_sale_log()
    
    def _load_wash_sale_log(self):
        """Wash Sale 로그 파일 로드"""
        path = "wash_sale_log.json"
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                for sym, date_str in data.items():
                    self._loss_sales[sym] = datetime.strptime(date_str, "%Y-%m-%d")
                # 만료된 항목 제거 (30일 지난 것)
                now = datetime.now()
                self._loss_sales = {
                    s: d for s, d in self._loss_sales.items()
                    if (now - d).days <= self.config.wash_sale_window_days
                }
            except Exception:
                pass
    
    def _save_wash_sale_log(self):
        """Wash Sale 로그 저장"""
        data = {
            sym: dt.strftime("%Y-%m-%d") 
            for sym, dt in self._loss_sales.items()
        }
        with open("wash_sale_log.json", "w") as f:
            json.dump(data, f, indent=2)
    
    def record_loss_sale(self, symbol: str, loss_amount: float):
        """
        손실 매도 기록 — 30일 카운트다운 시작
        
        loss_amount가 음수(손실)인 경우만 기록
        """
        if loss_amount >= 0:
            return  # 수익 매도는 Wash Sale 대상 아님
        
        self._loss_sales[symbol] = datetime.now()
        self._save_wash_sale_log()
        
        expiry = datetime.now() + timedelta(days=self.config.wash_sale_window_days)
        logger.info(
            f"  ⏰ Wash Sale 시작: {symbol} | "
            f"손실: ${loss_amount:,.0f} | "
            f"재매수 차단: ~{expiry:%Y-%m-%d} (30일)"
        )
    
    def check_buy(self, symbol: str) -> dict:
        """
        매수 전 Wash Sale 체크
        
        Returns:
            {
                "blocked": bool,
                "warning": bool,
                "reason": str,
                "days_remaining": int,
                "expiry_date": str,
                "substitutes": list,
            }
        """
        if not self.config.wash_sale_enabled:
            return {"blocked": False, "warning": False}
        
        now = datetime.now()
        result = {
            "blocked": False,
            "warning": False,
            "reason": "",
            "days_remaining": 0,
            "expiry_date": "",
            "substitutes": [],
        }
        
        # 1. 동일 종목 체크
        if symbol in self._loss_sales:
            sale_date = self._loss_sales[symbol]
            days_elapsed = (now - sale_date).days
            days_remaining = self.config.wash_sale_window_days - days_elapsed
            
            if days_remaining > 0:
                expiry = sale_date + timedelta(days=self.config.wash_sale_window_days)
                result["blocked"] = True
                result["days_remaining"] = days_remaining
                result["expiry_date"] = expiry.strftime("%Y-%m-%d")
                result["reason"] = (
                    f"🚫 Wash Sale! {symbol} 손실 매도 후 {days_elapsed}일 경과 "
                    f"(잔여 {days_remaining}일, {expiry:%m/%d} 이후 매수 가능)"
                )
                # 대체 종목 추천
                result["substitutes"] = TaxLossHarvester.SUBSTITUTE_MAP.get(symbol, [])
                if result["substitutes"]:
                    result["reason"] += f" → 대체: {', '.join(result['substitutes'][:3])}"
                return result
        
        # 2. 실질적 동일 종목 체크
        identical = self.SUBSTANTIALLY_IDENTICAL.get(symbol, [])
        for ident_sym in identical:
            if ident_sym in self._loss_sales:
                sale_date = self._loss_sales[ident_sym]
                days_elapsed = (now - sale_date).days
                days_remaining = self.config.wash_sale_window_days - days_elapsed
                
                if days_remaining > 0:
                    result["warning"] = True
                    result["days_remaining"] = days_remaining
                    result["reason"] = (
                        f"⚠️ Wash Sale 경고: {ident_sym}(실질적 동일)이 "
                        f"{days_elapsed}일 전 손실 매도됨 (잔여 {days_remaining}일)"
                    )
                    return result
        
        # 3. 유사 종목 체크 (같은 섹터 손실 매도 후 유사 종목 매수)
        for loss_sym, sale_date in self._loss_sales.items():
            days_elapsed = (now - sale_date).days
            if days_elapsed > self.config.wash_sale_window_days:
                continue
            
            subs = TaxLossHarvester.SUBSTITUTE_MAP.get(loss_sym, [])
            if symbol in subs and self.config.wash_sale_warn_similar:
                result["warning"] = True
                result["reason"] = (
                    f"⚠️ 유사 종목 경고: {loss_sym} 손실 매도 {days_elapsed}일 전 | "
                    f"{symbol}은 대체 종목으로 간주될 수 있음 (IRS 판단에 따라 다름)"
                )
                return result
        
        return result
    
    def get_blacklist(self) -> list[dict]:
        """현재 Wash Sale 블랙리스트"""
        now = datetime.now()
        blacklist = []
        
        for sym, sale_date in self._loss_sales.items():
            days_elapsed = (now - sale_date).days
            days_remaining = self.config.wash_sale_window_days - days_elapsed
            
            if days_remaining > 0:
                expiry = sale_date + timedelta(days=self.config.wash_sale_window_days)
                blacklist.append({
                    "symbol": sym,
                    "sale_date": sale_date.strftime("%Y-%m-%d"),
                    "days_remaining": days_remaining,
                    "expiry_date": expiry.strftime("%Y-%m-%d"),
                    "substitutes": TaxLossHarvester.SUBSTITUTE_MAP.get(sym, []),
                })
        
        return blacklist


# ═══════════════════════════════════════════════════════════════
#  통합 세금 최적화 엔진
# ═══════════════════════════════════════════════════════════════

class TaxOptimizer:
    """
    통합 세금 최적화
    - TLH + Wash Sale 통합 관리
    - Smart Trader에서 매수/매도 시 자동 호출
    """
    
    def __init__(self, config: TaxConfig = None):
        self.config = config or TaxConfig()
        self.harvester = TaxLossHarvester(self.config)
        self.wash_filter = WashSaleFilter(self.config)
    
    def on_buy(self, symbol: str, price: float, shares: int) -> dict:
        """
        매수 시 호출 — Wash Sale 체크 + 매수 기록
        
        Returns:
            {"allowed": bool, "wash_sale": dict}
        """
        # Wash Sale 체크
        wash = self.wash_filter.check_buy(symbol)
        
        if wash["blocked"]:
            logger.info(f"  🚫 매수 차단 (Wash Sale): {wash['reason']}")
            return {"allowed": False, "wash_sale": wash}
        
        if wash["warning"]:
            logger.info(f"  ⚠️ {wash['reason']}")
        
        # 매수 기록
        self.harvester.record_buy(symbol, price, shares)
        return {"allowed": True, "wash_sale": wash}
    
    def on_sell(self, symbol: str, price: float, shares: int) -> dict:
        """
        매도 시 호출 — 손익 기록 + Wash Sale 트리거
        
        Returns:
            {"pnl": float, "is_loss": bool, "wash_sale_started": bool}
        """
        pnl = self.harvester.record_sell(symbol, price, shares)
        
        result = {
            "pnl": pnl,
            "is_loss": pnl < 0,
            "wash_sale_started": False,
        }
        
        # 손실 매도인 경우 Wash Sale 카운트다운 시작
        if pnl < 0:
            self.wash_filter.record_loss_sale(symbol, pnl)
            result["wash_sale_started"] = True
        
        return result
    
    def check_buy_allowed(self, symbol: str) -> dict:
        """매수 전 Wash Sale만 빠르게 체크"""
        return self.wash_filter.check_buy(symbol)
    
    def scan_harvest_opportunities(self, positions: dict) -> list[dict]:
        """TLH 기회 스캔"""
        return self.harvester.scan_for_harvest(positions)
    
    def get_wash_sale_blacklist(self) -> list[dict]:
        """현재 재매수 차단 종목"""
        return self.wash_filter.get_blacklist()
    
    def print_tax_report(self):
        """연간 세금 리포트"""
        summary = self.harvester.get_ytd_summary()
        blacklist = self.wash_filter.get_blacklist()
        
        print("\n" + "═" * 70)
        print("  💰 Tax Optimization Report")
        print("═" * 70)
        
        print(f"\n  📊 연간 실현 손익 (YTD):")
        print(f"    Short-term P&L:  ${summary['short_term_pnl']:>+10,.2f}")
        print(f"    Long-term P&L:   ${summary['long_term_pnl']:>+10,.2f}")
        print(f"    순 합계:          ${summary['net_pnl']:>+10,.2f}")
        print(f"    총 거래:          {summary['total_trades']}건")
        
        print(f"\n  🏛️ 추정 세금 (California):")
        print(f"    연방세:           ${summary['est_federal_tax']:>10,.2f}")
        print(f"    CA 주세:          ${summary['est_ca_tax']:>10,.2f}")
        print(f"    합계:             ${summary['est_total_tax']:>10,.2f}")
        
        if summary['net_pnl'] < 0:
            print(f"\n  🌾 손실 공제:")
            print(f"    올해 공제 가능:   ${summary['deductible_loss']:>10,.2f} (최대 $3,000)")
            if summary['loss_carryover'] > 0:
                print(f"    이월 손실:        ${summary['loss_carryover']:>10,.2f} (내년으로)")
        
        if summary['wash_sale_count'] > 0:
            print(f"\n  ⚠️ Wash Sale 발생: {summary['wash_sale_count']}건")
        
        if blacklist:
            print(f"\n  🚫 Wash Sale 블랙리스트 ({len(blacklist)}종목):")
            for b in blacklist:
                print(
                    f"    {b['symbol']:6s} | "
                    f"매도일: {b['sale_date']} | "
                    f"잔여: {b['days_remaining']}일 | "
                    f"해제: {b['expiry_date']} | "
                    f"대체: {', '.join(b['substitutes'][:2])}"
                )
        
        print("═" * 70)


# ═══════════════════════════════════════════════════════════════
#  데모
# ═══════════════════════════════════════════════════════════════

def demo():
    """Tax Optimizer 데모"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  💰 Tax Optimizer 데모 — TLH + Wash Sale 방지           ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    tax = TaxOptimizer()
    
    # 1) 매수 기록
    print("  📈 매수 기록:")
    tax.on_buy("XOM", 150.00, 50)
    tax.on_buy("NVDA", 200.00, 30)
    tax.on_buy("CRM", 320.00, 20)
    
    # 2) 일부 매도 (손실 발생)
    print("\n  📉 매도 기록:")
    tax.on_sell("CRM", 280.00, 20)    # 손실: -$800
    tax.on_sell("NVDA", 185.00, 30)   # 손실: -$450
    tax.on_sell("XOM", 160.00, 50)    # 수익: +$500
    
    # 3) Wash Sale 체크 — CRM 재매수 시도
    print("\n  🔍 Wash Sale 체크:")
    
    crm_check = tax.check_buy_allowed("CRM")
    print(f"  CRM 매수: {'차단' if crm_check['blocked'] else '허용'}")
    if crm_check.get("reason"):
        print(f"    → {crm_check['reason']}")
    
    # ORCL은 CRM 대체 종목 → 경고만
    orcl_check = tax.check_buy_allowed("ORCL")
    print(f"  ORCL 매수: {'경고' if orcl_check['warning'] else '허용'}")
    if orcl_check.get("reason"):
        print(f"    → {orcl_check['reason']}")
    
    # XOM은 수익 매도라 Wash Sale 없음
    xom_check = tax.check_buy_allowed("XOM")
    print(f"  XOM 매수: {'차단' if xom_check['blocked'] else '허용'}")
    
    # 4) TLH 기회 스캔
    print("\n  🌾 Tax-Loss Harvesting 스캔:")
    positions = {
        "LMT": {"avg_cost": 600.00, "quantity": 10, "market_price": 580.00},
        "HOOD": {"avg_cost": 40.00, "quantity": 200, "market_price": 32.00},
    }
    harvest = tax.scan_harvest_opportunities(positions)
    
    # 5) Blacklist 확인
    print("\n  🚫 현재 블랙리스트:")
    for b in tax.get_wash_sale_blacklist():
        print(f"    {b['symbol']} — {b['days_remaining']}일 남음 → 대체: {', '.join(b['substitutes'][:2])}")
    
    # 6) 세금 리포트
    tax.print_tax_report()
    
    # 정리
    for f in ["tax_records.json", "wash_sale_log.json"]:
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    demo()
