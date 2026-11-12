"""Trend-rider bot for btc-race-24h-20261112-1439.

Long-only trend following on 1-minute cbBTC bars (agent-fun/bars-replay/v1).
Speaks the protocol through the vendored shim (vendor/makefun_bars.py), one
reply per batch, echoing the batch seq.

Idea: BTC trends in multi-hour legs and is nearly flat minute to minute
(dev window: 1-minute return sd ~5 bps, round trip costs ~26 bps), so the
bot only trades when the short, medium and long pictures agree, then rides
the leg with a trailing stop. It never trades a flat or falling market.

  Entry (flat): EMA(6) above EMA(18), close above SMA(30), the 30-bar
    return above +15 bps, and EMA(6) rising over the last 6 bars.
    Buys 100% of cash (one asset, one position max).
  Exit (long): the position gains 100 bps over its cost (take profit),
    or falls 100 bps under its peak value since entry (trailing stop),
    or 80 bps under its cost (stop-loss), or the 60-bar return turns
    negative (trend dead). Sells the whole position.
  After an exit it waits 240 bars before a new entry, and a re-entry
  needs a much stronger move (30-bar return above +70 bps instead of
  +15), so one whipsaw cannot re-enter on the same move and the small
  bounces of a falling market do not keep costing round trips.

Everything is a pure function of the bars the harness sends (hello, batch,
orders, bar): no clock, no randomness, no environment, no files outside
the repository, so two runs on the same data answer byte for byte alike.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
FAST_SPAN = 6
SLOW_SPAN = 18
TREND_SPAN = 30
RET_LOOKBACK = 30
RET_MIN_BPS = 15
REENTRY_MIN_BPS = 70
EXIT_LOOKBACK = 60
TRAIL_BPS = 100
STOP_BPS = 80
TAKE_PROFIT_BPS = 100
COOLDOWN_BARS = 240
EXIT_ON_CROSS = False
MAX_SLIPPAGE_BPS = 100


class Ema:
    """One EMA: starts at the first close it sees, then moves
    2 / (span + 1) of the way to each new close (float64, as the baselines).
    Starting at the first close, instead of the mean of the first span,
    lets a trend be read within a few bars of the window opening."""

    def __init__(self, span):
        self.span = span
        self.value = None

    def step(self, close):
        if self.value is None:
            self.value = float(close)
            return
        self.value += (2.0 / (self.span + 1)) * (close - self.value)


class TrendRider:
    def __init__(self):
        self.closes = []
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.trend_warm = []
        self.trend = None  # SMA(TREND_SPAN) of closes
        self.fast_hist = []  # EMA(12) values, for the rising check
        self.peak_value = None  # position value at its best close since entry
        self.last_exit_seq = -10**9
        self.round_trips = 0  # completed exits; re-entries need a stronger move

    def push(self, close):
        """Fold one fresh bar's close into every indicator."""
        self.closes.append(close)
        self.fast.step(close)
        self.slow.step(close)
        self.fast_hist.append(self.fast.value)
        if self.trend is not None:
            self.trend += (close - self.closes[-TREND_SPAN - 1]) / TREND_SPAN
        else:
            self.trend_warm.append(close)
            if len(self.trend_warm) == TREND_SPAN:
                self.trend = sum(self.trend_warm) / TREND_SPAN

    def ret_bps(self, lookback):
        """The return over `lookback` bars in bps, or None without history."""
        if len(self.closes) <= lookback:
            return None
        then = self.closes[-lookback - 1]
        now = self.closes[-1]
        return (now - then) * 10000.0 / then

    def fast_rising(self):
        if len(self.fast_hist) < 7:
            return False
        now = self.fast_hist[-1]
        then = self.fast_hist[-7]
        return now is not None and then is not None and now > then

    def may_enter(self, state):
        if self.fast.value is None or self.slow.value is None or self.trend is None:
            return False
        if self.fast.value <= self.slow.value:
            return False
        if self.closes[-1] <= self.trend:
            return False
        ret = self.ret_bps(RET_LOOKBACK)
        need = REENTRY_MIN_BPS if self.round_trips >= 1 else RET_MIN_BPS
        if ret is None or ret <= need:
            return False
        if not self.fast_rising():
            return False
        if state.seq - self.last_exit_seq < COOLDOWN_BARS:
            return False
        return True

    def should_exit(self, state, held):
        if EXIT_ON_CROSS and self.fast.value is not None and self.slow.value is not None:
            if self.fast.value < self.slow.value:
                return True
        value = state.value(SYMBOL, held.qty)
        if value * 10000 >= held.cost * (10000 + TAKE_PROFIT_BPS):
            return True
        if self.peak_value is not None and value * 10000 < self.peak_value * (10000 - TRAIL_BPS):
            return True
        if value * 10000 < held.cost * (10000 - STOP_BPS):
            return True
        ret = self.ret_bps(EXIT_LOOKBACK)
        if ret is not None and ret < 0:
            return True
        return False


BOT = TrendRider()


def on_batch(state, batch):
    fresh = False
    for bar in batch["bars"]:
        if bar["symbol"] == SYMBOL:
            BOT.push(int(bar["c"]))
            fresh = True
    if not fresh or state.warming_up:
        return []
    held = state.positions.get(SYMBOL)
    if held is not None:
        value = state.value(SYMBOL, held.qty)
        if BOT.peak_value is None or value > BOT.peak_value:
            BOT.peak_value = value
        if BOT.should_exit(state, held):
            BOT.last_exit_seq = state.seq
            BOT.peak_value = None
            BOT.round_trips += 1
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []
    if BOT.may_enter(state):
        room = state.weight_room(SYMBOL)
        amount = state.cash if state.cash < room else room
        if amount < int(state.hello["fees"]["min_notional_quote_units"]):
            return []
        BOT.peak_value = None
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": amount,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]
    return []


makefun_bars.run(on_batch)
