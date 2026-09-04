# The ops layer — Claude on the Alpaca MCP server

The fourth GridFly component is not code in this repository: it is a Claude
agent connected to the **Alpaca MCP server**, acting as the desk's operations
officer. The engine trades; Claude **watches, verifies, and journals**. This
separation is deliberate:

- **No order authority.** The ops agent's job is oversight. Every entry and
  cut is placed by the deterministic engine; the ops agent reads accounts,
  orders, positions, and market data through MCP tools and compares them
  against the engine's journal. An overseer that can also trade is not an
  overseer.
- **Independent data path.** The ops agent queries Alpaca directly
  (`get_account_info`, `get_all_positions`, `get_orders`,
  `get_account_activities`, `get_option_latest_quote`, `get_news`,
  `get_calendar`), so its picture of the account does not depend on any state
  the engine holds. Drift between the two is the finding.
- **Everything journaled.** Ops findings land in the same evidence trail as
  trades (`journal/journal.jsonl`), with honest verdicts — including nulls.

## Example interactions

### 1. Morning account verification

> **Operator:** Verify the desk is clean before the open.
>
> **Claude:** `get_account_info` → equity $100,412, options buying power
> $98,730, no margin calls. `get_all_positions` → no open option legs, which
> matches yesterday's journal (all structures cash-settled overnight;
> settlement activity visible via `get_account_activities`). `get_calendar`
> confirms a full session today. One flag: the journal expected a settlement
> credit of ~$1,940 and the account shows +$1,912 — the $28 gap is the
> settlement-mark estimate vs the official settlement value, noted in the
> journal, not a missing fill. Desk is clean to run.

### 2. Settings audit

> **Operator:** Audit today's behavior against the configured rules.
>
> **Claude:** Reading today's journal CSV against `get_orders` (closed +
> open). 11 slots evaluated, 7 orders, 4 council skips. Checks: every order
> was a 4-leg MLEG **limit** (no market entries), each submitted within its
> slot's grace window, and each canceled or filled inside the configured
> cancel window — no re-submits, so the no-chase rule held. Slot sizing never
> exceeded `max_lots_per_slot`, and pre-noon orders left the configured
> reserves untouched. All 4 skips carry council rationales (3 × RV decisive,
> 1 × calendar veto on the 08:30 print). One observation: one afternoon slot's
> credit cleared its floor by a whisker — within rules, worth watching, noted
> in the journal with verdict OPEN.

### 3. EOD reconciliation

> **Operator:** Reconcile the day.
>
> **Claude:** `get_orders` shows 8 filled packages (7 entries, 1 wing cut);
> journal shows the same 8, with matching fill prices. `get_all_positions`
> still lists the 6 held structures — correct, they cash-settle overnight;
> the cut structure is gone from both books. The 14:02 cut confirms in the
> journal: trigger, the confirmation window, then a fill a few seconds later
> a tick through the package mid. Structural P&L if settled at the 16:01 mark:
> +$1,285; broker mark P&L differs (wide EOD option marks on held
> structures) and is recorded separately, per the journal's convention.
> Writing the EOD summary to `journal/journal.jsonl` with verdict PASS —
> tomorrow morning's verification will confirm the overnight settlement
> against `get_account_activities`.
