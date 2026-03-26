# CashCow Trading Safety Rules

## Safety Net Algorithm (MANDATORY)

All trading code MUST enforce these rules. They are non-negotiable and apply to every trader (Smart, Day, Politician).

### Rule 1: Cash-Only Trading

- Every trade MUST be executed within the available cash balance.
- Never place a buy order that exceeds the current available cash.
- Do NOT use margin or leverage. All positions must be fully cash-backed.

### Rule 2: Pre-Order Cash Validation

- Before sending any **buy** order, query all open/pending orders across the account.
- Compute the **projected cash** = available cash - (sum of all open buy orders' estimated cost).
- Only proceed with the new buy order if projected cash >= cost of the new order.
- This prevents over-commitment when multiple orders are in-flight simultaneously.

### Rule 3: Daily Transaction Limit Per Trader

- Each trader has a maximum number of transactions allowed per day.
- Before placing any order, check how many orders the trader has already placed today.
- If the daily limit is reached, reject the order and log a warning.
- The daily limit is configurable per trader and must be enforced independently for each trader (Smart, Day, Politician).

## Logging Language

- All trader log messages MUST be in **English**. No Korean or other languages in log output.
- This applies to Smart Trader, Day Trader, and Politician Trader.
- Emoji usage in logs is fine (✅, 🔴, 🎯, etc.), but all text must be English.

## Intraday Bar Data Caching

- After market close, save 10-min resolution intraday bars for all traded symbols to `~/ib_smart_trader/logs/intraday_cache/{YYYY-MM-DD}.json`.
- The `/today` page auto-triggers this save after 1:05 PM PT if trades exist.
- On page load, the `/api/trader/intraday/{symbol}?date=YYYY-MM-DD` endpoint serves cached data if available, avoiding repeated IB Gateway connections.
- For past dates, bars are always served from cache. For today, live IB data is used until cache is saved.
