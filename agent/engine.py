"""GridFly engine: the mechanics of the desk that ran the judged week.

PUBLIC (this file): the session loop and its order of operations, the order
path through the Alpaca Trading API (package limit first, per-leg fallback,
one reprice, never chase, idempotent retries, every rejection journaled), the
read-only reconcile tick, the containment engine's trigger-confirm-execute
shape with graceful degradation, hold-to-cash-settlement bookkeeping, and the
journal format the public site is rendered from.

PRIVATE (not in this repository): the grid, sizing and reserves, body and
wing selection, the realized-volatility measure and its ceiling, credit
floors, the containment trigger and its timing, the explorer's caps and
window, and every calibrated value. Wherever the code would hold one, the
value or the body reads "this is redacted". The loader fails closed on any
redacted value in any mode: this skeleton is meant to be read, not run.

Run (with a complete private config.json):  python -m agent.engine
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from concurrent import futures
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from agent.gate_council import CouncilConfig, convene
from agent.signal_provider import SignalProvider, StubSignalProvider

ET = ZoneInfo("America/New_York")
REDACTED = "this is redacted"
REDACTED_VALUES = {"REDACTED", REDACTED}

logger = logging.getLogger("gridfly")

# Every decision value. The public example ships all of them redacted and
# the engine refuses to start, in any mode, while one is still redacted.
TRADE_REQUIRES = [
    "grid.slots_et", "grid.late_entry_grace_s",
    "sizing.slot_weights", "sizing.reserve_explorer_frac", "sizing.reserve_ops_frac",
    "sizing.reserve_release_explorer_et", "sizing.reserve_release_ops_et", "sizing.max_lots_per_slot",
    "structure.wing_width_pct",
    "entry.limit_inside_mid", "entry.cancel_after_s", "entry.min_credit",
    "gates.rv_lookback_min", "gates.rv_ceiling_bps", "gates.credit_floor_am", "gates.credit_floor_pm",
    "gates.credit_floor_split_et",
    "containment.cut_fraction", "containment.confirm_seconds", "containment.poll_seconds",
    "containment.cancel_after_s", "containment.retry_cap",
    "explorer.max_trades_per_day", "explorer.cooldown_s", "explorer.hold_s",
    "explorer.vertical_width_pts", "explorer.window_start_et", "explorer.window_end_et",
    "council.credit_floor_frac_of_width", "council.sentiment_extreme_abs",
]

CSV_FIELDS = ["ts_et", "slot", "mode", "action", "S", "K", "wing_up",
              "wing_dn", "credit_mid", "credit_bid", "atm_iv_proxy",
              "order_id", "fill_status", "fill_credit", "rv30_bps", "lots",
              "council", "note"]


# ------------------------------------------------------------- config ----
def load_config(path: str = "config.json") -> dict:
    """config.json (private, gitignored) over config.example.json."""
    src = path if os.path.exists(path) else "config.example.json"
    with open(src, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_source"] = src
    return cfg


def cfgval(cfg: dict, dotted: str, default=None):
    """cfg['a']['b'] for 'a.b'; REDACTED placeholders read as `default`."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node in REDACTED_VALUES else node


def missing_calibration(cfg: dict) -> list:
    return [k for k in TRADE_REQUIRES if cfgval(cfg, k) is None]


def req(cfg: dict, dotted: str):
    """A decision value with NO default: redacted or absent means stop."""
    v = cfgval(cfg, dotted)
    if v is None:
        raise SystemExit(f"{dotted}: {REDACTED} (private calibration; set it in config.json)")
    return v


def parse_et(hhmm: str) -> tuple:
    h, m = hhmm.split(":")
    return int(h), int(m)


# ------------------------------------------------------------ journal ----
class Journal:
    """Append-only evidence trail: a per-day CSV of every decision/order and
    a JSONL hypothesis journal. The desk must be able to explain any trade it
    made — and any trade it declined."""

    def __init__(self, dirpath: str, mode: str):
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode

    def _csv_path(self) -> Path:
        return self.dir / f"gridfly_{datetime.now(ET):%Y%m%d}.csv"

    def row(self, **kw) -> None:
        p = self._csv_path()
        new = not p.exists()
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            kw.setdefault("ts_et", datetime.now(ET).strftime("%H:%M:%S"))
            kw.setdefault("mode", self.mode)
            w.writerow({k: kw.get(k, "") for k in CSV_FIELDS})

    def read_rows(self) -> list:
        p = self._csv_path()
        if not p.exists():
            return []
        with open(p, newline="") as f:
            return list(csv.DictReader(f))

    def note(self, sleeve: str, hypothesis: str, expression: str = "",
             verdict: str = "OPEN") -> None:
        rec = {"ts": datetime.now(ET).isoformat(timespec="seconds"),
               "sleeve": sleeve, "hypothesis": hypothesis,
               "expression": expression, "verdict": verdict}
        with open(self.dir / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------ helpers ----
def call_with_timeout(fn, *args, timeout: int = 20, **kw):
    """Every broker/data call goes through an executor with a hard timeout —
    a hung HTTP call must cost a tick, never the session."""
    with futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn, *args, **kw).result(timeout=timeout)


def trailing_rv_bps(stk_data, symbol: str, lookback_min: int) -> Optional[float]:
    """The council's decisive input: a trailing realized-volatility measure of
    the underlying over a private lookback, in basis points of spot. Returns
    None on ANY failure; the council fails closed on None, never open.

    The measure itself is a private calibration."""
    raise NotImplementedError(REDACTED + ": the realized-volatility measure")


