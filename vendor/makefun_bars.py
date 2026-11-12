"""agent-fun/bars-replay/v1 bot shim for Python 3.13: standard library only, one file.

The Python twin of bars/sdk/makefun-bars.ts: the same state, checks and reply bytes. The grader runs a
Python bot as ``python3 -P -s /bot/main.py`` with ``PYTHONHASHSEED=0``; -P leaves the script's
directory off sys.path, so copy this file to vendor/makefun_bars.py and put that directory on it::

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
    import makefun_bars

    def on_batch(state, batch):
        # the orders decided on this bar; None or [] for none
        return [{"side": "buy", "symbol": "SOL", "notional_quote_units": state.cash // 10, "max_slippage_bps": 100}]

    makefun_bars.run(on_batch)              # optional: on_hello=f(state), on_done=f(state, done)

The shim owns the wire: one JSON line at a time from stdin, ``state`` kept current (account, positions,
every symbol's latest bar: its close and its pool depth), exactly one reply per batch on stdout echoing
its seq, flushed before the next line is read. Messages are plain dicts with the protocol's keys; u64
values arrive as decimal strings (``int()`` them). Amounts may be ints or u64 decimal strings;
client_id is filled in when left out. What the harness would call a protocol violation (an unknown
key, a malformed symbol, amount, bps or client_id, a client_id starting with ``_``, a reply over
MAX_BOT_LINE_BYTES) raises OrderError here, and so do more orders than
``hello["caps"]["max_orders_per_bar"]``. What the market refuses (cash, caps, slippage, no bar) is no
error: it comes back in the next batch's fills as status "rejected".

Market model (contracts/bars-protocol.ts): orders decided on bar k fill at bar k + 1's open, moved by
the modelled slippage and by constant-product price impact against that bar's pool depth, so size
costs: a trade worth a USDC meets the factor 1 + a/D1 (+ a/D2 for a SOL-quoted asset's SOL/USDC leg),
and the orders of one symbol at one open together pay what their sum would. Every bar carries the pool
depth at its open and its close (``o_depth``, ``c_depth``: USDC raw units per hop); the fills a batch
carries were priced at its bars' o and o_depth. ``state.depth_at(symbol)`` is the latest close's depth,
the best guess at the next open's, and ``state.impact_bps(symbol, amount)`` what a trade worth amount
would pay there; max_slippage_bps bounds the move from the latest close, the slippage and the impact
together. A sell's proceeds never fund a buy of the same reply; the last bar's orders are rejected
(after_end) and every position is flattened at the end, at its latest close against that close's
depth. A contest may open with a warm-up (``hello["warmup_bars"]``): its first batches are the bars
before the scored window, to build history from, and every order decided on one is rejected (warmup);
``state.warming_up`` says so, ``state.warmup_bars`` counts them (0 without one), and seq
``state.warmup_bars`` is the first batch you can trade on. stdout carries replies only (diagnostics:
stderr). Every entry runs twice and the two action logs must match byte for byte: never read the
clock, the environment or an unseeded random; seed ``random.Random`` from ``int(state.hello["seed"])``.
"""

import json
import re
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
#: Harness -> bot lines are at most this many UTF-8 bytes, the newline not counted (1 MiB).
MAX_HARNESS_LINE_BYTES = 1_048_576
#: Bot -> harness lines (replies) are at most this many UTF-8 bytes, the newline not counted (64 KiB).
MAX_BOT_LINE_BYTES = 65_536
#: Basis points per unit: 10_000 bps = 100%.
BPS = 10_000
U64_MAX = 2**64 - 1
#: USDC raw units per USDC (6 decimals): the unit of every pool depth.
USDC_RAW = 1_000_000

REJECT_REASONS = (
    "max_open_positions",
    "max_orders_per_bar",
    "max_weight",
    "insufficient_cash",
    "insufficient_position",
    "not_in_universe",
    "no_bar",
    "zero_amount",
    "duplicate_client_id",
    "below_min_notional",
    "max_slippage",
    "after_end",
    "warmup",
)

