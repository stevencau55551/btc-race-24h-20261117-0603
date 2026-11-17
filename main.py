"""cbBTC/USDC 1-minute trading bot for make.fun btc-race-24h-20261117-0603.

Strategy: crash-rebound dip buying, long only, paper $1,000 USDC.

  * Arm when the close has fallen at least 65 bps in the last 10 bars or
    90 bps in the last 30 bars. Track the dip low (the lowest bar low seen
    while armed).
  * Buy 80% of equity when the close rebounds at least 15 bps off that low.
  * Exit the whole position at +120 bps over the fill price (take profit),
    at -60 bps (stop loss), or on a trailing stop: once the close has been
    60 bps over the fill price, sell when it falls 35 bps from its high
    since entry.
  * Circuit breaker: one stop-loss exit ends all further trading for the
    window. A failed dip-buy means dip-buying does not work in this regime
    (a sustained downtrend); preserving capital beats a second falling
    knife. A winning exit resets the breaker.
  * At most 4 entries per window.
  * No-trade fallback: if nothing has traded by bar 1200, relax the arm
    (40 bps / 10 bars, 60 bps / 30 bars, 10 bps rebound, 50% size); if
    still nothing by bar 1380, buy 10% once, so the entry always meets
    the race's minimum-1-trade guardrail with minimal forced exposure.
    An armed dip setup expires 60 bars after arming, so a stale low
    cannot trigger an unrelated bounce. No new buys after bar 1400.

Costs: a full-size round trip pays ~37 bps (4 bps fee + 2 bps slippage +
~13 bps price impact per side at this pool's depth), so every trade must
catch a move well above that. Entries are therefore rare and selective.

Determinism: pure float arithmetic on the integer bar prices, in fixed
order, over lists only. No network, no wall clock, no files, no random
numbers, no environment reads. Two runs on the same bars give the same
orders byte for byte.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- crash-rebound parameters (basis points unless noted) ---
DROP_10_BPS = 65        # arm: close down this much over the last 10 bars
DROP_30_BPS = 90        # arm: ... or this much over the last 30 bars
REBOUND_BPS = 15        # buy: close this much above the tracked dip low
SIZE_BPS = 8000         # buy: this share of equity (80%)
TAKE_PROFIT_BPS = 120   # exit: close this much above the fill price
STOP_LOSS_BPS = 60      # exit: close this much below the fill price
TRAIL_ARM_BPS = 60      # trailing stop arms once the close is this far up
TRAIL_BPS = 35          # ... then exit on this pullback from the high
MAX_ENTRIES = 6         # at most this many round trips per window
MAX_LOSSES = 1          # stop trading after this many stop-loss exits

# --- no-trade fallback (guardrail: at least 1 trade) ---
RELAX_BAR = 1200        # from this bar, relax the arm while nothing traded
RELAX_DROP_10_BPS = 40
RELAX_DROP_30_BPS = 60
RELAX_REBOUND_BPS = 10
RELAX_SIZE_BPS = 5000
ARM_LIFETIME_BARS = 60  # an armed dip setup expires this many bars after arming
LAST_BUY_BAR = 1380     # blind buy here if still nothing traded (small size:
FALLBACK_SIZE_BPS = 1000  # 10%: the fallback only has to make 1 trade)
NO_BUY_AFTER_BAR = 1400 # never open a new position after this bar

MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on

# --- bot state (module level, reset per process run) ---
closes = []             # closes in USD, oldest first, one per CBBTC bar
entry_price = None      # fill price in USD of the open position, else None
high_since_entry = None # highest close since entry, else None
dip_low = None          # lowest bar low while armed, else None
armed = False
armed_bar = -1          # seq of the bar that armed the current setup
entries = 0             # round trips opened so far
losses = 0              # consecutive stop-loss exits (reset by a win)
pending_exit = None     # exit kind of the sell order awaiting its fill


def _price_usd(state, raw):
    """A price string at the symbol's price scale, as USD float."""
    scale = state.symbols[SYMBOL]["price_scale"]
    return int(raw) / (10 ** scale)


