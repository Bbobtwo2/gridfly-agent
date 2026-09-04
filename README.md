# GridFly: an AI council that sells the day

*Team MostlyHarmless, for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon), Aug 28 to Sep 4, 2026.*
Site: [mostlyharmlessmarkets.com](https://mostlyharmlessmarkets.com) (the saga as a comic, with day strips built from the journal and a live camp map) ·
The film: [youtu.be/tPwzSvq4oTY](https://youtu.be/tPwzSvq4oTY) ·
**One-page write-up (AI logic · risk gates · Alpaca infrastructure): [WRITEUP.md](WRITEUP.md)** ·
The post: [the week on LinkedIn](https://www.linkedin.com/posts/boblongccm_84k-wednesday-morning-152415-settled-activity-7501512245932126208-L3Qp)

GridFly is a structured options-income strategy wrapped in AI agent risk control, built on the
Alpaca MCP server and Trading API with Claude in the decision loop. The base hypothesis is old
and simple: option buyers overpay for protection, so a disciplined seller of defined-risk SPX
structures on a fixed intraday schedule, held to cash settlement, collects that premium. It works
on boring days and gets hurt on trending ones. So the agent's real job is not picking trades: it
is deciding, every half hour, whether the day has stopped being boring.

That decision belongs to the **Gate Council**: a realized-volatility seat that decides, an
economic-calendar seat with a veto (an LLM reads release coverage and classifies surprise risk),
and observers (news sentiment scored by an open-weights model on Featherless, credit quality, and
a Kalshi prediction-market read) that are journaled but cannot touch the outcome. Every decision,
including every refusal, is journaled in plain language. Every failure path fails closed.

Claude's role is context, hypotheses and journaling. Entry gates, exposure limits and the cut
engine are deterministic code with pre-declared thresholds, and the model has zero order-entry
permissions.

## The judged week

Fresh $100,000 paper account, settled cash as posted by the broker.

| session | capital at risk | at close | settled T+1 |
|---|---|---|---|
| Mon Aug 31 | $97,745 | +$7,760 | −$1,539 |
| Tue Sep 1 | $76,620 | −$16,306 | −$14,414 |
| Wed Sep 2 | $82,399 | +$33,808 | +$33,070 |
| Thu Sep 3 | $180,000 | +$31,690 | +$35,322 |

**$152,415 settled at the final bell, +52% on the week.** Two red days the council contained,
two green days that paid the wage. The per-day strips on the site are generated from the journal,
not written by hand.

## What this repository is, and is not

This is the **public skeleton** of the engine that ran. The mechanics are here and documented:
the session loop and its order of operations, the order path through the Alpaca Trading API
(package limit first, per-leg fallback, one reprice, never chase, idempotent retries, every
rejection journaled verbatim), the read-only reconcile tick, the containment engine's
trigger-confirm-execute shape with graceful degradation, hold-to-cash-settlement bookkeeping, the
journal format the site is rendered from, the council's shape (who decides, who vetoes, who only
narrates, and how every failure path fails closed), and the ops layer on the Alpaca MCP server.
Those mechanics are safety, not edge, and they are public on purpose.

Everything that decides is proprietary and is **not in this repository**: the grid, sizing and
reserves, body and wing selection, the realized-volatility measure and its ceiling, the credit
floors, the containment trigger and its timing, the explorer's caps and window, the council's
vote bodies, and every calibrated value. Wherever the code would hold one, it says
`this is redacted`. `config.example.json` keeps the *shape* of the configuration with every
decision value redacted, and `agent/engine.py` refuses to start, in any mode, while one remains.
The competition build lives in a private tree.

## Architecture

```
 option chain + account ──► context ──► gate council (decides / veto / observers)
                                             │
                                   entry allowed? ──► structure ──► executor (Alpaca Trading API)
                                             │                          │
                              containment tick (every poll) ◄── positions / reconcile
                                             │
                                   journal (CSV + JSONL) ──► public site strips + live camp map
                                             ▲
                        Claude ops agent (Alpaca MCP server): verify · audit · reconcile · no order authority
```

| file | what it holds |
|---|---|
| `agent/engine.py` | the session loop, order path, containment shape, reconcile, settlement, journal |
| `agent/gate_council.py` | the council's shape and fail-closed behavior; vote bodies redacted |
| `agent/signal_provider.py` | the typed interface the explorer sleeve reads its signal through; the reference provider is silent |
| `agent/ops_mcp.md` | the Claude ops layer on the Alpaca MCP server, with example interactions |
| `config.example.json` | the configuration's shape, every decision value redacted |
| `WRITEUP.md` | the one-page write-up |

## Running

```
pip install -r requirements.txt
cp config.example.json config.json    # add PAPER keys; replace every "this is redacted" with your own values
python -m agent.engine
```

Without a complete private `config.json` the engine exits with the list of redacted keys. Paper
only: it refuses trade mode on a non-paper key.

## Infrastructure

Alpaca Trading API (paper, options multi-leg orders) · Alpaca MCP server (the ops layer) · Alpaca
Market Data (OPRA option chains, news) · Claude via Claude Code · Featherless for open-weights
inference · Kalshi for prediction-market odds · Python.

## License

MIT, see [LICENSE](LICENSE). An engineering journal, not investment advice. Paper trading only.