_U64_RE = re.compile(r"0|[1-9][0-9]{0,19}")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}")
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9-][A-Za-z0-9_-]{0,63}")
_KEYS = {
    "buy": ("side", "client_id", "symbol", "notional_quote_units", "max_slippage_bps"),
    "sell": ("side", "client_id", "symbol", "qty", "max_slippage_bps"),
}


class ProtocolError(Exception):
    """The harness sent something this shim cannot follow."""


class OrderError(Exception):
    """The bot returned something the harness would treat as a protocol violation."""


class Holding:
    """A held position (ints): its quantity at the symbol's qty_scale and its cost, fees included."""

    __slots__ = ("qty", "cost")

    def __init__(self, qty, cost):
        self.qty = qty
        self.cost = cost

    def __repr__(self):
        return f"Holding(qty={self.qty}, cost={self.cost})"


class BarsState:
    """Everything the bot knows, current before each callback.

    Unit-test a strategy with ``s = BarsState(hello); s.apply_batch(batch); on_batch(s, batch)``.
    """

    def __init__(self, hello):
        self.hello = hello
        #: Seq and close time of the batch being answered (-1 before the first).
        self.seq = -1
        self.t = -1
        units = hello["capital"]["quote_units"]
        self.account = {"cash_quote_units": units, "positions": [], "equity_quote_units": units}
        #: The account's cash and equity (cash plus every position at its latest close) as ints.
        self.cash = int(units)
        self.equity = int(units)
        #: Held positions by symbol (Holding).
        self.positions = {}
        #: Each symbol's latest bar dict, however many bars back; absent until its first.
        self.latest = {}
        self.symbols = {s["symbol"]: s for s in hello["symbols"]}
        #: The warm-up batches before the scored window (hello["warmup_bars"]), 0 without a warm-up.
        self.warmup_bars = hello.get("warmup_bars", 0)
        self._used = set()
        self._next = 1

    @property
    def warming_up(self):
        """True while the batch being answered is a warm-up batch (seq < warmup_bars): any order decided on it is rejected (warmup). Always False without a warm-up."""
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def close(self, symbol):
        """The symbol's latest close, or None before its first bar (a buy then is rejected no_bar)."""
        bar = self.latest.get(symbol)
        return None if bar is None else int(bar["c"])

    def value(self, symbol, qty, price=None):
        """What ``qty`` of the symbol is worth at ``price`` (default its latest close), truncated, in quote units."""
        s = self.symbols.get(symbol)
        if price is None:
            price = self.close(symbol)
        if s is None or price is None:
            return 0
        return qty * price * 10 ** self.hello["capital"]["quote_scale"] // 10 ** (s["qty_scale"] + s["price_scale"])

    def depth_at(self, symbol):
        """The pool depth at the symbol's latest close (its latest bar's c_depth: per hop, USDC raw units, ints), or None before its first bar."""
        bar = self.latest.get(symbol)
        return None if bar is None else [int(d) for d in bar["c_depth"]]

    def impact_bps(self, symbol, amount):
        """The price impact in bps, truncated, of one trade worth ``amount`` quote units on the pool depth_at(symbol), untouched.

        ``amount`` is a buy's spend after the fee, or a sell's quantity valued at the open: (M - 1) x 10000
        for M = 1 + a/D1 + a/D2, a in USDC raw units. A buy pays the slipped open times M, a sell gets it
        divided by M. None before the symbol's first bar.
        """
        depth = self.depth_at(symbol)
        if depth is None:
            return None
        # 1/D1 + 1/D2 = (D2 + D1) / (D1 D2): the sum of each depth's complement over their product.
        prod = 1
        for d in depth:
            prod *= d
        total = 0
        for i in range(len(depth)):
            other = 1
            for j, d in enumerate(depth):
                if j != i:
                    other *= d
            total += other
        return amount * USDC_RAW * BPS * total // (10 ** self.hello["capital"]["quote_scale"] * prod)

    def weight_room(self, symbol):
        """The most a buy of the symbol may spend in one reply under caps.max_weight_bps (before other buys of it)."""
        held = self.positions.get(symbol)
        room = self.hello["caps"]["max_weight_bps"] * self.equity // BPS - self.value(symbol, held.qty if held else 0)
        return room if room > 0 else 0

    def client_id(self):
        """A fresh client_id (o1, o2, ...) never used in this run."""
        cid = self._free_id(())
        self._used.add(cid)
        return cid

    def apply_batch(self, batch):
        """Fold a batch in (bars, then the account). run() calls this."""
        self.seq = batch["seq"]
        self.t = batch["t"]
        for bar in batch["bars"]:
            self.latest[bar["symbol"]] = bar
        self._set_account(batch["account"])

    def apply_done(self, done):
        """Fold the final account in. run() calls this before on_done."""
        self.seq = done["seq"]
        self._set_account(done["account"])

    def to_orders(self, result):
        """What on_batch returned as wire orders (missing client_ids assigned); raises OrderError on a violation or too many orders."""
        if result is None:
            return []
        if not isinstance(result, (list, tuple)):
            raise OrderError(f"batch {self.seq}: on_batch must return a list of orders, got {_describe(result)}")
        cap = self.hello["caps"]["max_orders_per_bar"]
        if len(result) > cap:
            raise OrderError(
                f"batch {self.seq}: on_batch returned {len(result)} orders, over hello.caps.max_orders_per_bar ({cap}): "
                f"the harness rejects every order past it"
            )
        checked = [_check_order(o, f"batch {self.seq} order {i}") for i, o in enumerate(result)]
        taken = {o["client_id"] for o in checked if o["client_id"] is not None}
        orders = []
        for o in checked:
            cid = o["client_id"]
            if cid is None:
                cid = self._free_id(taken)
            taken.add(cid)
            keys = _KEYS[o["side"]]
            orders.append({"side": o["side"], "client_id": cid, "symbol": o["symbol"], keys[3]: o["amount"], "max_slippage_bps": o["bps"]})
        return orders

    def record_orders(self, orders):
        """Record orders as sent: their ids are never assigned again."""
        for o in orders:
            self._used.add(o["client_id"])

    def _free_id(self, taken):
        while True:
            cid = f"o{self._next}"
            self._next += 1
            if cid not in self._used and cid not in taken:
                return cid

    def _set_account(self, account):
        self.account = account
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {p["symbol"]: Holding(int(p["qty"]), int(p["cost_quote_units"])) for p in account["positions"]}


