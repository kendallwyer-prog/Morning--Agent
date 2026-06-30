"""Portfolio section — Robinhood via the unofficial robin_stocks library.

THIS IS THE FRAGILE PART. Robinhood has no official API; robin_stocks may break
at any time, and logins from datacenter IPs (like GitHub Actions) are often
challenged or blocked. Everything here is defensive:

- Credentials come only from env vars (RH_USERNAME / RH_PASSWORD).
- 2FA is handled headlessly by generating the TOTP code from RH_MFA_SECRET via
  pyotp. If that secret is absent and Robinhood demands interactive input,
  robin_stocks' input() call raises EOFError on a non-interactive runner rather
  than hanging — which we catch and turn into a graceful fallback.
- ANY failure raises a RuntimeError with a friendly message; the orchestrator
  converts it into a "couldn't fetch portfolio" note so the digest still sends.

On success, the returned SectionResult carries a ``holdings`` attribute
(list of {"symbol", "quantity"}) so the suggestions section can screen within
sectors you already own.
"""
from __future__ import annotations

from .. import config
from . import SectionResult


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _login() -> None:
    """Log into Robinhood headlessly. Raises RuntimeError on any failure."""
    import pyotp
    import robin_stocks.robinhood as rh

    username = config.require("RH_USERNAME")
    password = config.require("RH_PASSWORD")
    mfa_secret = config.get("RH_MFA_SECRET")

    mfa_code = None
    if mfa_secret:
        try:
            mfa_code = pyotp.TOTP(mfa_secret).now()
        except Exception as exc:  # bad secret format, etc.
            raise RuntimeError(f"invalid RH_MFA_SECRET: {exc}") from exc

    try:
        result = rh.login(
            username=username,
            password=password,
            mfa_code=mfa_code,
            store_session=True,
        )
    except EOFError as exc:
        # robin_stocks tried to prompt for a code/approval on a headless runner.
        raise RuntimeError(
            "login needs interactive verification (set RH_MFA_SECRET, "
            "or fetch from a trusted IP)"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"login failed: {exc}") from exc

    if not result or not result.get("access_token"):
        raise RuntimeError("login did not return an access token")


def _gather() -> tuple[str, list[dict]]:
    """Return (rendered_body, holdings). Assumes already logged in."""
    import robin_stocks.robinhood as rh

    profile = rh.load_portfolio_profile() or {}
    holdings_map = rh.build_holdings() or {}

    def _f(key: str) -> float | None:
        val = profile.get(key)
        try:
            return float(val) if val not in (None, "") else None
        except (TypeError, ValueError):
            return None

    # Total value: prefer extended-hours equity when present (pre/post market),
    # else regular equity, else sum of position equity.
    equity = _f("extended_hours_equity") or _f("equity")
    prev_close = _f("adjusted_equity_previous_close")

    positions = []
    holdings = []
    total_position_equity = 0.0
    for symbol, h in sorted(holdings_map.items()):
        try:
            qty = float(h.get("quantity", 0) or 0)
            price = float(h.get("price", 0) or 0)
            pos_equity = float(h.get("equity", qty * price) or 0)
            pct = float(h.get("percent_change", 0) or 0)  # total return %
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        total_position_equity += pos_equity
        positions.append((symbol, qty, pos_equity, pct))
        holdings.append({"symbol": symbol, "quantity": qty})

    if equity is None:
        equity = total_position_equity if total_position_equity else None

    lines: list[str] = []
    if equity is not None:
        lines.append(f"**Total: {_money(equity)}**")
        if prev_close:
            day_change = equity - prev_close
            day_pct = (day_change / prev_close * 100) if prev_close else 0.0
            arrow = "🔺" if day_change >= 0 else "🔻"
            sign = "+" if day_change >= 0 else "-"
            lines.append(
                f"Today: {arrow} {sign}{_money(abs(day_change))} "
                f"({sign}{abs(day_pct):.2f}%)"
            )
    else:
        lines.append("_Total value unavailable._")

    if positions:
        lines.append("")
        for symbol, qty, pos_equity, pct in positions:
            sign = "+" if pct >= 0 else ""
            qty_str = f"{qty:g}"
            lines.append(
                f"• **{symbol}** {qty_str} sh — {_money(pos_equity)} "
                f"({sign}{pct:.1f}% total)"
            )
    else:
        lines.append("_No open positions found._")

    return "\n".join(lines), holdings


def build() -> SectionResult:
    try:
        import pyotp  # noqa: F401
        import robin_stocks.robinhood  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(f"missing dependency: {exc}") from exc

    _login()

    try:
        body, holdings = _gather()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"couldn't read portfolio: {exc}") from exc
    finally:
        try:
            import robin_stocks.robinhood as rh

            rh.logout()
        except Exception:  # noqa: BLE001
            pass

    result = SectionResult(title="Portfolio", body=body)
    result.holdings = holdings  # consumed by the suggestions section
    return result
