"""
Trader Module — CashCow only.
Manages IB Smart Trader + Day Trader locally (no SSH needed).
Provides status, log tail, daily picks, portfolio, start/stop via REST API.
"""

import json
import os
import re
import smtplib
import socket
import subprocess
import sys
import logging
import threading
import time

from datetime import datetime as _dt, timedelta
from email.mime.text import MIMEText

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

# SMS Alert via Verizon email-to-SMS gateway
SMS_PHONE = "6616180571"
SMS_GATEWAY = f"{SMS_PHONE}@vtext.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


class TraderModule:
    name = "trader"

    def __init__(self):
        self._portfolio_cache = {"data": None, "ts": 0}
        self._portfolio_lock = threading.Lock()

        # ── TraderWatcher state ──
        self._watcher_enabled = False
        self._watcher_thread = None
        self._watcher_lock = threading.Lock()
        self._watcher_log = []  # recent events, max 100
        self._watcher_state_file = os.path.join(TRADER_LOG_DIR, "watcher_state.json")
        self._watcher_check_interval = 60  # seconds
        self._max_restarts_per_day = 10
        self._restart_cooldown = 300  # 5 minutes
        self._last_restart_smart = 0  # timestamp
        self._last_restart_day = 0
        self._restart_count_today = 0
        self._restart_date = ""
        self._sms_sent_today = False
        self._sms_date = ""
        self._load_watcher_state()

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

            # Look up purchase dates from trader logs
            position_dates = self._get_position_dates(
                [p["symbol"] for p in positions]
            )
            for p in positions:
                p["purchase_date"] = position_dates.get(p["symbol"], "")

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

    def _get_position_dates(self, symbols: list) -> dict:
        """Parse trader logs to find the most recent BUY date for each symbol."""
        if not symbols:
            return {}
        dates = {}  # symbol -> "YYYY-MM-DD"
        # Log files to search (most recent entries win)
        log_files = [
            os.path.expanduser("~/ib_smart_trader/day_trader.log"),
            os.path.expanduser("~/ib_smart_trader/ib_smart_trader/smart_trader.log"),
        ]
        # Dated logs: "2026-03-25 10:11:38,053 [INFO]   ✅ BUY order sent! GOOGL x51"
        buy_re_dated = re.compile(
            r"^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}.*BUY order sent!\s+(\w+)\s+x"
        )
        sym_set = set(symbols)
        for lf in log_files:
            if not os.path.isfile(lf):
                continue
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = buy_re_dated.match(line)
                        if m and m.group(2) in sym_set:
                            dates[m.group(2)] = m.group(1)
            except Exception:
                continue

        # stdout logs have no date prefix: "09:16:51   ✅ BUY order sent! QQQ x25"
        # Use file modification date as fallback
        stdout_logs = [
            os.path.join(TRADER_LOG_DIR, "day_trader_stdout.log"),
            os.path.join(TRADER_LOG_DIR, "trader_stdout.log"),
        ]
        buy_re_short = re.compile(
            r"^\d{2}:\d{2}:\d{2}\s+.*BUY order sent!\s+(\w+)\s+x"
        )
        today_str = _dt.now().strftime("%Y-%m-%d")
        for lf in stdout_logs:
            if not os.path.isfile(lf):
                continue
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = buy_re_short.match(line)
                        if m and m.group(1) in sym_set:
                            # stdout logs are from today's session
                            if m.group(1) not in dates:
                                dates[m.group(1)] = today_str
            except Exception:
                continue
        return dates

    def _build_trade_history(self, days: int = 10) -> list:
        """Parse trader logs for the last N days and build trade history.

        Returns a list of trade rounds: each is a buy matched with sells.
        Cross-references current IB positions to determine hold/sold status.
        """
        cutoff = (_dt.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        order_re = re.compile(
            r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}).*"
            r"(BUY|SELL) order sent!\s+(\w+)\s+x(\d+)"
        )

        # Collect all orders from dated log files
        orders = []  # list of {side, symbol, qty, date, time}
        log_files = [
            os.path.expanduser("~/ib_smart_trader/day_trader.log"),
            os.path.expanduser("~/ib_smart_trader/ib_smart_trader/smart_trader.log"),
        ]
        for lf in log_files:
            if not os.path.isfile(lf):
                continue
            trader_label = "Day" if "day_trader" in lf else "Smart"
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = order_re.match(line)
                        if not m:
                            continue
                        date_str = m.group(1)
                        if date_str < cutoff:
                            continue
                        orders.append({
                            "side": m.group(3),
                            "symbol": m.group(4),
                            "qty": int(m.group(5)),
                            "date": date_str,
                            "time": m.group(2),
                            "trader": trader_label,
                        })
            except Exception:
                continue

        # Also parse stdout logs (today only, no date prefix)
        stdout_logs = [
            (os.path.join(TRADER_LOG_DIR, "day_trader_stdout.log"), "Day"),
            (os.path.join(TRADER_LOG_DIR, "trader_stdout.log"), "Smart"),
        ]
        stdout_re = re.compile(
            r"^(\d{2}:\d{2}:\d{2})\s+.*"
            r"(BUY|SELL) order sent!\s+(\w+)\s+x(\d+)"
        )
        today_str = _dt.now().strftime("%Y-%m-%d")
        for lf, trader_label in stdout_logs:
            if not os.path.isfile(lf):
                continue
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = stdout_re.match(line)
                        if not m:
                            continue
                        orders.append({
                            "side": m.group(2),
                            "symbol": m.group(3),
                            "qty": int(m.group(4)),
                            "date": today_str,
                            "time": m.group(1),
                            "trader": trader_label,
                        })
            except Exception:
                continue

        # Sort by date+time
        orders.sort(key=lambda o: o["date"] + o["time"])

        # Get current positions to determine hold/sold
        current_positions = {}
        try:
            port_data = self._get_ib_portfolio()
            if port_data.get("connected"):
                for p in port_data.get("positions", []):
                    current_positions[p["symbol"]] = {
                        "qty": p["quantity"],
                        "avgCost": p["avgCost"],
                        "marketPrice": p["marketPrice"],
                        "unrealizedPNL": p["unrealizedPNL"],
                    }
        except Exception:
            pass

        # Build trade rounds: match BUYs to SELLs per symbol
        # Track open buy inventory per symbol
        open_buys = {}  # symbol -> [list of {qty_remaining, date, time, trader}]
        trades = []  # completed or open trade rounds

        for order in orders:
            sym = order["symbol"]
            if order["side"] == "BUY":
                if sym not in open_buys:
                    open_buys[sym] = []
                open_buys[sym].append({
                    "qty_remaining": order["qty"],
                    "qty_original": order["qty"],
                    "date": order["date"],
                    "time": order["time"],
                    "trader": order["trader"],
                })
            elif order["side"] == "SELL":
                sell_qty = order["qty"]
                if sym in open_buys and open_buys[sym]:
                    # Match against oldest open buy (FIFO)
                    while sell_qty > 0 and open_buys[sym]:
                        buy = open_buys[sym][0]
                        matched = min(sell_qty, buy["qty_remaining"])
                        buy["qty_remaining"] -= matched
                        sell_qty -= matched
                        if buy["qty_remaining"] <= 0:
                            # Buy fully sold
                            trades.append({
                                "symbol": sym,
                                "qty": buy["qty_original"],
                                "status": "Sold",
                                "buy_date": buy["date"],
                                "buy_time": buy["time"],
                                "sell_date": order["date"],
                                "sell_time": order["time"],
                                "trader": buy["trader"],
                                "pnl": None,
                            })
                            open_buys[sym].pop(0)

        # Remaining open buys = currently held
        for sym, buys in open_buys.items():
            for buy in buys:
                if buy["qty_remaining"] <= 0:
                    continue
                pos = current_positions.get(sym, {})
                pnl = pos.get("unrealizedPNL")
                trades.append({
                    "symbol": sym,
                    "qty": buy["qty_remaining"],
                    "status": "Hold",
                    "buy_date": buy["date"],
                    "buy_time": buy["time"],
                    "sell_date": None,
                    "sell_time": None,
                    "trader": buy["trader"],
                    "pnl": round(pnl, 2) if pnl is not None else None,
                    "avg_cost": pos.get("avgCost"),
                    "mkt_price": pos.get("marketPrice"),
                })

        # Sort: Hold first, then by date descending
        trades.sort(key=lambda t: (0 if t["status"] == "Hold" else 1, t["buy_date"]), reverse=False)
        # Actually reverse so most recent first, but Hold on top
        trades.sort(key=lambda t: (0 if t["status"] == "Hold" else 1, -(int(t["buy_date"].replace("-","")))))

        return trades

    # ── TraderWatcher methods ──

    def _load_watcher_state(self):
        """Load watcher state from disk."""
        try:
            if os.path.isfile(self._watcher_state_file):
                with open(self._watcher_state_file, "r") as f:
                    state = json.load(f)
                self._watcher_enabled = state.get("enabled", False)
                self._restart_count_today = state.get("restart_count_today", 0)
                self._restart_date = state.get("restart_date", "")
                self._last_restart_smart = state.get("last_restart_smart", 0)
                self._last_restart_day = state.get("last_restart_day", 0)
        except Exception:
            pass

    def _save_watcher_state(self):
        """Persist watcher state to disk."""
        try:
            os.makedirs(os.path.dirname(self._watcher_state_file), exist_ok=True)
            state = {
                "enabled": self._watcher_enabled,
                "restart_count_today": self._restart_count_today,
                "restart_date": self._restart_date,
                "last_restart_smart": self._last_restart_smart,
                "last_restart_day": self._last_restart_day,
            }
            with open(self._watcher_state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save watcher state: %s", e)

    def _check_ib_gateway(self, port: int = IB_PAPER_PORT) -> bool:
        """Lightweight TCP check if IB Gateway is listening."""
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.close()
            return True
        except (OSError, ConnectionRefusedError):
            return False

    def _watcher_event(self, msg: str, level: str = "info"):
        """Log a watcher event to both logger and internal ring buffer."""
        full_msg = f"TraderWatcher: {msg}"
        if level == "warning":
            logger.warning(full_msg)
        else:
            logger.info(full_msg)
        with self._watcher_lock:
            self._watcher_log.append({
                "time": _dt.now().strftime("%H:%M:%S"),
                "message": msg,
                "level": level,
            })
            if len(self._watcher_log) > 100:
                self._watcher_log = self._watcher_log[-100:]

    def _send_sms_alert(self, message: str) -> bool:
        """Send SMS via Verizon email-to-SMS gateway using Gmail SMTP."""
        if not GMAIL_USER or not GMAIL_APP_PASSWORD:
            self._watcher_event("SMS failed: Gmail credentials not set", "warning")
            return False
        try:
            msg = MIMEText(message)
            msg["From"] = GMAIL_USER
            msg["To"] = SMS_GATEWAY
            msg["Subject"] = ""
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_USER, [SMS_GATEWAY], msg.as_string())
            self._watcher_event(f"SMS sent: {message}", "warning")
            return True
        except Exception as e:
            self._watcher_event(f"SMS error: {e}", "warning")
            return False

    def _restart_trader(self, trader_type: str) -> bool:
        """Restart a trader process. Returns True on success."""
        try:
            python_exec = TRADER_VENV_PYTHON if os.path.exists(TRADER_VENV_PYTHON) else sys.executable
            cmd = [python_exec, TRADER_RUN_SCRIPT, "--auto", "--port", str(IB_PAPER_PORT)]
            if trader_type == "day":
                cmd.append("--day")
                log_file = "day_trader_stdout.log"
            else:
                log_file = "trader_stdout.log"
            subprocess.Popen(
                cmd,
                stdout=open(os.path.join(TRADER_LOG_DIR, log_file), "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return True
        except Exception as e:
            self._watcher_event(f"Failed to restart {trader_type}: {e}", "warning")
            return False

    def _watcher_tick(self):
        """Single watcher check cycle."""
        now = time.time()
        today_str = _dt.now().strftime("%Y-%m-%d")

        # Reset daily counters if date changed
        if self._restart_date != today_str:
            self._restart_count_today = 0
            self._restart_date = today_str
        if self._sms_date != today_str:
            self._sms_sent_today = False
            self._sms_date = today_str

        # Check IB Gateway
        gw_up = self._check_ib_gateway()
        if not gw_up:
            self._watcher_event("IB Gateway not reachable, skipping restart check", "warning")
            # SMS alert: 9:00-9:05 AM ET on weekdays if GW down
            market = self._get_market_status()
            if market.get("is_weekday") and not self._sms_sent_today:
                try:
                    from zoneinfo import ZoneInfo
                    now_et = _dt.now(ZoneInfo("America/New_York"))
                except ImportError:
                    now_et = _dt.now()
                if now_et.hour == 9 and 0 <= now_et.minute <= 5:
                    if self._send_sms_alert(
                        "AJ Alert: IB Gateway NOT connected on CashCow! "
                        "Market opens in 30min. Check now."
                    ):
                        self._sms_sent_today = True
            return

        # Check market status
        market = self._get_market_status()
        market_status = market.get("market_status", "closed")

        # Check running processes
        proc = self._is_running()
        smart_up = proc.get("smart_running", False)
        day_up = proc.get("day_running", False)

        # Day Trader: only restart during pre-market or trading
        day_active_hours = market_status in ("pre-market", "open", "trading")

        status_parts = []
        status_parts.append(f"GW:UP Smart:{'UP' if smart_up else 'DOWN'} Day:{'UP' if day_up else 'DOWN'}")
        status_parts.append(f"Market:{market_status}")

        # Check daily cap
        if self._restart_count_today >= self._max_restarts_per_day:
            if not smart_up or (not day_up and day_active_hours):
                self._watcher_event(
                    f"Restart needed but daily limit reached ({self._restart_count_today}/{self._max_restarts_per_day})",
                    "warning",
                )
            return

        restarted = False

        # Smart Trader: restart anytime
        if not smart_up:
            if now - self._last_restart_smart < self._restart_cooldown:
                self._watcher_event("Smart Trader down, cooldown active", "info")
            else:
                self._watcher_event("Smart Trader down, restarting...", "warning")
                if self._restart_trader("smart"):
                    self._last_restart_smart = now
                    self._restart_count_today += 1
                    self._watcher_event("Smart Trader restarted successfully", "warning")
                    restarted = True

        # Day Trader: only during market hours
        if not day_up and day_active_hours:
            if now - self._last_restart_day < self._restart_cooldown:
                self._watcher_event("Day Trader down, cooldown active", "info")
            else:
                self._watcher_event("Day Trader down, restarting...", "warning")
                if self._restart_trader("day"):
                    self._last_restart_day = now
                    self._restart_count_today += 1
                    self._watcher_event("Day Trader restarted successfully", "warning")
                    restarted = True

        if not restarted and smart_up and (day_up or not day_active_hours):
            self._watcher_event(f"All OK | {' | '.join(status_parts)}")

        self._save_watcher_state()

    def _watcher_loop(self):
        """Background thread loop."""
        self._watcher_event("Watcher started")
        while self._watcher_enabled:
            try:
                self._watcher_tick()
            except Exception as e:
                self._watcher_event(f"Error in tick: {e}", "warning")
            time.sleep(self._watcher_check_interval)
        self._watcher_event("Watcher stopped")

    def _start_watcher(self):
        """Enable watcher and start background thread."""
        with self._watcher_lock:
            self._watcher_enabled = True
            self._save_watcher_state()
            if self._watcher_thread is None or not self._watcher_thread.is_alive():
                self._watcher_thread = threading.Thread(
                    target=self._watcher_loop, daemon=True, name="trader-watcher"
                )
                self._watcher_thread.start()

    def _stop_watcher(self):
        """Disable watcher (thread exits on next loop)."""
        self._watcher_enabled = False
        self._save_watcher_state()

    def _get_market_status(self) -> dict:
        """Return current US market status based on Eastern Time."""
        try:
            from zoneinfo import ZoneInfo
            now_et = _dt.now(ZoneInfo("America/New_York"))
        except ImportError:
            try:
                import pytz
                now_et = _dt.now(pytz.timezone("US/Eastern"))
            except ImportError:
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

    def _get_day_watchlist(self) -> list[str]:
        """Extract Day Trader watchlist symbols from log file."""
        for name in ["day_trader_stdout.log", "day_trader.log"]:
            path = os.path.join(TRADER_LOG_DIR, name)
            if os.path.isfile(path):
                try:
                    with open(path, "r", errors="replace") as f:
                        for line in f:
                            if "Watchlist: [" in line or "워치리스트: [" in line:
                                import re
                                m = re.search(r"\[([^\]]+)\]", line)
                                if m:
                                    return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()]
                except Exception:
                    continue
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
            day_watchlist = self._get_day_watchlist()
            return jsonify({
                **proc,
                **market,
                "log_lines": log_lines,
                "day_log_lines": day_log_lines,
                "politician_log_lines": politician_log_lines,
                "daily_picks": picks,
                "config": config,
                "day_watchlist": day_watchlist,
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

        @bp.route("/api/trader/trading-limits", methods=["GET", "POST"])
        def trading_limits():
            limits_file = os.path.join(TRADER_BASE, "trading_limits.json")
            defaults = {
                "smart": {
                    "aggressiveness": 5,
                    "max_trades_per_day": 10,
                    "max_position_size": 100,
                    "default_quantity": 10,
                    "daily_loss_limit_pct": -3.0,
                },
                "day": {
                    "aggressiveness": 5,
                    "max_trades_per_day": 20,
                    "max_positions": 5,
                    "max_trades_per_hour": 10,
                    "daily_loss_hard_limit": -2000,
                    "short_stop_loss_pct": 2.0,
                },
            }
            if request.method == "GET":
                saved = self._read_json(limits_file)
                if saved:
                    # Merge defaults with saved (so new fields get defaults)
                    for trader in defaults:
                        if trader not in saved:
                            saved[trader] = defaults[trader]
                        else:
                            for k, v in defaults[trader].items():
                                saved[trader].setdefault(k, v)
                    return jsonify(saved)
                return jsonify(defaults)
            data = request.json or {}
            trader_type = data.get("trader_type")
            if trader_type not in ("smart", "day"):
                return jsonify({"error": "Invalid trader_type"}), 400
            limits = self._read_json(limits_file) or defaults
            limits[trader_type].update({
                k: v for k, v in data.get("limits", {}).items()
                if k in defaults.get(trader_type, {})
            })
            with open(limits_file, "w") as f:
                json.dump(limits, f, indent=2)
            logger.info("Trading limits updated [%s]: %s", trader_type, limits[trader_type])
            return jsonify({"success": True, "limits": limits})

        @bp.route("/api/trader/trade-history")
        def trade_history():
            """Return trade history for the last N days from log files."""
            days = request.args.get("days", 10, type=int)
            history = self._build_trade_history(days)
            return jsonify(history)

        @bp.route("/api/trader/watcher", methods=["GET", "POST"])
        def trader_watcher():
            if request.method == "POST":
                data = request.json or {}
                enabled = data.get("enabled", False)
                if enabled:
                    self._start_watcher()
                    self._watcher_event("Enabled by user")
                else:
                    self._stop_watcher()
                    self._watcher_event("Disabled by user")
                return jsonify({"success": True, "enabled": self._watcher_enabled})

            # GET: return watcher status
            gw_up = self._check_ib_gateway()
            proc = self._is_running()
            with self._watcher_lock:
                events = list(self._watcher_log[-20:])
            return jsonify({
                "enabled": self._watcher_enabled,
                "ib_gateway_up": gw_up,
                "smart_running": proc.get("smart_running", False),
                "day_running": proc.get("day_running", False),
                "restart_count_today": self._restart_count_today,
                "max_restarts_per_day": self._max_restarts_per_day,
                "recent_events": events,
            })

        @bp.route("/api/trader/sms-test", methods=["POST"])
        def sms_test():
            data = request.json or {}
            message = data.get("message", "AJ Robotics test message from CashCow")
            ok = self._send_sms_alert(message)
            return jsonify({"ok": ok, "phone": SMS_PHONE, "message": message})

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
            """Fetch bars for a symbol — from cache if available, otherwise live IB."""
            date_str = request.args.get("date", _dt.now().strftime("%Y-%m-%d"))
            # Try cache first
            cached = self._load_intraday_cache(date_str)
            if cached and symbol in cached:
                return jsonify(cached[symbol])
            # Live fetch (only for today)
            port = request.args.get("port", IB_PAPER_PORT, type=int)
            bars = self._fetch_intraday_bars(symbol, port)
            return jsonify(bars)

        @bp.route("/api/trader/save-intraday", methods=["POST"])
        def save_intraday():
            """Fetch 10-min bars for all today's traded symbols and cache to disk."""
            today_str = _dt.now().strftime("%Y-%m-%d")
            # Get symbols from today's trades
            day_trades = self._parse_trades_from_log("day", today_str)
            smart_trades = self._parse_trades_from_log("smart", today_str)
            symbols = list(set(
                [t["symbol"] for t in day_trades] +
                [t["symbol"] for t in smart_trades]
            ))
            if not symbols:
                return jsonify({"error": "No trades today", "saved": 0})
            port = IB_PAPER_PORT
            if request.is_json and request.json:
                port = request.json.get("port", IB_PAPER_PORT)
            symbol_bars = {}
            for sym in symbols:
                bars = self._fetch_intraday_bars(sym, port, bar_size="10 mins")
                if bars:
                    symbol_bars[sym] = bars
            self._save_intraday_cache(today_str, symbol_bars)
            return jsonify({"success": True, "date": today_str, "saved": len(symbol_bars), "symbols": list(symbol_bars.keys())})

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

        # Auto-start TraderWatcher if previously enabled
        if self._watcher_enabled:
            self._start_watcher()
            logger.info("TraderWatcher auto-started (persisted state)")

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

        # Patterns for executed orders (English log format)
        if trader_type == "day":
            # ✅ BUY order sent! SYMBOL x{shares} | OrderID: {id} | Status: {status}
            order_re = re.compile(
                r"✅\s+(BUY|SELL)\s+order\s+sent!\s+(\w+)\s+x(\d+)\s*\|\s*OrderID:\s*(\d+)"
            )
        else:
            # ✅ Order submitted! BUY SYMBOL x{qty} | Order ID: {id} | Status: {status}
            order_re = re.compile(
                r"✅\s+Order\s+submitted!\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s*\|\s*Order\s*ID:\s*(\d+)"
            )

        # Also capture ALERT mode signals (no actual order placed)
        if trader_type == "day":
            alert_re = re.compile(
                r"🔔\s+\[ALERT\]\s+(BUY|SELL)\s+(\w+)\s+x(\d+)\s+@\s+\$([0-9.]+)"
            )
        else:
            alert_re = None

        # Ensemble decision line (English: "Consensus:")
        ensemble_re = re.compile(
            r"🎯\s+(\w+).*Consensus:\s+([+-]?[0-9.]+)\s*\|\s*BUY:(\d+)\s+SELL:(\d+)"
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
                # "📏 Sizing: 25 shares × $598.08 = $14,952.00"
                if 'Sizing' in pline or 'sizing' in pline:
                    pm = re.search(r'[×x]\s*\$([0-9.]+)', pline)
                    if pm:
                        trade_price = float(pm.group(1))
                        break
                # "VWAP $380.62 support" or "VWAP $380.62 resistance"
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

    def _fetch_intraday_bars(self, symbol: str, port: int = IB_PAPER_PORT, bar_size: str = "5 mins") -> list:
        """Connect to IB and fetch today's bars for a symbol."""
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
                barSizeSetting=bar_size,
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

    # ── Intraday Bar Cache ─────────────────────────────────────

    def _intraday_cache_path(self, date_str: str) -> str:
        return os.path.join(TRADER_LOG_DIR, "intraday_cache", f"{date_str}.json")

    def _save_intraday_cache(self, date_str: str, symbol_bars: dict):
        cache_dir = os.path.join(TRADER_LOG_DIR, "intraday_cache")
        os.makedirs(cache_dir, exist_ok=True)
        with open(self._intraday_cache_path(date_str), "w") as f:
            json.dump(symbol_bars, f)
        logger.info("Saved intraday cache for %s (%d symbols)", date_str, len(symbol_bars))

    def _load_intraday_cache(self, date_str: str) -> dict | None:
        path = self._intraday_cache_path(date_str)
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return None
