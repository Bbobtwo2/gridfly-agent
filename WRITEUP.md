# GridFly: one-page write-up

**What it is:** an autonomous agent that sells defined-risk SPX 0DTE structures on a fixed
intraday schedule on Alpaca paper, holds them to cash settlement, and spends most of its
intelligence deciding **when not to trade** and **how to fail safely**.

## AI logic
The agent is a decision loop, not a price oracle. Every half hour during regular hours the
Gate Council convenes on one question: is today still boring enough to sell? A realized-volatility
seat decides. An economic-calendar seat holds a veto: before a scheduled release, an LLM reads
the coverage and classifies surprise risk; expected hot or cold sits the day out, expected
in-line defers to the operator's standing instruction, unreadable fails closed. Observers (news
sentiment scored by an open-weights model on Featherless, credit quality, and a Kalshi
prediction-market read) argue and are journaled, but cannot change the outcome. Weight is earned
only by surviving a pre-registered study. Claude also writes the journal entry that makes every
action explainable, and a Claude ops agent on the Alpaca MCP server verifies the account, audits
the day against the rules and reconciles settlement through an independent data path, with no
order authority. Structured output only; calibrated values are proprietary.

## Risk gates
Entry gates run in a fixed order; the first refusal wins and is journaled. Exposure is sized from
live buying power with reserves released through the day. Entries never chase: one package limit,
one reprice, cancelled on timeout; a missed fill is a missed slot. Open structures are watched
every poll by a containment engine: a trigger is re-checked after a confirmation window, a
confirmed break is closed with a package order first and per-leg fallback with fresh quotes,
under a hard retry cap. Structures are held to cash settlement; there is no forced close at the
bell. A read-only reconcile tick diffs the broker's book against the journal. The engine refuses
trade mode on a non-paper key and refuses to start while any calibrated value is a placeholder.

## Alpaca infrastructure
Alpaca Trading API on the paper environment: multi-leg option limit orders for the structures,
two-leg verticals for the explorer sleeve, order polling, cancels, positions and account state
for sizing and reconciliation. Alpaca Market Data: 0DTE option chains and quotes (strike-bounded
requests, parity-based level), stock bars for the volatility seat, and the news endpoint for the
sentiment observer. Alpaca MCP server: the ops agent's entire interface (account, positions,
orders, activities, quotes, calendar). Judged account: PA3OOCQ2E1CU.

## Results
Fresh $100,000 paper account, Mon Aug 31 to Thu Sep 3, settled cash as posted by the broker:
Monday −$1,539, Tuesday −$14,414, Wednesday +$33,070, Thursday +$35,322. **$152,415 settled,
+52% on the week.** Day-by-day strips generated from the journal, and a live camp map, are at
mostlyharmlessmarkets.com; the five-minute film is at youtu.be/tPwzSvq4oTY; the week's
write-up post: linkedin.com/posts/boblongccm_84k-wednesday-morning-152415-settled-activity-7501512245932126208-L3Qp
