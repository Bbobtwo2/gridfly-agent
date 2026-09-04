"""gate_council.py — the GridFly Gate Council.

Every slot's entry decision routes through a council of weighted
votes. Weights are EARNED: a vote carries decision power only after surviving
pre-registered studies. Two years of registered testing put realized
volatility in the decisive seat and the economic calendar in a veto seat;
sentiment and credit quality sit as OBSERVER votes — they narrate, accrue a
live track record, and cannot alter the desk's behavior until they earn
weight.

Council seats (current configuration):
  RV        decisive   trailing realized vol vs a frozen ceiling (the
                       ceiling is a calibrated private value; the vote's
                       mechanics are fully public). Survived two years of
                       attempts to kill it.
  CALENDAR  veto       scheduled macro-release days: an LLM reads the
                       upcoming print and classifies expectation risk.
                       HOT/COLD expected -> veto (sit out). IN-LINE ->
                       the operator's standing choice (release_day_override).
                       Unreadable / no key -> fail CLOSED to the legacy
                       tier rule (skip scoped release days), never fail open.
  SENTIMENT observer   recent market headlines (Alpaca news API) scored
                       -1..+1 by an open-weights model. Refuted as a
                       day-gate five times in registered studies — hence
                       observer: it narrates and builds a live record only.
  CREDIT    observer   is the market paying for the risk (credit/width vs
                       floor). Structural guard, narrates premium richness.

Behavior guarantees:
  - Decision = decisive(RV) AND veto(CALENDAR). Nothing else can block or
    open the desk.
  - Observer votes NEVER change the decision; a broken observer ABSTAINS.
  - A broken veto fails CLOSED (falls back to the conservative legacy rule).

All endpoints, models and keys are injected via CouncilConfig — this module
touches no databases and holds no credentials of its own.

The vote bodies in this public skeleton are redacted; the council's shape
(who decides, who vetoes, who only narrates) and its fail-closed behavior
are the public part.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

REDACTED = "this is redacted"
PRIVATE = REDACTED + ": proprietary gate logic is not published"


# ------------------------------------------------------------- config ----
@dataclass
class CouncilConfig:
    """Injected plumbing: LLM endpoint, news feed, credentials.

    llm_base_url may be any OpenAI-compatible chat-completions host. The key
    resolves from (in order): the explicit field, GRIDFLY_LLM_API_KEY, or
    FEATHERLESS_API_KEY in the environment. No key => sentiment abstains and
    the calendar veto uses its fail-closed fallback.
    """
    llm_base_url: str = "https://api.featherless.ai/v1"
    llm_model: str = "meta-llama/Meta-Llama-3.1-8B-Instruct"
    llm_api_key: Optional[str] = None
    news_symbols: str = "SPY"
    news_limit: int = 12
    alpaca_key: str = ""
    alpaca_secret: str = ""
    sentiment_extreme_abs: Optional[float] = None   # private; observer abstains without it

    @classmethod
    def from_agent_config(cls, cfg: dict) -> "CouncilConfig":
        c = cfg.get("council", {}) if isinstance(cfg, dict) else {}
        a = cfg.get("alpaca", {}) if isinstance(cfg, dict) else {}
        kw = {}
        for name in ("llm_base_url", "llm_model", "llm_api_key",
                     "news_symbols", "news_limit", "sentiment_extreme_abs"):
            if name in c and c[name] not in ("", None, "REDACTED", REDACTED):
                kw[name] = c[name]
        return cls(alpaca_key=a.get("api_key_id", ""),
                   alpaca_secret=a.get("api_secret_key", ""), **kw)

    def resolve_key(self) -> Optional[str]:
        for k in (self.llm_api_key,
                  os.environ.get("GRIDFLY_LLM_API_KEY"),
                  os.environ.get("FEATHERLESS_API_KEY")):
            k = (k or "").strip()
            if k and not k.startswith("PASTE"):
                return k
        return None

    @property
    def news_url(self) -> str:
        return (f"https://data.alpaca.markets/v1beta1/news"
                f"?symbols={self.news_symbols}&limit={self.news_limit}")


# -------------------------------------------------------------- model ----
@dataclass
class Vote:
    seat: str
    role: str          # decisive | veto | observer
    signal: str        # ENTER | SKIP | ABSTAIN
    score: Optional[float]
    rationale: str
    weight_note: str = ""


@dataclass
class CouncilDecision:
    enter: bool
    votes: list = field(default_factory=list)
    narrative: str = ""

    def journal_note(self) -> str:
        parts = [f"{v.seat}:{v.signal}" +
                 (f"({v.score:+.2f})" if v.score is not None else "")
                 for v in self.votes]
        return "council[" + " ".join(parts) + "]"


# ----------------------------------------------------------- plumbing ----
def _llm(prompt: str, key: str, cc: CouncilConfig,
         max_tokens: int = 120, timeout: int = 20) -> str:
    body = json.dumps({
        "model": cc.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(
        cc.llm_base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"].strip()


def _alpaca_news(cc: CouncilConfig, timeout: int = 10) -> list:
    req = urllib.request.Request(
        cc.news_url,
        headers={"APCA-API-KEY-ID": cc.alpaca_key,
                 "APCA-API-SECRET-KEY": cc.alpaca_secret})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        items = json.loads(r.read()).get("news", [])
    return [n.get("headline", "") for n in items if n.get("headline")]


# -------------------------------------------------------------- votes ----
def rv_vote(rv30: Optional[float], ceiling: Optional[float]) -> Vote:
    """The decisive seat: trailing realized volatility against a frozen ceiling.
    None input means fail closed. Definition and ceiling are private."""
    raise NotImplementedError(PRIVATE)


def sentiment_vote(cc: CouncilConfig) -> Vote:
    """Observer: recent headlines from the Alpaca news API scored by an open-weights
    model. Refuted as a day-gate in five registered studies, so it narrates and
    cannot change the outcome; without a key it abstains. Scoring is private."""
    raise NotImplementedError(PRIVATE)


def calendar_vote(release_today: bool, release_name: str, legacy_skip: bool,
                  override_play_inline: bool, cc: CouncilConfig) -> Vote:
    """The veto seat: on a scheduled macro-release day an LLM reads the coverage and
    classifies surprise risk. Hot or cold expected: sit out. In line: the
    operator's standing instruction. Unreadable or no key: fail closed to the
    conservative rule, never open. The classification prompt is private."""
    raise NotImplementedError(PRIVATE)


def credit_vote(credit: Optional[float], wing_avg: Optional[float],
                floor_frac: Optional[float]) -> Vote:
    """Observer: is the market paying for the risk it asks the desk to carry?
    Narrates premium richness against a private floor."""
    raise NotImplementedError(PRIVATE)


# ------------------------------------------------------------ council ----
def convene(*, rv30, rv_ceiling, credit=None, wing_avg=None, floor_frac=None,
            release_today=False, release_name="", legacy_skip=True,
            override_play_inline=False,
            council_cfg: Optional[CouncilConfig] = None) -> CouncilDecision:
    """Full council for one slot. Decision = decisive(RV) AND veto(CALENDAR).
    Observers narrate only — by construction they cannot change `enter`."""
    cc = council_cfg or CouncilConfig()
    votes = [
        rv_vote(rv30, rv_ceiling),
        calendar_vote(release_today, release_name, legacy_skip,
                      override_play_inline, cc),
        sentiment_vote(cc),
        credit_vote(credit, wing_avg, floor_frac),
    ]
    decisive_ok = votes[0].signal == "ENTER"
    veto_ok = votes[1].signal == "ENTER"
    enter = decisive_ok and veto_ok
    lead = votes[0] if not decisive_ok else (votes[1] if not veto_ok else votes[0])
    narrative = (f"council {'ENTERS' if enter else 'SKIPS'}: {lead.rationale}. "
                 + "; ".join(f"{v.seat.lower()} {v.signal.lower()}"
                             + (f" {v.score:+.2f}" if v.score is not None else "")
                             for v in votes[1:]))
    return CouncilDecision(enter=enter, votes=votes, narrative=narrative)


if __name__ == "__main__":
    print(__doc__)
