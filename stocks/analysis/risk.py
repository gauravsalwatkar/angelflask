"""Position sizing and pre-trade limits, so every BUY says how much to buy and what it can lose.

* Size: as many shares (or option lots) as keep the loss at the stop loss within
  MAX_LOSS_PER_TRADE and the cost within TRADING_CAPITAL (both in settings / .env).
* A trade where even one share or lot risks more than MAX_LOSS_PER_TRADE (or costs more than the
  capital) does not fit, and the live screen turns it from BUY into WAIT / SKIP with the reason.
* Intraday options: no new entries after LAST_ENTRY; a leg still open at SQUARE_OFF is exited so
  nothing is carried past the close.

The loss shown is the loss at the stop price. A gap or a fast market can fill a stop lower, so the
real loss can be bigger. No orders are placed anywhere in this app.
"""
import math
from datetime import time as dtime

from django.conf import settings

LAST_ENTRY = dtime(15, 0)
SQUARE_OFF = dtime(15, 15)


def size(entry, stop_loss, lot=1):
    """How many lots of `lot` units to buy at `entry` with the given stop, or None without a valid stop."""
    if not entry or stop_loss is None or entry <= stop_loss:
        return None
    capital, max_loss = settings.TRADING_CAPITAL, settings.MAX_LOSS_PER_TRADE
    lot_risk, lot_cost = (entry - stop_loss) * lot, entry * lot
    # The epsilon keeps 1000 / (0.1 * 100) from flooring to 99 on float error.
    by_risk = math.floor(max_loss / lot_risk + 1e-9)
    by_cash = math.floor(capital / lot_cost + 1e-9)
    lots = max(min(by_risk, by_cash), 0)
    return {
        "lots": lots,
        "qty": lots * lot,
        "cost": round(lots * lot_cost, 2),
        "max_loss": round(lots * lot_risk, 2),
        "risk_per_lot": round(lot_risk, 2),
        "cost_per_lot": round(lot_cost, 2),
        "limited_by": "max loss per trade" if by_risk <= by_cash else "capital",
        "max_loss_limit": max_loss,
        "capital": capital,
    }


def fits_detail(position, unit="share"):
    """(ok, plain-language detail) for a size() result."""
    if position is None:
        return False, "No position: the price is not above the stop loss"
    if position["lots"] == 0:
        if position["risk_per_lot"] > position["max_loss_limit"]:
            return False, (f"1 {unit} risks ₹{position['risk_per_lot']:,.2f} to the stop, over your max loss "
                           f"per trade ₹{position['max_loss_limit']:,.0f}")
        return False, f"1 {unit} costs ₹{position['cost_per_lot']:,.2f}, more than your capital ₹{position['capital']:,.0f}"
    plural = unit if position["lots"] == 1 else f"{unit}s"
    return True, (f"{position['lots']} {plural} (₹{position['cost']:,.0f}) loses ₹{position['max_loss']:,.0f} at the "
                  f"stop, within your ₹{position['max_loss_limit']:,.0f} limit (sized by {position['limited_by']})")