def package_close_mid(legs, quotes) -> Optional[float]:
    """Net package mid for CLOSING a structure, Alpaca MLEG signed convention
    (positive = net debit). legs: [(symbol, 'buy'|'sell', ratio)]. A buy leg
    (covering a short) with no ask cannot be priced -> None; a sell leg with
    no bid contributes 0 (a worthless wing must not block the package)."""
    net = 0.0
    for sym, side, ratio in legs:
        bid, ask = quotes.get(sym, (0.0, 0.0))
        if side == "buy":
            if ask <= 0:
                return None
            net += ratio * (bid + ask) / 2
        else:
            net -= ratio * (bid + ask) / 2 if ask > 0 else 0.0
    net = round(net, 2)
    return net if net != 0 else 0.01   # zero is unpriceable as a limit


def expected_book_from_rows(rows, d8: str, root: str) -> dict:
    """Journal-implied option book {symbol: signed qty} for reconciliation.
    Filled/partial `order` rows add a fly; filled `wing_cut` rows reverse it."""
    book = {}
    for row in rows:
        action = row.get("action")
        if action not in ("order", "wing_cut"):
            continue
        st = row.get("fill_status") or ""
        if st != "filled" and not st.startswith("partial_"):
            continue
        try:
            lots = int(float(row.get("lots") or 0))
            K = float(row["K"])
            up, dn = float(row["wing_up"]), float(row["wing_dn"])
        except (ValueError, TypeError, KeyError):
            continue
        if lots <= 0:
            continue
        sign = -1 if action == "wing_cut" else 1

        def sym(cp, k):
            return f"{root}{d8}{cp}{int(round(k * 1000)):08d}"
        for s, q in ((sym("C", K), -lots * sign), (sym("P", K), -lots * sign),
                     (sym("C", up), lots * sign), (sym("P", dn), lots * sign)):
            book[s] = book.get(s, 0) + q
    return {s: q for s, q in book.items() if q}


class ReleaseCalendar:
    """Scheduled macro releases from a local JSON file the operator maintains:
    {"YYYY-MM-DD": "RELEASE NAME"}. A missing/broken file does not silently
    fail open — the engine flags calendar_ok=False on every journal row and
    warns loudly at startup, so an unwatched calendar is always visible."""

    def __init__(self, path: Optional[str]):
        self.ok, self.days = False, {}
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.days = {str(k): str(v) for k, v in json.load(f).items()}
            self.ok = True
        except FileNotFoundError:
            logger.warning(f"release calendar {path} not found — "
                           "calendar_ok=False on all rows")
        except Exception as e:
            logger.warning(f"release calendar unreadable ({e}) — "
                           "calendar_ok=False on all rows")

    def today(self) -> tuple:
        """(release_today, release_name) for the current ET date."""
        name = self.days.get(datetime.now(ET).strftime("%Y-%m-%d"))
        return bool(name), name or ""


