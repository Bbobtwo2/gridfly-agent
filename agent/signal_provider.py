"""SignalProvider — the Explorer's only source of directional conviction.

The Explorer sleeve (agent/engine.py) never invents a view of its own. It
fires a defined-risk call vertical only when an EXTERNAL signal source says
its pre-registered entry rule is met, and the interface below is the entire
contract between the two.

Contract
--------
`read()` returns a `SignalReading` or `None`.

  prob_mean   consensus probability that the next move is up: the MEAN of
              several independently trained models' calibrated probabilities
              (a probability-consensus ensemble). The mean of calibrated probabilities is deliberately
              boring — the edge, where there is one, lives in the ensemble
              and in WHEN the rule allows a fire, not in this transport.
  threshold   the fire threshold that prob_mean must clear. It ships WITH
              the reading because it is a property of the signal source's
              own registered deploy rule (e.g. a rolling quantile of recent
              consensus values), not something the engine may tune.
  as_of       timestamp (UTC) of the underlying model outputs.

Freshness is part of the contract: a provider MUST return `None` rather than
a stale reading (the staleness bound is private). `None` always
means "no trade" — the Explorer treats every provider failure as silence,
never as a signal.

Nothing in this repository computes prob_mean. The reference implementation
below is a stub so the engine runs (Explorer stays idle) without the private
model stack.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class SignalReading:
    prob_mean: float          # ensemble consensus P(up), 0..1
    threshold: float          # registered fire threshold for prob_mean
    as_of: datetime           # UTC timestamp of the model outputs

    @property
    def fires(self) -> bool:
        return self.prob_mean >= self.threshold


class SignalProvider(ABC):
    """Interface between the Explorer and an external signal source."""

    @abstractmethod
    def read(self) -> Optional[SignalReading]:
        """Return the freshest reading, or None if unavailable or stale."""
        raise NotImplementedError


class StubSignalProvider(SignalProvider):
    """Reference implementation: always silent.

    The production implementation feeds an external ML ensemble's
    probabilities (several independently trained direction models whose
    calibrated outputs are averaged into prob_mean, with the fire threshold
    maintained by that system's own registered refit process). That stack is
    private infrastructure and is not part of this repository; with the stub
    in place the Explorer sleeve simply never fires.

    TODO(private): swap in the production provider at deploy time.
    """

    def read(self) -> Optional[SignalReading]:
        return None