def _check_order(value, where):
    if not isinstance(value, dict):
        raise OrderError(f"{where}: expected an order dict, got {_describe(value)}")
    side = value.get("side")
    if side not in ("buy", "sell"):
        raise OrderError(f'{where}: side must be "buy" or "sell", got {_describe(side)}')
    keys = _KEYS[side]
    for k in value:
        if k not in keys:
            raise OrderError(f"{where}: unknown key {_describe(k)} on a {side} order")
    symbol = value.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise OrderError(f"{where}: symbol must be a ticker of 1-20 [A-Z0-9], got {_describe(symbol)}")
    amount = to_u64(value.get(keys[3]), f"{where}: {keys[3]}")
    bps = value.get("max_slippage_bps")
    if type(bps) is not int or not 0 <= bps <= 10_000:
        raise OrderError(f"{where}: max_slippage_bps must be an int 0..10000, got {_describe(bps)}")
    cid = value.get("client_id")
    if cid is not None and (not isinstance(cid, str) or not _CLIENT_ID_RE.fullmatch(cid)):
        raise OrderError(
            f"{where}: client_id must be 1-64 of [A-Za-z0-9_-] not starting with _ (reserved for the harness's closes), "
            f"got {_describe(cid)}"
        )
    return {"side": side, "client_id": cid, "symbol": symbol, "amount": amount, "bps": bps}


def to_u64(value, what="amount"):
    """An amount (int or u64 decimal string) as a canonical u64 decimal string; raises OrderError otherwise."""
    n = None
    if type(value) is int:
        n = value
    elif isinstance(value, str) and _U64_RE.fullmatch(value):
        n = int(value)
    if n is None or n < 0 or n > U64_MAX:
        raise OrderError(f"{what} must be a u64 (int or decimal string), got {_describe(value)}")
    return str(n)