# ------------------------------------------------------------- engine ----
class Engine:
    def __init__(self, cfg: dict, signal_provider: Optional[SignalProvider] = None):
        self.cfg = cfg
        self.mode = str(cfgval(cfg, "mode", "log")).lower()

        missing = missing_calibration(cfg)
        if missing:
            raise SystemExit(f"{len(missing)} decision value(s) are redacted or absent "
                             f"({', '.join(missing[:5])} ...): {REDACTED}. The public "
                             "skeleton does not run; provide a complete private config.json.")

        key = cfgval(cfg, "alpaca.api_key_id", "")
        sec = cfgval(cfg, "alpaca.api_secret_key", "")
        if not key or "YOUR" in key.upper():
            raise SystemExit("no Alpaca API key configured — copy "
                             "config.example.json to config.json and fill in "
                             "paper credentials")
        # PAPER GUARD: this is a hackathon agent. Alpaca paper keys start
        # with 'PK'; the engine refuses to trade on anything else.
        self.paper = key.startswith("PK") and bool(cfgval(cfg, "alpaca.paper", True))
        if self.mode == "trade" and not self.paper:
            raise SystemExit("REFUSING trade mode on a non-paper key. "
                             "GridFly trades paper accounts only.")

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        self.trading = TradingClient(key, sec, paper=True)
        self.opt_data = OptionHistoricalDataClient(key, sec)
        self.stk_data = StockHistoricalDataClient(key, sec)

        self.index = cfgval(cfg, "underlying.index_symbol", "SPX")
        self.root = cfgval(cfg, "underlying.option_root", "SPXW")
        self.proxy = cfgval(cfg, "underlying.spot_proxy_symbol", "SPY")
        self.mult = float(cfgval(cfg, "underlying.index_multiplier", 10.0))

        self.slots = sorted(parse_et(s) for s in
                            cfgval(cfg, "grid.slots_et", []))
        w = cfgval(cfg, "sizing.slot_weights")   # None while REDACTED
        self.slot_weights = ({parse_et(k): float(v) for k, v in w.items()}
                             if isinstance(w, dict)
                             else {s: 1.0 for s in self.slots})

        self.journal = Journal(cfgval(cfg, "journal_dir", "journal"), self.mode)
        self.calendar = ReleaseCalendar(cfgval(cfg, "gates.release_day_file"))
        self.council_cfg = CouncilConfig.from_agent_config(cfg)
        self.signals = signal_provider or StubSignalProvider()

        # containment / reconcile state; the open book is rebuilt from the
        # journal so a mid-session restart keeps cutting correctly.
        self.open_flies = self._load_open_flies()      # {(K, up, dn): lots}
        self._book_day = datetime.now(ET).strftime("%Y%m%d")
        self._cut_pending: dict = {}                   # key -> first-trigger ts
        self._cut_attempts: dict = {}                  # key -> failed attempts
        self._last_cut_poll = 0.0
        self._last_recon = 0.0
        self._last_recon_sig = ""

        logger.info(f"gridfly up: mode={self.mode} paper={self.paper} "
                    f"underlying={self.index}/{self.root} "
                    f"slots={len(self.slots)} "
                    f"open_structures={len(self.open_flies)} "
                    f"calendar={'ok' if self.calendar.ok else 'UNAVAILABLE'} "
                    f"config={cfg.get('_source')}")
        if missing:
            logger.info(f"calibration placeholders present: {', '.join(missing)}")

    # ---------------------------------------------------- market data ----
    def spot_hint(self) -> float:
        """Approximate index level: liquid ETF proxy x multiplier.
        This is only an ANCHOR for the strike window — the tradable level is
        re-derived from the option chain's put-call parity in run_slot, which
        removes the proxy's basis error. TODO(private): production runs a
        native index-level feed here (Alpaca's index-values endpoint works
        too, where enabled); the parity refinement below stays either way."""
        from alpaca.data.requests import StockLatestTradeRequest
        t = call_with_timeout(
            self.stk_data.get_stock_latest_trade,
            StockLatestTradeRequest(symbol_or_symbols=self.proxy))
        return float(t[self.proxy].price) * self.mult

    def chain_strikes(self, today, S: Optional[float] = None) -> dict:
        """0DTE contracts as {strike: {'call': sym, 'put': sym}}. The request
        is BOUNDED by a strike window — an unbounded page of a large 0DTE
        listing can silently truncate to all-calls and break the parity scan."""
        from alpaca.trading.requests import GetOptionContractsRequest
        kw = {}
        if S:
            wing = float(req(self.cfg, "structure.wing_width_pct")) * S
            kw = {"strike_price_gte": str(round(S - wing - 25)),
                  "strike_price_lte": str(round(S + wing + 25))}
        req = GetOptionContractsRequest(underlying_symbols=[self.index],
                                        expiration_date=today, limit=500, **kw)
        res = call_with_timeout(self.trading.get_option_contracts, req)
        out: dict = {}
        for c in (res.option_contracts or []):
            t = str(c.type.value if hasattr(c.type, "value") else c.type)
            out.setdefault(float(c.strike_price), {})[t] = c.symbol
        return out

    def quotes(self, symbols: list) -> dict:
        from alpaca.data.requests import OptionLatestQuoteRequest
        q = call_with_timeout(self.opt_data.get_option_latest_quote,
                              OptionLatestQuoteRequest(symbol_or_symbols=symbols))
        return {s: (float(q[s].bid_price or 0), float(q[s].ask_price or 0))
                for s in symbols if s in q}

    def _fly_symbols(self, K: float, up: float, dn: float) -> dict:
        d8 = datetime.now(ET).strftime("%y%m%d")

        def sym(cp, k):
            return f"{self.root}{d8}{cp}{int(round(k * 1000)):08d}"
        return {"sc": sym("C", K), "sp": sym("P", K),
                "bc": sym("C", up), "bp": sym("P", dn)}

    # --------------------------------------------------------- sizing ----
    def _avail_bp(self) -> float:
        a = call_with_timeout(self.trading.get_account)
        return float(getattr(a, "options_buying_power", 0) or 0)

    def _bp_lots(self, slot_hm: tuple, credit_mid: float, wing_pts: float):
        """Sizes a slot from LIVE buying power: reserves held for the other
        sleeves release intraday, and an unfilled or skipped slot's budget rolls
        forward to later slots. Returns (lots, note, avail_bp).

        The weighting, the reserves and their release schedule are private."""
        raise NotImplementedError(REDACTED + ": the sizing rule")

    # ------------------------------------------------------- one slot ----
    def run_slot(self, slot_name: str) -> None:
        cfg = self.cfg
        today = datetime.now(ET).date()
        S = self.spot_hint()
        strikes = self.chain_strikes(today, S)
        if not strikes:
            self.journal.row(slot=slot_name, action="skip", S=round(S, 2),
                             note="no_0dte_chain")
            return
        ks = sorted(strikes)

        # ATM by put-call parity: among the strikes nearest the anchor, pick
        # the one whose call and put mids are closest, then REFINE S to the
        # parity-implied level K + (cm - pm) — the chain's own opinion of
        # spot, which removes the proxy anchor's basis error.
        cand = sorted(ks, key=lambda x: abs(x - S))[:9]
        csyms = [strikes[k][t] for k in cand
                 for t in ("call", "put") if t in strikes.get(k, {})]
        pq = self.quotes(csyms)
        best, bestdiff, _cm, _pm = None, 9e9, 0.0, 0.0
        for k in cand:
            cs, ps = strikes[k].get("call"), strikes[k].get("put")
            qc, qp = pq.get(cs), pq.get(ps)
            if not qc or not qp or qc[1] <= 0 or qp[1] <= 0:
                continue
            cm, pm = (qc[0] + qc[1]) / 2, (qp[0] + qp[1]) / 2
            if abs(cm - pm) < bestdiff:
                best, bestdiff, _cm, _pm = k, abs(cm - pm), cm, pm
        if best is None:
            self.journal.row(slot=slot_name, action="skip", S=round(S, 2),
                             note="no_parity_atm")
            return
        K = best
        S = round(K + (_cm - _pm), 2)

        wing_pct = float(req(cfg, "structure.wing_width_pct"))
        up = min(ks, key=lambda x: abs(x - (K + wing_pct * S)))
        dn = min(ks, key=lambda x: abs(x - (K - wing_pct * S)))
        legs_ok = ("call" in strikes.get(K, {}) and "put" in strikes.get(K, {})
                   and "call" in strikes.get(up, {})
                   and "put" in strikes.get(dn, {}) and up > K > dn)
        if not legs_ok:
            self.journal.row(slot=slot_name, action="skip", S=S, K=K,
                             wing_up=up, wing_dn=dn, note="missing_leg_or_wing")
            return
        syms = {"sc": strikes[K]["call"], "sp": strikes[K]["put"],
                "bc": strikes[up]["call"], "bp": strikes[dn]["put"]}
        q = self.quotes(list(syms.values()))
        if len(q) < 4 or any(q[s][1] <= 0 for s in syms.values()):
            self.journal.row(slot=slot_name, action="skip", S=S, K=K,
                             wing_up=up, wing_dn=dn, note="missing_quotes")
            return
        mid = {k: (q[s][0] + q[s][1]) / 2 for k, s in syms.items()}
        credit_mid = round(mid["sc"] + mid["sp"] - mid["bc"] - mid["bp"], 2)
        credit_bid = round(q[syms["sc"]][0] + q[syms["sp"]][0]
                           - q[syms["bc"]][1] - q[syms["bp"]][1], 2)
        iv_proxy = round((mid["sc"] + mid["sp"]) / S * 1e4, 1)
        tick_in = float(req(cfg, "entry.limit_inside_mid"))
        credit_limit = round(max(credit_mid - tick_in, 0.05), 2)
        wing_pts = up - K
        rv30 = trailing_rv_bps(self.stk_data, self.proxy,
                               int(req(cfg, "gates.rv_lookback_min")))

        # ---- Gate Council: RV decisive, calendar veto, observers narrate.
        release_today, release_name = self.calendar.today()
        decision = convene(
            rv30=rv30,
            rv_ceiling=cfgval(cfg, "gates.rv_ceiling_bps"),
            credit=credit_mid, wing_avg=(wing_pts + (K - dn)) / 2,
            floor_frac=cfgval(cfg, "council.credit_floor_frac_of_width"),
            release_today=release_today, release_name=release_name,
            legacy_skip=True,
            override_play_inline=bool(cfgval(cfg, "gates.release_day_override",
                                             False)),
            council_cfg=self.council_cfg)

        # ---- deterministic floors + sizing (risk engine, not council) ----
        lots, size_note, _avail = self._bp_lots(parse_et(slot_name),
                                                credit_mid, wing_pts)
        gate_note = ""
        split = parse_et(req(cfg, "gates.credit_floor_split_et"))
        floor = (cfgval(cfg, "gates.credit_floor_am")
                 if parse_et(slot_name) < split
                 else cfgval(cfg, "gates.credit_floor_pm"))
        if floor is not None and credit_mid < float(floor):
            lots, gate_note = 0, f"credit_floor {credit_mid}<{floor}"

        base = dict(slot=slot_name, S=S, K=K, wing_up=up, wing_dn=dn,
                    credit_mid=credit_mid, credit_bid=credit_bid,
                    atm_iv_proxy=iv_proxy, rv30_bps=rv30, lots=lots,
                    council=decision.journal_note())
        note = ";".join(filter(None, (size_note, gate_note,
                                      "" if self.calendar.ok else "calendar_unavailable")))
        if credit_mid <= float(req(cfg, "entry.min_credit")):
            self.journal.row(action="skip", note="credit_too_small", **base)
            return
        if not decision.enter:
            self.journal.row(action="log_only",
                             note=";".join(filter(None, ("council_skip", note))),
                             **base)
            self.journal.note("HARVESTER", f"{slot_name}: council SKIP",
                              decision.narrative, "PASS")
            logger.info(f"{slot_name}: council SKIP — {decision.narrative}")
            return
        if self.mode != "trade" or lots == 0:
            self.journal.row(action="log_only", note=note, **base)
            logger.info(f"{slot_name}: no order ({gate_note or 'log mode'})")
            return
        logger.warning(f"{slot_name}: SELL {lots}x {K} fly +/-{wing_pts} "
                       f"credit_limit={credit_limit} ({size_note})")
        self.journal.note("HARVESTER",
                          f"{slot_name}: council ENTER -> sell {lots}x {K} "
                          f"fly +/-{wing_pts} @ >= {credit_limit}",
                          decision.narrative, "OPEN")
        self.place_fly(syms, credit_limit, base, qty=lots,
                       extra_note=note[:110])

    # ------------------------------------------------------ execution ----
    def place_fly(self, syms: dict, credit_limit: float, base: dict,
                  qty: int = 1, extra_note: str = "") -> None:
        """No-chase entry: ONE MLEG limit inside the net mid, canceled after
        entry.cancel_after_s. Before recording, the broker's LAST WORD is
        re-fetched: a failed cancel can mean the order just filled, and a
        multi-lot package can fill PARTIALLY — the journal records the actual
        book, never an assumed state transition."""
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce
        legs = [OptionLegRequest(symbol=syms["sc"], side=OrderSide.SELL, ratio_qty=1),
                OptionLegRequest(symbol=syms["sp"], side=OrderSide.SELL, ratio_qty=1),
                OptionLegRequest(symbol=syms["bc"], side=OrderSide.BUY, ratio_qty=1),
                OptionLegRequest(symbol=syms["bp"], side=OrderSide.BUY, ratio_qty=1)]
        req = LimitOrderRequest(qty=qty, order_class=OrderClass.MLEG,
                                time_in_force=TimeInForce.DAY,
                                # Alpaca MLEG SIGNED convention: negative = credit
                                limit_price=-abs(credit_limit), legs=legs)
        try:
            order = call_with_timeout(self.trading.submit_order, req, timeout=30)
        except Exception as e:
            self.journal.row(action="order_error",
                             note=f"{type(e).__name__}: {e}"[:120], **base)
            logger.error(f"submit failed: {e}")
            return
        oid = str(order.id)
        deadline = time.time() + float(req(self.cfg, "entry.cancel_after_s"))
        status, fill_credit = "pending", ""
        while time.time() < deadline:
            time.sleep(5)
            try:
                o = call_with_timeout(self.trading.get_order_by_id, oid)
            except Exception:
                continue
            st = str(o.status.value if hasattr(o.status, "value") else o.status)
            if st == "filled":
                status = "filled"
                fill_credit = o.filled_avg_price or credit_limit
                break
            if st in ("canceled", "rejected", "expired"):
                status = st
                break
        if status == "pending":
            try:
                call_with_timeout(self.trading.cancel_order_by_id, oid)
                status = "canceled_timeout"
            except Exception:
                status = "cancel_failed"
        try:  # broker's last word: late fills and partials
            o = call_with_timeout(self.trading.get_order_by_id, oid)
            st = str(o.status.value if hasattr(o.status, "value") else o.status)
            fq = int(float(getattr(o, "filled_qty", 0) or 0))
            if st == "filled":
                status, fill_credit = "filled", o.filled_avg_price or credit_limit
            elif fq > 0:
                logger.warning(f"{base['slot']}: PARTIAL FILL {fq}/{qty} lots "
                               f"(order {st}) — recording actual book")
                status = f"partial_{fq}of{qty}"
                fill_credit = o.filled_avg_price or ""
                base["lots"] = fq
        except Exception:
            pass
        self.journal.row(action="order", order_id=oid, fill_status=status,
                         fill_credit=fill_credit, note=extra_note, **base)
        logger.info(f"{base['slot']}: {status} credit_limit={credit_limit} oid={oid}")
        if status == "filled" or status.startswith("partial_"):
            key = (float(base["K"]), float(base["wing_up"]), float(base["wing_dn"]))
            self.open_flies[key] = self.open_flies.get(key, 0) + int(base["lots"])

    # ---------------------------------------------------- containment ----
    def _load_open_flies(self) -> dict:
        """Rebuild today's open structures from the journal: filled/partial
        `order` rows add lots, filled `wing_cut` rows remove them — so a
        restart mid-session keeps containment armed on the real book."""
        book: dict = {}
        try:
            for row in self.journal.read_rows():
                action = row.get("action")
                st = row.get("fill_status") or ""
                if action not in ("order", "wing_cut"):
                    continue
                if st != "filled" and not st.startswith("partial_"):
                    continue
                try:
                    key = (float(row["K"]), float(row["wing_up"]),
                           float(row["wing_dn"]))
                    lots = int(float(row.get("lots") or 0))
                except (ValueError, TypeError, KeyError):
                    continue
                if lots <= 0:
                    continue
                book[key] = book.get(key, 0) + (lots if action == "order" else -lots)
        except Exception as e:
            logger.warning(f"open-book rebuild failed ({str(e)[:60]}); starting empty")
        return {k: v for k, v in book.items() if v > 0}

    def containment_tick(self) -> None:
        """Tail containment with CONFIRMATION: a trigger must hold continuously
        for a confirmation window before any cut executes, so transient touches
        are not breaks. While the trigger holds, execution retries on later
        ticks; after the retry cap it degrades to per-leg closes. The trigger
        and the window are private calibrations."""
        f = cfgval(self.cfg, "containment.cut_fraction")
        now = datetime.now(ET)
        # day rollover: a desk running across sessions must never act on
        # yesterday's settled book (cutting a stale fly would OPEN new legs).
        today = now.strftime("%Y%m%d")
        if self._book_day != today:
            self.open_flies = self._load_open_flies()
            self._cut_pending, self._cut_attempts = {}, {}
            self._book_day = today
            logger.info(f"day rollover -> book reset ({len(self.open_flies)} open)")
        if f is None or self.mode != "trade" or not self.open_flies:
            return
        if now.weekday() >= 5 or not ((9, 35) <= (now.hour, now.minute) < (16, 0)):
            return
        poll = float(req(self.cfg, "containment.poll_seconds"))
        if time.time() - self._last_cut_poll < poll:
            return
        self._last_cut_poll = time.time()
        try:
            S = self.spot_hint()
        except Exception as e:
            logger.warning(f"containment: spot fetch failed ({str(e)[:50]})")
            return
        confirm_s = float(req(self.cfg, "containment.confirm_seconds"))
        for (K, up, dn), lots in list(self.open_flies.items()):
            key = (K, up, dn)
            trig = self._trigger_hit(S, K, up, dn, f)
            if not trig:
                self._cut_pending.pop(key, None)
                continue
            t0 = self._cut_pending.setdefault(key, time.time())
            if time.time() - t0 < confirm_s:
                continue
            logger.warning(f"CUT CONFIRMED ({confirm_s:.0f}s): spot {S:.2f} vs "
                           f"K={K} [{dn}/{up}] f={f} lots={lots}")
            self._cut_structure(K, up, dn, lots, S)

    def _trigger_hit(self, S, K, up, dn, f) -> bool:
        """Is this structure under threat at spot S? The trigger geometry and
        its fraction are a private calibration."""
        raise NotImplementedError(REDACTED + ": the containment trigger")

    def _cut_structure(self, K, up, dn, lots, trig_spot) -> None:
        """Buy the whole structure back. All legs are CLOSING direction, so
        strike-netting across flies can never create an opening leg. Ladder:
        MLEG limit one tick through the package mid -> cancel-on-timeout ->
        retry next tick while the trigger holds -> per-leg fallback after
        retry_cap failed package attempts."""
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce
        key = (K, up, dn)
        syms = self._fly_symbols(K, up, dn)
        retry_cap = int(req(self.cfg, "containment.retry_cap"))
        if self._cut_attempts.get(key, 0) >= retry_cap:
            self._cut_per_leg_fallback(key, syms, lots, trig_spot)
            return
        try:
            q = self.quotes(list(syms.values()))
        except Exception as e:
            logger.warning(f"cut: quotes failed ({str(e)[:50]}); retry next tick")
            self._cut_attempts[key] = self._cut_attempts.get(key, 0) + 1
            return
        tup = [(syms["sc"], "buy", 1.0), (syms["sp"], "buy", 1.0),
               (syms["bc"], "sell", 1.0), (syms["bp"], "sell", 1.0)]
        pmid = package_close_mid(tup, q)
        if pmid is None:
            logger.warning("cut: package unpriceable (no ask on a body); retry")
            self._cut_attempts[key] = self._cut_attempts.get(key, 0) + 1
            return
        limit = round(pmid + 0.01, 2)   # one tick through, debit positive
        legs = [OptionLegRequest(symbol=syms["sc"], side=OrderSide.BUY, ratio_qty=1),
                OptionLegRequest(symbol=syms["sp"], side=OrderSide.BUY, ratio_qty=1),
                OptionLegRequest(symbol=syms["bc"], side=OrderSide.SELL, ratio_qty=1),
                OptionLegRequest(symbol=syms["bp"], side=OrderSide.SELL, ratio_qty=1)]
        base = dict(slot="cut", S=round(trig_spot, 2), K=K, wing_up=up,
                    wing_dn=dn, credit_mid=pmid, lots=lots)
        try:
            req = LimitOrderRequest(qty=lots, order_class=OrderClass.MLEG,
                                    time_in_force=TimeInForce.DAY,
                                    limit_price=limit, legs=legs)
            order = call_with_timeout(self.trading.submit_order, req, timeout=30)
        except Exception as e:
            self._cut_attempts[key] = self._cut_attempts.get(key, 0) + 1
            self.journal.row(action="wing_cut", fill_status="order_error",
                             note=f"{type(e).__name__}: {e}"[:120], **base)
            logger.error(f"cut submit failed: {str(e)[:100]}")
            return
        oid = str(order.id)
        deadline = time.time() + float(req(self.cfg, "containment.cancel_after_s"))
        status, fill_px = "pending", ""
        while time.time() < deadline:
            time.sleep(5)
            try:
                o = call_with_timeout(self.trading.get_order_by_id, oid)
            except Exception:
                continue
            st = str(o.status.value if hasattr(o.status, "value") else o.status)
            if st == "filled":
                status, fill_px = "filled", o.filled_avg_price
                break
            if st in ("canceled", "rejected", "expired"):
                status = st
                break
        if status == "pending":
            try:
                call_with_timeout(self.trading.cancel_order_by_id, oid)
                status = "canceled_timeout"
            except Exception:
                status = "cancel_failed"
        try:  # broker's last word (same discipline as entries)
            o = call_with_timeout(self.trading.get_order_by_id, oid)
            st = str(o.status.value if hasattr(o.status, "value") else o.status)
            fq = int(float(getattr(o, "filled_qty", 0) or 0))
            if st == "filled":
                status, fill_px = "filled", o.filled_avg_price
            elif fq > 0:
                status = f"partial_{fq}of{lots}"
                fill_px = o.filled_avg_price or ""
                base["lots"] = fq
        except Exception:
            pass
        self.journal.row(action="wing_cut", order_id=oid, fill_status=status,
                         fill_credit=fill_px, note=f"trigger f={f_note(self.cfg)}",
                         **base)
        logger.info(f"cut: {status} K={K} lots={base['lots']} debit={fill_px or pmid}")
        if status == "filled":
            self.open_flies.pop(key, None)
            self._cut_pending.pop(key, None)
            self._cut_attempts.pop(key, None)
            self.journal.note("HARVESTER", f"containment cut executed K={K}",
                              f"debit {fill_px}", "PASS")
        elif status.startswith("partial_"):
            left = lots - int(base["lots"])
            if left > 0:
                self.open_flies[key] = left
            else:
                self.open_flies.pop(key, None)
                self._cut_pending.pop(key, None)
                self._cut_attempts.pop(key, None)
        else:
            self._cut_attempts[key] = self._cut_attempts.get(key, 0) + 1

    def _cut_per_leg_fallback(self, key, syms, lots, trig_spot) -> None:
        """Last rung of the ladder: the package book would not fill after
        retry_cap attempts, so close leg-by-leg via close_position (shorts
        first — never transiently naked). One pass; results journaled."""
        K, up, dn = key
        logger.warning(f"cut: per-leg fallback K={K} lots={lots}")
        ok = True
        for leg in ("sc", "sp", "bc", "bp"):   # shorts first
            try:
                call_with_timeout(self.trading.close_position, syms[leg])
            except Exception as e:
                ok = False
                logger.error(f"cut per-leg {syms[leg]} failed: {str(e)[:70]}")
        self.journal.row(action="wing_cut", slot="cut", S=round(trig_spot, 2),
                         K=K, wing_up=up, wing_dn=dn, lots=lots,
                         fill_status="per_leg" if ok else "per_leg_partial",
                         note="package retries exhausted -> per-leg fallback")
        if ok:
            self.open_flies.pop(key, None)
            self._cut_pending.pop(key, None)
        self._cut_attempts.pop(key, None)   # reconcile_tick audits the rest

    # -------------------------------------------------- reconciliation ----
    RECON_INTERVAL_S = 600

    def reconcile_tick(self) -> None:
        """Every 10 minutes: broker's open option legs vs the journal-implied
        book. Read-only — it never places orders; it makes drift VISIBLE
        (warn + journal row) within minutes instead of at settlement. An
        unchanged mismatch warns once; returning to sync is logged too."""
        now = datetime.now(ET)
        if now.weekday() >= 5 or not ((9, 35) <= (now.hour, now.minute) < (16, 0)):
            return
        if time.time() - self._last_recon < self.RECON_INTERVAL_S:
            return
        self._last_recon = time.time()
        try:
            positions = call_with_timeout(self.trading.get_all_positions)
        except Exception as e:
            logger.warning(f"reconcile: positions fetch failed ({str(e)[:60]})")
            return
        broker = {}
        for p in positions:
            if not str(getattr(p, "asset_class", "")).lower().endswith("us_option"):
                continue
            q = int(float(p.qty))
            if q:
                broker[p.symbol] = q
        expected = expected_book_from_rows(self.journal.read_rows(),
                                           now.strftime("%y%m%d"), self.root)
        diffs = [f"{s}: broker={broker.get(s, 0)} book={expected.get(s, 0)}"
                 for s in sorted(set(broker) | set(expected))
                 if broker.get(s, 0) != expected.get(s, 0)]
        sig = ";".join(diffs)
        if diffs:
            if sig != self._last_recon_sig:
                logger.warning(f"RECONCILE MISMATCH ({len(diffs)} legs): {sig[:300]}")
                self.journal.row(slot="", action="reconcile",
                                 note=f"MISMATCH {len(diffs)} legs: {sig[:180]}")
        elif self._last_recon_sig:
            logger.info("reconcile: back in sync")
            self.journal.row(slot="", action="reconcile", note="back in sync")
        elif broker:
            logger.info(f"reconcile OK: {len(broker)} legs match book")
        self._last_recon_sig = sig

    # ----------------------------------------------------- settlement ----
    def eod_settlement(self) -> None:
        """Hold-to-settlement bookkeeping. SPX options are European and cash
        settle — there is nothing to close at the bell. At ~16:01 ET the desk
        writes a settlement MARK (best available index estimate); the exact
        cash settlement posts to the account overnight and the ops layer
        reconciles it next morning (agent/ops_mcp.md). TODO(private): the
        production mark uses an official index-close feed."""
        try:
            S_close = self.spot_hint()
            self.journal.row(slot="EOD", action="settlement_mark",
                             S=round(S_close, 2),
                             note="proxy-close estimate; official cash "
                                  "settlement posts overnight — ops layer "
                                  "reconciles next morning")
            self.journal.note("HARVESTER",
                              f"settlement mark {S_close:.2f} "
                              f"({len(self.open_flies)} structures held to settle)",
                              "", "PASS")
        except Exception as e:
            logger.warning(f"EOD mark failed: {e}")


