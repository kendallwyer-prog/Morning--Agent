"""Stock suggestions — transparent, explainable momentum screen.

NOT financial advice. The logic is intentionally simple and visible:

1. Build a candidate universe from the SECTORS you already hold. A held ticker
   "activates" its sector (membership in data/sector_peers.json); we then
   consider that sector's liquid peers, excluding what you already own. If no
   holding maps to a known sector (e.g. you hold only broad ETFs), we fall back
   to data/watchlist.json.
2. For each candidate, pull ~1 month of closes (yfinance) and compute two plain
   momentum numbers:
     - 5-day % change
     - % above/below the 20-day simple moving average
3. Rank by 5-day momentum, take the top 1-3, and state the actual numbers and
   the sector link as the rationale — so you can always see WHY.

Every failure degrades to a graceful note; the digest still sends.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from . import SectionResult

_DATA = Path(__file__).resolve().parent.parent.parent / "data"
_PEERS = _DATA / "sector_peers.json"
_WATCHLIST = _DATA / "watchlist.json"
_MAX_IDEAS = 3
_MAX_CANDIDATES = 20  # cap network work
_DISCLAIMER = "_Not financial advice — for informational purposes only._"


def _candidate_universe(holdings) -> tuple[list[str], dict[str, str]]:
    """Return (candidate_tickers, ticker->reason_context).

    reason_context maps each candidate to either "<Sector> peer of your <TICKER>"
    or "watchlist".
    """
    peers = json.loads(_PEERS.read_text())
    held = {h["symbol"].upper() for h in (holdings or []) if h.get("symbol")}

    # Which sectors are "active": a held ticker is a member of that sector list.
    active: dict[str, str] = {}  # sector -> the held ticker that activated it
    for sector, members in peers.items():
        for m in members:
            if m in held and sector not in active:
                active[sector] = m

    context: dict[str, str] = {}
    candidates: list[str] = []
    for sector, anchor in active.items():
        for ticker in peers[sector]:
            if ticker in held or ticker in context:
                continue
            context[ticker] = f"{sector} peer of your {anchor}"
            candidates.append(ticker)

    if not candidates:
        wl = json.loads(_WATCHLIST.read_text()).get("tickers", [])
        for ticker in wl:
            if ticker in held or ticker in context:
                continue
            context[ticker] = "from your watchlist (no held sectors matched)"
            candidates.append(ticker)

    return candidates[:_MAX_CANDIDATES], context


def _metrics_from_closes(closes: list[float]) -> dict | None:
    """Compute momentum metrics from a chronological list of closes."""
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < 5:
        return None
    last = closes[-1]
    ref5 = closes[-6] if len(closes) >= 6 else closes[0]
    mom5 = (last / ref5 - 1) * 100 if ref5 else 0.0
    window = closes[-20:] if len(closes) >= 20 else closes
    sma = mean(window)
    vs_sma = (last / sma - 1) * 100 if sma else 0.0
    return {"price": last, "mom5": mom5, "vs_sma20": vs_sma}


def _download_closes(tickers: list[str]) -> dict[str, list[float]]:
    """Fetch ~1 month of daily closes per ticker via yfinance."""
    import yfinance as yf

    data = yf.download(
        tickers,
        period="1mo",
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=True,
    )
    out: dict[str, list[float]] = {}
    if data is None or len(data) == 0:
        return out

    close = data["Close"]
    if len(tickers) == 1:
        # Single ticker -> close is a Series.
        out[tickers[0]] = [float(x) for x in close.dropna().tolist()]
    else:
        for t in tickers:
            if t in close.columns:
                series = close[t].dropna()
                out[t] = [float(x) for x in series.tolist()]
    return out


def _render(ranked: list[tuple[str, dict, str]]) -> str:
    lines = []
    for ticker, m, ctx in ranked:
        mom_sign = "+" if m["mom5"] >= 0 else ""
        sma_sign = "+" if m["vs_sma20"] >= 0 else ""
        lines.append(
            f"• **{ticker}** — {mom_sign}{m['mom5']:.1f}% over 5 days, "
            f"{sma_sign}{m['vs_sma20']:.1f}% vs 20-day avg. _{ctx}._"
        )
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def rank(metrics: dict[str, dict], context: dict[str, str]) -> list[tuple[str, dict, str]]:
    """Pure ranking step (separated for testability)."""
    scored = [
        (t, m, context.get(t, ""))
        for t, m in metrics.items()
        if m is not None
    ]
    scored.sort(key=lambda x: x[1]["mom5"], reverse=True)
    return scored[:_MAX_IDEAS]


def build(holdings=None) -> SectionResult:
    candidates, context = _candidate_universe(holdings)
    if not candidates:
        raise RuntimeError("no candidates to screen")

    closes = _download_closes(candidates)
    metrics = {t: _metrics_from_closes(c) for t, c in closes.items()}
    metrics = {t: m for t, m in metrics.items() if m is not None}
    if not metrics:
        raise RuntimeError("no price data available")

    ranked = rank(metrics, context)
    if not ranked:
        raise RuntimeError("no ideas passed screening")

    return SectionResult(title="Ideas", body=_render(ranked))
