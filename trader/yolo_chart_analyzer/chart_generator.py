"""Generate candlestick chart images from stock data for YOLO pattern detection."""

import os
from datetime import datetime, timedelta
from pathlib import Path

import mplfinance as mpf
import yfinance as yf


def load_watchlist(path: str) -> list[str]:
    """Load ticker symbols from watchlist file."""
    tickers = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tickers.append(line.upper())
    return tickers


def download_ohlcv(ticker: str, days: int = 120) -> "pd.DataFrame | None":
    """Download OHLCV data from Yahoo Finance."""
    end = datetime.now()
    start = end - timedelta(days=days + 30)  # extra buffer for weekends/holidays
    try:
        df = yf.download(ticker, start=start, end=end, progress=False)
        if df.empty:
            print(f"  [WARN] No data for {ticker}")
            return None
        # Flatten multi-level columns if present
        if hasattr(df.columns, "levels") and len(df.columns.levels) > 1:
            df.columns = df.columns.get_level_values(0)
        return df.tail(days)
    except Exception as e:
        print(f"  [ERROR] Failed to download {ticker}: {e}")
        return None


def generate_chart_image(
    df, ticker: str, output_dir: str, img_size: tuple[int, int] = (640, 640)
) -> str | None:
    """Generate a candlestick chart image and save to disk."""
    output_path = os.path.join(output_dir, f"{ticker}.png")

    dpi = 100
    figsize = (img_size[0] / dpi, img_size[1] / dpi)

    style = mpf.make_mpf_style(
        base_mpf_style="charles",
        gridstyle="",
        y_on_right=False,
    )

    try:
        mpf.plot(
            df,
            type="candle",
            style=style,
            volume=True,
            figsize=figsize,
            savefig=dict(fname=output_path, dpi=dpi, bbox_inches="tight"),
            tight_layout=True,
        )
        return output_path
    except Exception as e:
        print(f"  [ERROR] Chart generation failed for {ticker}: {e}")
        return None


def generate_charts(
    watchlist_path: str, output_dir: str, days: int = 120
) -> list[dict]:
    """Generate chart images for all tickers in watchlist."""
    os.makedirs(output_dir, exist_ok=True)
    tickers = load_watchlist(watchlist_path)
    results = []

    print(f"Generating charts for {len(tickers)} tickers ({days}-day window)...")

    for ticker in tickers:
        print(f"  Processing {ticker}...")
        df = download_ohlcv(ticker, days=days)
        if df is None:
            results.append({"ticker": ticker, "status": "no_data", "path": None})
            continue

        path = generate_chart_image(df, ticker, output_dir)
        if path:
            results.append({"ticker": ticker, "status": "ok", "path": path})
            print(f"  -> Saved: {path}")
        else:
            results.append({"ticker": ticker, "status": "error", "path": None})

    ok_count = sum(1 for r in results if r["status"] == "ok")
    print(f"Done: {ok_count}/{len(tickers)} charts generated.")
    return results


if __name__ == "__main__":
    import sys

    watchlist = sys.argv[1] if len(sys.argv) > 1 else "watchlist.txt"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "charts_temp"
    generate_charts(watchlist, outdir)