def f_note(cfg: dict) -> str:
    v = cfgval(cfg, "containment.cut_fraction")
    return "private" if v is None else str(v)


# ----------------------------------------------------------- explorer ----
class Explorer:
    """Capped directional sleeve. Zero discretion lives here: the entry rule
    (probability consensus over a registered threshold) belongs to the
    SignalProvider; this class only enforces caps and executes one bounded
    round trip at a time — buy a defined-risk call vertical, hold for a fixed
    window, close. Unfilled entries are canceled, never chased."""

    def __init__(self, engine: Engine):
        self.e = engine
        cfg = engine.cfg
        self.enabled = bool(cfgval(cfg, "explorer.enabled", False))
        self.max_trades = int(req(cfg, "explorer.max_trades_per_day"))
        self.cooldown_s = float(req(cfg, "explorer.cooldown_s"))
        self.hold_s = float(req(cfg, "explorer.hold_s"))
        self.width = float(req(cfg, "explorer.vertical_width_pts"))
        self.win_start = parse_et(req(cfg, "explorer.window_start_et"))
        self.win_end = parse_et(req(cfg, "explorer.window_end_et"))
        self.fired = 0
        self.last_fire = 0.0
        self.open_trade = None    # (order_id, syms, entry_ts, debit)

    def tick(self) -> None:
        now = datetime.now(ET)
        if self.open_trade and time.time() - self.open_trade[2] >= self.hold_s:
            self.close_trade()
        if not self.enabled or self.e.mode != "trade":
            return
        if now.weekday() >= 5 or not (self.win_start <= (now.hour, now.minute)
                                      <= self.win_end):
            return
        if (self.fired >= self.max_trades or self.open_trade
                or time.time() - self.last_fire < self.cooldown_s):
            return
        reading = None
        try:
            reading = self.e.signals.read()
        except Exception as e:
            self.e.journal.note("EXPLORER", f"signal read failed: {str(e)[:60]}",
                                "", "KINK")
        if reading is None or not reading.fires:
            return   # silence, staleness and sub-threshold all mean NO TRADE
        try:
            self.fire(reading)
        except Exception as e:
            self.e.journal.note("EXPLORER", f"fire failed: {str(e)[:70]}",
                                "", "KINK")

    def fire(self, reading) -> None:
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce
        e = self.e
        S = e.spot_hint()
        strikes = e.chain_strikes(datetime.now(ET).date(), S)
        ks = sorted(strikes)
        K = min(ks, key=lambda x: abs(x - S))
        Ku = min(ks, key=lambda x: abs(x - (K + self.width)))
        if Ku <= K or "call" not in strikes.get(K, {}) \
                or "call" not in strikes.get(Ku, {}):
            e.journal.note("EXPLORER", f"no strikes for vertical {K}/{Ku}",
                           "", "KINK")
            return
        syms = {"b": strikes[K]["call"], "s": strikes[Ku]["call"]}
        q = e.quotes(list(syms.values()))
        mb = (q[syms["b"]][0] + q[syms["b"]][1]) / 2
        ms = (q[syms["s"]][0] + q[syms["s"]][1]) / 2
        debit = round(mb - ms + 0.05, 2)
        e.journal.note("EXPLORER",
                       f"FIRE: prob_mean={reading.prob_mean:.3f} >= "
                       f"threshold={reading.threshold:.3f} -> BUY 1x "
                       f"{K:g}/{Ku:g} call vertical @ <= {debit:.2f}",
                       "registered deploy rule, fixed round trip", "OPEN")
        legs = [OptionLegRequest(symbol=syms["b"], side=OrderSide.BUY, ratio_qty=1),
                OptionLegRequest(symbol=syms["s"], side=OrderSide.SELL, ratio_qty=1)]
        req = LimitOrderRequest(qty=1, order_class=OrderClass.MLEG,
                                time_in_force=TimeInForce.DAY,
                                limit_price=abs(debit), legs=legs)
        o = call_with_timeout(e.trading.submit_order, req, timeout=30)
        time.sleep(20)
        oo = call_with_timeout(e.trading.get_order_by_id, str(o.id))
        st = str(oo.status.value if hasattr(oo.status, "value") else oo.status)
        if st == "filled":
            self.open_trade = (str(o.id), syms, time.time(),
                               float(oo.filled_avg_price or debit))
            self.fired += 1
            self.last_fire = time.time()
            e.journal.note("EXPLORER", f"FILLED vertical @ {oo.filled_avg_price}",
                           "", "PASS")
        else:
            try:
                e.trading.cancel_order_by_id(str(o.id))
            except Exception:
                pass
            e.journal.note("EXPLORER",
                           f"vertical unfilled ({st}) -> canceled, no chase",
                           "", "NULL")

    def close_trade(self) -> None:
        from alpaca.trading.requests import (LimitOrderRequest,
                                             MarketOrderRequest,
                                             OptionLegRequest)
        from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce
        e = self.e
        _oid, syms, _t0, debit = self.open_trade
        legs = [OptionLegRequest(symbol=syms["b"], side=OrderSide.SELL, ratio_qty=1),
                OptionLegRequest(symbol=syms["s"], side=OrderSide.BUY, ratio_qty=1)]
        try:
            q = e.quotes(list(syms.values()))
            mb = (q[syms["b"]][0] + q[syms["b"]][1]) / 2
            ms = (q[syms["s"]][0] + q[syms["s"]][1]) / 2
            credit = round(max(mb - ms - 0.05, 0.05), 2)
            req = LimitOrderRequest(qty=1, order_class=OrderClass.MLEG,
                                    time_in_force=TimeInForce.DAY,
                                    limit_price=-abs(credit), legs=legs)
            o = call_with_timeout(e.trading.submit_order, req, timeout=30)
            time.sleep(45)
            oo = call_with_timeout(e.trading.get_order_by_id, str(o.id))
            st = str(oo.status.value if hasattr(oo.status, "value") else oo.status)
            if st != "filled":   # a directional sleeve must not linger: market out
                e.trading.cancel_order_by_id(str(o.id))
                mreq = MarketOrderRequest(qty=1, order_class=OrderClass.MLEG,
                                          legs=legs)
                oo = call_with_timeout(e.trading.submit_order, mreq, timeout=30)
            exit_px = float(getattr(oo, "filled_avg_price", 0) or 0)
            pnl = round((abs(exit_px) - debit) * 100)
            e.journal.note("EXPLORER",
                           f"round trip closed: entry {debit:.2f} exit "
                           f"{abs(exit_px):.2f} pnl ~{pnl:+d}",
                           "pre-declared single RT",
                           "PASS" if pnl >= 0 else "NULL")
        except Exception as ex:
            e.journal.note("EXPLORER",
                           f"close failed: {str(ex)[:70]} — POSITION MAY BE OPEN",
                           "", "KINK")
        self.open_trade = None