def _describe(value):
    if isinstance(value, str):
        return json.dumps(value if len(value) <= 80 else value[:77] + "...")
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return f"a {type(value).__name__}"


def read_lines(src, max_bytes=MAX_HARNESS_LINE_BYTES):
    """Yield the lines of a binary stream (newline-terminated, a trailing CR dropped) as UTF-8.

    A line longer than max_bytes raises ProtocolError; an unterminated last line is yielded.
    """
    line_no = 0
    while True:
        raw = src.readline(max_bytes + 1)
        if not raw:
            return
        line_no += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        elif len(raw) > max_bytes:
            raise ProtocolError(f"input line {line_no} is over {max_bytes} bytes")
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(f"input line {line_no} is not valid UTF-8") from None
        yield text


class RunResult:
    """What run() returns after done: the final state, the done message and how many batches were answered."""

    __slots__ = ("state", "done", "batches")

    def __init__(self, state, done, batches):
        self.state = state
        self.done = done
        self.batches = batches


def encode_reply(seq, orders):
    """The reply line for a batch (no newline), byte for byte what bars/sdk/makefun-bars.ts writes."""
    return json.dumps({"type": "reply", "seq": seq, "orders": orders}, separators=(",", ":"), ensure_ascii=False)


def run(on_batch, on_hello=None, on_done=None, *, input=None, output=None):
    """Speak agent-fun/bars-replay/v1 until done, then stop reading and return a RunResult.

    ``input`` is a binary stream (sys.stdin.buffer by default), ``output`` a text stream (sys.stdout by
    default), flushed after every reply. Raises ProtocolError, OrderError or what a callback raised;
    uncaught, that ends the process with exit code 1.
    """
    src = input if input is not None else sys.stdin.buffer
    out = output if output is not None else sys.stdout
    state = None
    line_no = 0
    for line in read_lines(src):
        line_no += 1
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError(f"input line {line_no} is not JSON") from None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if state is not None:
                raise ProtocolError(f"input line {line_no}: a second hello")
            if msg.get("protocol") != PROTOCOL:
                raise ProtocolError(f"the harness speaks {_describe(msg.get('protocol'))}; this shim speaks {PROTOCOL}")
            state = BarsState(msg)
            if on_hello is not None:
                on_hello(state)
        elif kind in ("batch", "done"):
            if state is None:
                raise ProtocolError(f"input line {line_no}: {kind} before hello")
            seq = msg.get("seq")
            account = msg.get("account")
            shaped = type(seq) is int and seq == state.seq + 1 and isinstance(account, dict) and isinstance(account.get("positions"), list)
            if kind == "done":
                if not shaped:
                    raise ProtocolError(f"input line {line_no}: malformed done (seq {_describe(seq)} after batch {state.seq})")
                state.apply_done(msg)
                if on_done is not None:
                    on_done(state, msg)
                return RunResult(state, msg, msg["seq"])
            if not (shaped and type(msg.get("t")) is int and isinstance(msg.get("bars"), list) and isinstance(msg.get("fills"), list)):
                raise ProtocolError(f"input line {line_no}: malformed batch (seq {_describe(seq)} after {state.seq})")
            state.apply_batch(msg)
            orders = state.to_orders(on_batch(state, msg))
            text = encode_reply(msg["seq"], orders)
            size = len(text.encode("utf-8"))
            if size > MAX_BOT_LINE_BYTES:
                raise OrderError(f"batch {msg['seq']}: the reply is {size} bytes (max {MAX_BOT_LINE_BYTES})")
            state.record_orders(orders)
            out.write(text + "\n")
            out.flush()
        else:
            raise ProtocolError(f"input line {line_no}: unknown message type {_describe(kind)}")
    if state is None:
        raise ProtocolError("input ended before hello")
    raise ProtocolError(f"input ended before done (after {state.seq + 1} batches)")