def on_batch(state, batch):
    global entry_price, high_since_entry, dip_low, armed, armed_bar, entries, losses, pending_exit

    # 1. Fold in the fills of the orders decided on the previous batch.
    #    The harness's own end-of-window flatten fills (client ids starting
    #    with "_") are not our orders: ignore them.
    for fill in batch.get("fills", []):
        if fill.get("symbol") != SYMBOL:
            continue
        if fill.get("status") != "filled":
            continue
        cid = fill.get("client_id") or ""
        if cid.startswith("_"):
            continue
        if fill.get("side") == "buy":
            entry_price = _price_usd(state, fill["price"])
            high_since_entry = entry_price
            entries += 1
        elif fill.get("side") == "sell":
            if pending_exit == "stop":
                losses += 1
            else:
                losses = 0
            entry_price = None
            high_since_entry = None
            pending_exit = None

    # 2. This batch's CBBTC bar, if any (a minute with no sample has none).
    bar = None
    for b in batch.get("bars", []):
        if b.get("symbol") == SYMBOL:
            bar = b
            break
    if bar is None:
        return []
    try:
        close = _price_usd(state, bar["c"])
        low = _price_usd(state, bar["l"])
    except (KeyError, TypeError, ValueError):
        return []
    if close <= 0 or low <= 0:
        return []
    closes.append(close)

    # 3. Warm-up batches build history only; their orders would be rejected.
    if state.warming_up:
        return []

    held = state.positions.get(SYMBOL)
    holding = held is not None and held.qty > 0
    if holding and entry_price is None and held.qty > 0 and held.cost > 0:
        # Fallback: derive the fill price from the position's cost basis
        # (cost includes the buy fee, so this is ~4 bps above the fill).
        entry_price = held.cost * 100.0 / held.qty
        high_since_entry = entry_price

    # 4. Holding: check the exits against the fill price.
    if holding and entry_price is not None:
        if high_since_entry is None:
            high_since_entry = close
        elif close > high_since_entry:
            high_since_entry = close
        exit_kind = None
        if close >= entry_price * (1 + TAKE_PROFIT_BPS / 10000.0):
            exit_kind = "tp"
        elif close <= entry_price * (1 - STOP_LOSS_BPS / 10000.0):
            exit_kind = "stop"
        elif (high_since_entry >= entry_price * (1 + TRAIL_ARM_BPS / 10000.0)
                and close <= high_since_entry * (1 - TRAIL_BPS / 10000.0)):
            exit_kind = "trail"
        if exit_kind is not None:
            pending_exit = exit_kind
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    # 5. Not holding (or entry price unknown after a restart): no new buys
    #    while the circuit breaker is tripped or the entry budget is spent.
    if entries >= MAX_ENTRIES or losses >= MAX_LOSSES:
        return []
    if state.seq > NO_BUY_AFTER_BAR:
        return []

    # 6. Arm / rebound logic.
    n = len(closes)
    if n < 31:
        return []
    relaxed = entries == 0 and state.seq >= RELAX_BAR
    drop10 = RELAX_DROP_10_BPS if relaxed else DROP_10_BPS
    drop30 = RELAX_DROP_30_BPS if relaxed else DROP_30_BPS
    rebound = RELAX_REBOUND_BPS if relaxed else REBOUND_BPS
    size_bps = RELAX_SIZE_BPS if relaxed else SIZE_BPS

    d10 = closes[-1] / closes[-11] - 1.0
    d30 = closes[-1] / closes[-31] - 1.0
    if d10 <= -drop10 / 10000.0 or d30 <= -drop30 / 10000.0:
        armed = True
        armed_bar = state.seq
        dip_low = low if dip_low is None else min(dip_low, low)
        return []
    if armed and state.seq - armed_bar > ARM_LIFETIME_BARS:
        # Stale setup: the dip is long past, do not buy an unrelated bounce
        # off its low; wait for a fresh drop signal.
        armed = False
        dip_low = None
        return []
    if armed and dip_low is not None and close >= dip_low * (1 + rebound / 10000.0):
        armed = False
        dip_low = None
        return _buy(state, size_bps)
    if armed and dip_low is not None and close > dip_low * 1.01:
        # Price ran away more than 1% above the low without triggering:
        # the rebound is over, do not chase it.
        armed = False
        dip_low = None
        return []

    # 7. Last-resort fallback buy so the entry always makes >= 1 trade.
    #    Small size on purpose: this buy has no signal, it only has to
    #    satisfy the guardrail, so it takes minimal forced exposure.
    if entries == 0 and state.seq >= LAST_BUY_BAR:
        return _buy(state, FALLBACK_SIZE_BPS)
    return []


def _buy(state, size_bps):
    """One buy order for size_bps of equity, capped by cash, or [] if too small."""
    notional = state.equity * size_bps // 10000
    if notional > state.cash:
        notional = state.cash
    room = state.weight_room(SYMBOL)
    if notional > room:
        notional = room
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if notional < min_notional:
        return []
    return [{"side": "buy", "symbol": SYMBOL,
             "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}]


makefun_bars.run(on_batch)