# --------------------------------------------------------------- main ----
def main() -> None:
    ap = argparse.ArgumentParser(description="GridFly engine")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    engine = Engine(cfg)                       # StubSignalProvider by default
    explorer = Explorer(engine)
    engine.journal.note(
        "SYSTEM",
        f"GridFly up: mode={engine.mode}, {len(engine.slots)} slots, "
        f"hold-to-settlement, containment "
        f"{'armed' if cfgval(cfg, 'containment.cut_fraction') is not None else 'unconfigured'}, "
        f"explorer {'on' if explorer.enabled else 'off'}",
        "council decides every slot; observers narrate", "OPEN")
    done = set()
    grace = float(req(cfg, "grid.late_entry_grace_s"))
    logger.info(f"slots: {['%02d:%02d' % s for s in engine.slots]}")
    while True:
        now = datetime.now(ET)
        if now.weekday() >= 5:
            time.sleep(300)
            continue
        try:
            engine.reconcile_tick()
        except Exception as e:
            logger.warning(f"reconcile tick error: {e}")
        try:
            engine.containment_tick()
        except Exception as e:
            logger.warning(f"containment tick error: {e}")
        try:
            explorer.tick()
        except Exception as e:
            logger.warning(f"explorer tick error: {e}")
        hm = (now.hour, now.minute)
        for slot in list(engine.slots):
            key = (now.date(), slot)
            if key in done or hm < slot:
                continue
            slot_dt = datetime(now.year, now.month, now.day,
                               slot[0], slot[1], tzinfo=ET)
            if (now - slot_dt).total_seconds() > grace:
                done.add(key)   # missed (service down) -> skip, never chase
                continue
            done.add(key)
            try:
                engine.run_slot("%02d:%02d" % slot)
            except Exception as e:
                logger.error(f"slot error: {e}")
                engine.journal.row(slot="%02d:%02d" % slot, action="error",
                                   note=str(e)[:120])
        if hm >= (16, 1) and (now.date(), "eod") not in done:
            done.add((now.date(), "eod"))
            engine.eod_settlement()
        time.sleep(10)


if __name__ == "__main__":
    main()
