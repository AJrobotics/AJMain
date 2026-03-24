"""
Trader Module — CashCow only.
Manages IB Smart Trader + Day Trader locally (no SSH needed).
Provides status, log tail, daily picks, portfolio, start/stop via REST API.
"""

import json
import os
import re
import subprocess
import sys
import logging
import threading
import time

from datetime import datetime as _dt, timedelta

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

# Paths on CashCow
TRADER_BASE = os.path.expanduser("~/ib_smart_trader/ib_smart_trader")
TRADER_LOG_DIR = os.path.expanduser("~/ib_smart_trader/logs")
TRADER_RUN_SCRIPT = os.path.join(TRADER_BASE, "run.py")
DAILY_PICKS_PATH = os.path.join(TRADER_BASE, "daily_picks.json")
CONFIG_PATH = os.path.join(TRADER_BASE, "config.json")

# IB Gateway ports
IB_PAPER_PORT = 7497
IB_LIVE_PORT = 7496

# Trader uses its own venv (has ib_insync, pandas, numpy)
TRADER_VENV_PYTHON = os.path.expanduser("~/trader_venv/bin/python")


class TraderModule:
    name = "trader"

    def __init__(self):
        self._portfolio_cache = {"data": None, "ts": 0}
        self._portfolio_lock = threading.Lock()

    def _get_ib_portfolio(self, port: int = IB_PAPER_PORT) -> dict:
        """Connect to IB Gateway and fetch account + portfolio data."""
        with self._portfolio_lock:
            # Cache for 15 seconds
            if self._portfolio_cache["data"] and time.time() - self._portfolio_cache["ts"] < 15:
                return self._portfolio_cache["data"]

        try:
            import asyncio
            # ib_insync needs an event loop; Flask worker threads don't have one
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            from ib_insync import IB
            ib = IB()
            ib.connect("127.0.0.1", port, clientId=99, timeout=5)

            # Account summary
            account_values = ib.accountSummary()
            acct = {}
            for av in account_values:
                if av.tag in ("NetLiquidation", "TotalCashValue", "GrossPositionValue",
                              "UnrealizedPnL", "RealizedPnL", "BuyingPower"):
                    acct[av.tag] = float(av.value)

            # Portfolio positions
            positions = []
            for item in ib.portfolio():
                positions.append({
                    "symbol": item.contract.symbol,
                    "secType": item.contract.secType,
                    "quantity": float(item.position),
                    "avgCost": round(item.averageCost, 2),
                    "marketPrice": round(item.marketPrice, 2),
                    "marketValue": round(item.marketValue, 2),
                    "unrealizedPNL": round(item.unrealizedPNL, 2),
                    "realizedPNL": round(item.realizedPNL, 2),
                })

            # Today's executions & commissions
            from datetime import datetime as _dt
            today_str = _dt.now().strftime("%Y%m%d")
            fills = ib.fills()
            total_commission = 0.0
            total_trades = 0
            trade_details = []
            for fill in fills:
                exec_time = fill.execution.time
                # Filter today's fills
                exec_date = exec_time.strftime("%Y%m%d") if hasattr(exec_time, 'strftime') else str(exec_time)[:8].replace('-','')
                if today_str not in exec_date:
                    continue
                comm = fill.commissionReport.commission if fill.commissionReport else 0
                if comm and comm < 1e9:  # IB returns 1e10 for unknown
                    total_commission += comm
                total_trades += 1
                trade_details.append({
                    "symbol": fill.contract.symbol,
                    "side": fill.execution.side,
                    "shares": fill.execution.shares,
                    "price": round(fill.execution.avgPrice, 2),
                    "commission": round(comm, 2) if comm < 1e9 else 0,
                    "time": exec_time.strftime("%H:%M:%S") if hasattr(exec_time, 'strftime') else str(exec_time),
                })

            ib.disconnect()

            result = {
                "connected": True,
                "account": acct,
                "positions": positions,
                "position_count": len(positions),
                "today_trades": total_trades,
                "today_commission": round(total_commission, 2),
                "trade_details": trade_details,
            }

            with self._portfolio_lock:
                self._portfolio_cache = {"data": result, "ts": time.time()}

            return result

        except Exception as e:
            logger.warning("IB portfolio fetch failed: %s", e)
            return {
                "connected": False,
                "error": str(e),
                "account": {},
                "positions": [],
                "position_count": 0,
            }

    def _get_market_status(self) -> dict:
        """Return current US market status based on Eastern Time."""
        try:
            import pytz
            et = pytz.timezone("US/Eastern")
            now_et = _dt.now(et)
        except ImportError:
            # Fallback: assume UTC-5 (EST) if pytz not available
            from datetime import timezone, timedelta
            et_offset = timezone(timedelta(hours=-5))
            now_et = _dt.now(et_offset)

        is_weekday = now_et.weekday() < 5
        market_hour = now_et.hour + now_et.minute / 60.0

        if not is_weekday:
            status = "closed"
        elif 9.5 <= market_hour < 16.0:
            status = "trading"
        elif 4.0 <= market_hour < 9.5:
            status = "pre-market"
        elif 16.0 <= market_hour < 20.0:
            status = "after-hours"
        else:
            status = "closed"

        return {
            "market_status": status,
            "market_time": now_et.strftime("%I:%M %p ET"),
            "market_open": status == "trading",
            "is_weekday": is_weekday,
        }

    def _is_running(self) -> dict:
        """Check if Smart Trader / Day Trader process is running."""
        try:
            proc = subprocess.run(
                ["pgrep", "-af", "run.py"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l for l in proc.stdout.strip().splitlines()
                     if "run.py" in l and "pgrep" not in l]
            if not lines:
                return {"running": False, "mode": "STOPPED", "pid": None,
                        "trader_type": "none"}

            # Could have smart, day, and politician trader running
            result = {"running": True, "processes": []}
            for line in lines:
                parts = line.split()
                pid = parts[0] if parts else None
                mode = "AUTO" if "--auto" in line else "ALERT"
                if "--politician" in line:
                    trader_type = "politician"
                elif "--day" in line:
                    trader_type = "day"
                else:
                    trader_type = "smart"
                result["processes"].append({
                    "pid": pid, "mode": mode, "trader_type": trader_type,
                    "cmdline": line,
                })

            # Primary process info (first found)
            primary = result["processes"][0]
            result["mode"] = primary["mode"]
            result["pid"] = primary["pid"]
            result["trader_type"] = primary["trader_type"]

            # Check which traders are running
            types = [p["trader_type"] for p in result["processes"]]
            result["smart_running"] = "smart" in types
            result["day_running"] = "day" in types
            result["politician_running"] = "politician" in types

            return result
        except Exception as e:
            logger.error("Process check failed: %s", e)
            return {"running": False, "mode": "ERROR", "pid": None, "error": str(e)}

    def _read_json(self, path: str) -> dict | None:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _tail_log(self, lines: int = 30, trader_type: str = "smart") -> list[str]:
        """Read last N lines from the trader log."""
        if trader_type == "politician":
            candidates = ["politician_trader_stdout.log", "politician_trader.log"]
        elif trader_type == "day":
            candidates = ["day_trader_stdout.log", "day_trader.log"]
        else:
            candidates = ["trader_stdout.log", "smart_trader.log"]

        for name in candidates:
            path = os.path.join(TRADER_LOG_DIR, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                try:
                    with open(path, "r", errors="replace") as f:
                        all_lines = f.readlines()
                        return [l.rstrip() for l in all_lines[-lines:]]
                except Exception:
                    continue
        # Fallback: smart_trader.log in base dir (only for smart trader)
        if trader_type == "smart":
            path = os.path.join(TRADER_BASE, "smart_trader.log")
            if os.path.isfile(path):
                try:
                    with open(path, "r", errors="replace") as f:
                        all_lines = f.readlines()
                        return [l.rstrip() for l in all_lines[-lines:]]
                except Exception:
                    pass
        return []

    def register(self, app):
        bp = Blueprint("trader", __name__)

        @bp.route("/api/trader/status")
        def trader_status():
            proc = self._is_running()
            market = self._get_market_status()
            log_lines = self._tail_log(30, "smart")
            day_log_lines = self._tail_log(30, "day")
            politician_log_lines = self._tail_log(30, "politician")
            picks = self._read_json(DAILY_PICKS_PATH)
            config = self._read_json(CONFIG_PATH)
            return jsonify({
                **proc,
                **market,
                "log_lines": log_lines,
                "day_log_lines": day_log_lines,
                "politician_log_lines": politician_log_lines,
                "daily_picks": picks,
                "config": config,
            })

        @bp.route("/api/trader/portfolio")
        def trader_portfolio():
            port = request.args.get("port", IB_PAPER_PORT, type=int)
            return jsonify(self._get_ib_portfolio(port))

        @bp.route("/api/trader/start", methods=["POST"])
        def trader_start():
            data = request.json or {}
            auto = data.get("auto", True)
            port = data.get("port", 7497)
            day = data.get("day", False)
            politician = data.get("politician", False)

            # Check if this specific type is already running
            proc = self._is_running()
            if proc.get("running"):
                if politician:
                    target_type = "politician"
                elif day:
                    target_type = "day"
                else:
                    target_type = "smart"
                if target_type == "politician" and proc.get("politician_running"):
                    return jsonify({"error": "Politician Trader already running"}), 409
                if target_type == "day" and proc.get("day_running"):
                    return jsonify({"error": "Day Trader already running"}), 409
                if target_type == "smart" and proc.get("smart_running"):
                    return jsonify({"error": "Smart Trader already running"}), 409

            # Use trader_venv python (has ib_insync, pandas, numpy)
            python_exec = TRADER_VENV_PYTHON if os.path.exists(TRADER_VENV_PYTHON) else sys.executable
            cmd = [python_exec, TRADER_RUN_SCRIPT]
            if politician:
                cmd.append("--politician")
            elif day:
                cmd.append("--day")
            if auto:
                cmd.append("--auto")
            cmd.extend(["--port", str(port)])

            if politician:
                log_file = "politician_trader_stdout.log"
            elif day:
                log_file = "day_trader_stdout.log"
            else:
                log_file = "trader_stdout.log"
            try:
                subprocess.Popen(
                    cmd,
                    stdout=open(os.path.join(TRADER_LOG_DIR, log_file), "a"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                if politician:
                    label = "Politician Trader"
                elif day:
                    label = "Day Trader"
                else:
                    label = "Smart Trader"
                return jsonify({"success": True, "message": f"{label} started"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @bp.route("/api/trader/stop", methods=["POST"])
        def trader_stop():
            data = request.json or {}
            target = data.get("type", "all")  # "smart", "day", "politician", or "all"
            try:
                if target == "all":
                    subprocess.run(["pkill", "-f", "run.py"], timeout=5)
                elif target == "politician":
                    subprocess.run(["pkill", "-f", "run.py --politician"], timeout=5)
                elif target == "day":
                    subprocess.run(["pkill", "-f", "run.py --day"], timeout=5)
                else:
                    # Kill smart trader only (run.py without --day/--politician)
                    proc = self._is_running()
                    for p in proc.get("processes", []):
                        if p["trader_type"] == "smart":
                            subprocess.run(["kill", p["pid"]], timeout=5)
                return jsonify({"success": True, "message": f"{target} trader stopped"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @bp.route("/api/trader/config", methods=["GET", "POST"])
        def trader_config():
            config_file = os.path.join(TRADER_BASE, "day_config.json")
            if request.method == "GET":
                return jsonify(self._read_json(config_file) or {"day_capital": 75000})
            data = request.json or {}
            # Load existing or create new
            cfg = self._read_json(config_file) or {}
            if "day_capital" in data:
                cfg["day_capital"] = int(data["day_capital"])
            with open(config_file, "w") as f:
                json.dump(cfg, f, indent=2)
            logger.info("Day trader config updated: %s", cfg)
            return jsonify({"success": True, "config": cfg})

        @bp.route("/api/trader/logs")
        def trader_logs():
            lines = request.args.get("lines", 50, type=int)
            trader_type = request.args.get("type", "smart")
            return jsonify({"log_lines": self._tail_log(lines, trader_type)})

        @bp.route("/api/trader/today")
        def trader_today():
            """Parse today's trades from both Day Trader and Smart Trader logs."""
            date_str = request.args.get("date", None)
            today_str = _dt.now().strftime("%Y-%m-%d")

            # If requesting a past date, load from saved JSON
            if date_str and date_str != today_str:
                saved = self._load_saved_trades(date_str)
                if saved:
                    return jsonify(saved)
                return jsonify({"day_trades": [], "smart_trades": [], "date": date_str, "saved": False})

            # Parse live from logs
            day_trades = self._parse_trades_from_log("day", today_str)
            smart_trades = self._parse_trades_from_log("smart", today_str)
            politician_trades = self._parse_trades_from_log("politician", today_str)
            result = {
                "day_trades": day_trades,
                "smart_trades": smart_trades,
                "politician_trades": politician_trades,
                "date": today_str,
            }

            # Auto-save to JSON (overwrite today's file)
            self._save_trades(today_str, result)

            return jsonify(result)

        @bp.route("/api/trader/history")
        def trader_history():
            """List available saved trade dates."""
            history_dir = os.path.join(TRADER_LOG_DIR, "trade_history")
            dates = []
            if os.path.isdir(history_dir):
                for f in sorted(os.listdir(history_dir), reverse=True):
                    if f.endswith(".json"):
                        dates.append(f.replace(".json", ""))
            return jsonify({"dates": dates})

        @bp.route("/api/trader/intraday/<symbol>")
        def trader_intraday(symbol):
            """Fetch today's 5-min bars for a symbol from IB Gateway."""
            port = request.args.get("port", IB_PAPER_PORT, type=int)
            bars = self._fetch_intraday_bars(symbol, port)
            return jsonify(bars)

        @bp.route("/api/trader/politician/disclosures")
        def politician_disclosures():
            """Fetch recent congressional trade disclosures for the UI."""
            try:
                import sys as _sys
                ib_trader_path = os.path.join(TRADER_BASE)
                if ib_trader_path not in _sys.path:
                    _sys.path.insert(0, ib_trader_path)
                from politician_data import PoliticianDataFetcher, PoliticianDataConfig
                fetcher = PoliticianDataFetcher(PoliticianDataConfig(
                    cache_dir=os.path.join(TRADER_BASE, "politician_cache"),
                ))
                disclosures = fetcher.fetch_recent_disclosures()
                actionable = fetcher.filter_actionable_disclosures(disclosures)
                profiles = fetcher.build_politician_profiles()
                result = []
                for d in actionable:
                    profile = profiles.get(d.politician_name)
                    result.append({
                        "politician": d.politician_name,
                        "party": d.party,
                        "chamber": d.chamber,
                        "symbol": d.symbol,
                        "type": d.disclosure_type.value,
                        "amount": d.midpoint_amount,
                        "transaction_date": d.transaction_date,
                        "disclosure_date": d.disclosure_date,
                        "delay_days": d.delay_days,
                        "committees": d.committees,
                        "sector": d.sector,
                        "reliability": round(profile.reliability_score, 3) if profile else 0,
                    })
                return jsonify({"disclosures": result, "total": len(result)})
            except Exception as e:
                logger.warning("Politician disclosures API error: %s", e)
                return jsonify({"disclosures": [], "total": 0, "error": str(e)})

        @bp.route("/api/trader/politician/news")
        def politician_news():
            """Fetch political news from RSS feeds."""
            try:
                import sys as _sys
                ib_trader_path = os.path.join(TRADER_BASE)
                if ib_trader_path not in _sys.path:
                    _sys.path.insert(0, ib_trader_path)
                from politician_data import PoliticianDataFetcher, PoliticianDataConfig
                max_items = request.args.get("max", 10, type=int)
                fetcher = PoliticianDataFetcher(PoliticianDataConfig(
                    cache_dir=os.path.join(TRADER_BASE, "politician_cache"),
                    refresh_interval_min=5,
                ))
                articles = fetcher.fetch_political_news(max_items=max_items)
                return jsonify({"articles": articles, "total": len(articles)})
            except Exception as e:
                logger.warning("Politician news API error: %s", e)
                return jsonify({"articles": [], "total": 0, "error": str(e)})

        app.register_blueprint(bp)

    # ── Log Parsing for Today's Trades ──────────────────────────

    def _parse_trades_from_log(self, trader_type: str, date_str: str) -> list:
        """
        Parse a trader log file and extract today's executed trades with
        strategy signal context.

        Day Trader log format:
          🎯 SYMBOL | BUY/SELL/HOLD | 합의: +0.xxx | BUY:n SELL:n
                STRATEGY_NAME     → BUY/SELL/HOLD (xx%) reason
          ✅ BUY 주문 전송! SYMBOL x{shares} | 주문ID: xxx | 상태: xxx
          ✅ SELL 주문 전송! SYMBOL x{shares} | 주문ID: xxx

        Smart Trader log format:
          🎯 SYMBOL 앙상블 | 합의: +0.xxx | BUY:n SELL:n HOLD:n
                STRATEGY_NAME      → BUY/SELL (xx%) reason
          ✅ 주문 전송! BUY/SELL SYMBOL x{qty} | 주문 ID: xxx | 상태: xxx
        """
        if trader_type == "politician":
            candidates = ["politician_trader_stdout.log"]
        elif trader_type == "day":
            candidates = ["day_trader_stdout.log"]
        else:
            candidates = ["trader_stdout.log"]

        log_path = None
        for name in candidates:
            p = os.path.join(TRADER_LOG_DIR, name)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                log_path = p
                break

        if not log_path:
            return []

        try:
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
        except Exception:
            return []

        trades = []
        # We scan all lines looking for order execution lines, then look
        # backwards for the strategy signals and ensemble decision.

        # Patterns for executed orders
        if trader_type == "day":
            # ✅ BUY 주문 전송! SYMBOL x{shares} | 주문ID: {id} | 상태: {status}
            order_re = re.compile(
                r"✅\s+(BUY|SELL)\s+주문\s+전송!\s+(\w+)\s+x(\d+)\s*\|\s*주문ID:\s*(\d+)"
            )
        else:
            # ✅ 주문 전송! BUY/SELL SYMBOL x{qty} | 주문 ID: {id} | 상태: {status}
            order_re = re.compile(
                r"✅\s+주문\s+전송!\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s*\|\s*주문\s*ID:\s*(\d+)"
            )

        # Also capture ALERT mode signals (no actual order placed)
        if trader_type == "day":
            alert_re = re.compile(
                r"🔔\s+\[ALERT\]\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s+@\s+\$([0-9.]+)"
            )
        else:
            alert_re = None

        # Ensemble decision line
        ensemble_re = re.compile(
            r"🎯\s+(\w+).*합의:\s+([+-]?[0-9.]+)\s*\|\s*BUY:(\d+)\s+SELL:(\d+)"
        )
        # Individual strategy signal line
        signal_re = re.compile(
            r"^\s+([\w_]+)\s+→\s+(BUY|SELL|HOLD)\s+\((\d+)%\)\s+(.*)"
        )
        # Timestamp patterns (various logging formats)
        ts_re = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
        # Time-only pattern: "11:40:11  ✅ BUY..." (day trader format)
        time_only_re = re.compile(r"^(\d{2}:\d{2}:\d{2})\s")

        for i, line in enumerate(all_lines):
            # Check if this line is an executed order
            m_order = order_re.search(line)
            m_alert = alert_re.search(line) if alert_re else None

            if not m_order and not m_alert:
                continue

            if m_order:
                side = m_order.group(1)
                symbol = m_order.group(2)
                quantity = int(m_order.group(3))
                order_id = m_order.group(4)
                mode = "AUTO"
            else:
                side = m_alert.group(1)
                symbol = m_alert.group(2)
                quantity = int(m_alert.group(3))
                order_id = None
                mode = "ALERT"

            # Extract timestamp from this line or nearby lines
            trade_time = None
            for check_line in [line] + all_lines[max(0, i-5):i]:
                # Try full date+time first
                tm = ts_re.search(check_line)
                if tm:
                    trade_date = tm.group(1)
                    trade_time = tm.group(2)
                    if trade_date != date_str:
                        trade_time = None
                    break
                # Try time-only (day trader log: "11:40:11  ✅ BUY...")
                tm2 = time_only_re.search(check_line)
                if tm2:
                    trade_time = tm2.group(1)
                    break

            if trade_time is None:
                trade_time = "unknown"

            # Look backwards for strategy signals (up to 15 lines back)
            signals = []
            consensus_score = None
            for j in range(max(0, i - 15), i):
                back_line = all_lines[j]

                em = ensemble_re.search(back_line)
                if em and em.group(1) == symbol:
                    consensus_score = float(em.group(2))

                sm = signal_re.search(back_line)
                if sm:
                    signals.append({
                        "strategy": sm.group(1),
                        "signal": sm.group(2),
                        "confidence": int(sm.group(3)),
                        "reason": sm.group(4).strip(),
                    })

            # Extract price from nearby lines (look backwards from order line)
            trade_price = None
            for j in range(i, max(0, i - 10), -1):
                pline = all_lines[j]
                # "📏 사이징: 39주 × $380.16 = $14,826.24"
                if '사이징' in pline:
                    pm = re.search(r'[×x]\s*\$([0-9.]+)', pline)
                    if pm:
                        trade_price = float(pm.group(1))
                        break
                # "VWAP $380.62 저항" or "VWAP $380.62 지지"
                if 'VWAP' in pline and '$' in pline:
                    pm = re.search(r'VWAP\s+\$([0-9.]+)', pline)
                    if pm:
                        trade_price = float(pm.group(1))
                        break
                # "SYMBOL | $xxx.xx" (smart trader format)
                if symbol in pline and '|' in pline and '$' in pline:
                    pm = re.search(r'\$([0-9]+\.[0-9]{2})', pline)
                    if pm:
                        trade_price = float(pm.group(1))

            trades.append({
                "time": trade_time,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": trade_price,
                "order_id": order_id,
                "mode": mode,
                "consensus_score": consensus_score,
                "signals": signals,
            })

        return trades

    # ── Trade History Save/Load ─────────────────────────────────

    def _save_trades(self, date_str: str, data: dict):
        """Save today's trades to a JSON file for future reference."""
        history_dir = os.path.join(TRADER_LOG_DIR, "trade_history")
        os.makedirs(history_dir, exist_ok=True)
        filepath = os.path.join(history_dir, f"{date_str}.json")
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.warning("Failed to save trades: %s", e)

    def _load_saved_trades(self, date_str: str) -> dict:
        """Load previously saved trades for a given date."""
        filepath = os.path.join(TRADER_LOG_DIR, "trade_history", f"{date_str}.json")
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                data["saved"] = True
                return data
            except Exception as e:
                logger.warning("Failed to load saved trades: %s", e)
        return None

    # ── Intraday Bars from IB ──────────────────────────────────

    def _fetch_intraday_bars(self, symbol: str, port: int = IB_PAPER_PORT) -> list:
        """Connect to IB and fetch today's 5-min bars for a symbol."""
        try:
            import asyncio
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            from ib_insync import IB, Stock
            ib = IB()
            ib.connect("127.0.0.1", port, clientId=98, timeout=5)

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
            )

            ib.disconnect()

            result = []
            for bar in bars:
                result.append({
                    "time": bar.date.strftime("%Y-%m-%d %H:%M:%S")
                            if hasattr(bar.date, "strftime")
                            else str(bar.date),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                })

            return result

        except Exception as e:
            logger.warning("IB intraday fetch failed for %s: %s", symbol, e)
            return []
