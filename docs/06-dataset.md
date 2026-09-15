# The recorded feed

Every listener writes one JSON object per trade to `scripts/runs/<script>-<time>.jsonl`, flushed as
it is written. The terminal is a hundred columns aimed at the eye; this file is the record. Amounts
keep their symbol, wallets and mints are whole, and two rows join on `order_id`.

The `<time>` in the filename is UTC, the same clock as every row's `at`, so a row and the file it
came from can be read against each other without an offset in between.

## Fields

| Field | Type | What it is |
|---|---|---|
| `t` | float | seconds since this listener started, to a millisecond. Restarts at zero every run |
| `at` | string | wall clock, ISO 8601 with a UTC offset. Use this to join anything outside the file |
| `run` | string | the run that produced the row, matching the filename. Survives a concatenation |
| `chain` | string | `RH` or `SOL` |
| `kind` | string | `BUY` `SELL` `PAY` `SWAP` `FILL` `IN` `OUT` `OTHER`, defined in `shared/feed.py` |
| `gave` | string | what left the wallet, as `<amount> <symbol>`, or one of the sentences below |
| `got` | string | what arrived, same shape |
| `who` | string | the wallet in full: a 0x address on Robinhood Chain, base58 on Solana |
| `app` | string | `fomo`, from the wallet's delegation. Empty on `FILL`, which no chain attributes |
| `note` | string | the venues a trade ran through, joined by `+` |
| `where` | string | `blk <number>` on Robinhood Chain, `slot <number>` on Solana |
| `key` | string | `<transaction>#<log index>` on Robinhood Chain, the signature on Solana |
| `order_id` | string | Relay's 32-byte order id, empty when the trade did not cross |

## Amounts

An amount is `<decimal> <symbol>`, six decimal places, human units. The symbol is a ticker when the
chain names one and the mint itself when it does not — 44 characters of base58, whole, because six
characters of a mint name no token and two mints sharing a prefix would merge into one row.

Four values are not figures, and a parser has to expect them:

| Value | What happened |
|---|---|
| `` (empty) | the trade has no side there: a `BUY` gave nothing, an `IN` gave nothing |
| `an unnamed token` | a token moved and the chain does not name it. Every crossing payment says this on the side it bought, because the deposit carries no token |
| `nothing the wallet owns moved` | a transaction arrived, none of the wallet's balances changed |
| `? <integer>`, or `?` alone | the decimals lookup failed, so the value is **raw base units** or absent. Scaling it by a guess would print a number that looks right and is wrong |

Filter on a leading `?` before summing anything.

## Joining the two legs

`order_id` is the key. A buy is a Solana `PAY` and a Robinhood Chain `BUY`; a sell is a Robinhood
Chain `SELL` and a Solana `FILL`. Both legs carry the same id, and the id is unique: 40,571
recorded rows held no duplicate on either side of either join.

```python
import pandas as pd
rows = pd.read_json("scripts/runs/00_listen_fomo-<time>.jsonl", lines=True)
crossed = rows[rows.order_id != ""]
buys = crossed[(crossed.chain == "SOL") & (crossed.kind == "PAY")].merge(
    crossed[(crossed.chain == "RH") & (crossed.kind == "BUY")], on="order_id", suffixes=("_paid", "_got")
)
```

The two legs land within about two seconds of each other, so a window of ten is generous. A leg
whose partner fell outside the run has no match; `00_listen_fomo.py` counts those at the end as
*still in flight* and *sells with no payout seen*.

`key` is the dedup key. It is derived from the chain, not from the run, so the same trade recorded
by two listeners carries the same `key`.

## Counting volume

`maintenance/volume.py` totals what these files record, in dollars, over a window, split by the
chain each trade ran on:

```bash
uv run scripts/maintenance/volume.py 12
```

Every trade is counted once, on the leg that names its size in cash. A Solana swap names both its
sides. A buy names its cash where that cash left — the `PAY` — and names its token on the other
chain, so the amount is read from one leg and the chain from the other. A sell is readable end to
end on Robinhood Chain: the token left the wallet and the proceeds went into Relay's depository in
the same transaction. A crossing trade therefore lands on one chain and not both, and the payout
that settles a sell on Solana is that same sell arriving a second later rather than more volume —
it is reported as a settlement check under the table.

| Row | What it holds |
|---|---|
| Solana | swaps that never left it |
| Robinhood Chain | crossing trades both ways: buys delivered there, sells swapped there |
| `chain unknown` | buys whose delivery was never seen, and buys paid out of Robinhood Chain |

`chain unknown` is what the two chains read here cannot account for. A payment carries an order id
and an amount and no destination, so the destination is the delivery on the far side — and only
Robinhood Chain is watched for it. A payment with no delivery went to one of the four chains
nothing here subscribes to (the catalog also spans Base, Ethereum, BNB Smart Chain and Monad) or
its far leg fell outside the run, and cash deposited into Relay on Robinhood Chain never touches
Solana at all. Both are real FOMO volume on an unknown chain, so they are counted and named rather
than dropped. A listener on those four chains, keyed the same way, is what would shrink that row.

Over the runs recorded on 2026-09-08 and 2026-09-09, 51% of Solana payments had their delivery seen
on Robinhood Chain. Robinhood Chain's share is therefore a floor and `chain unknown`'s is a
ceiling: every trade that leaves that row joins one of the two chains above it.

One flow is outside the count altogether: a sell that ran on a chain this does not read — Base,
Ethereum, BNB Smart Chain or Monad — and paid out to Solana. Its payout lands on Solana carrying
the Relay order id, but nothing on Solana marks a payout as FOMO's. What marks a trade as FOMO's
sits on the chain the trade ran on: the EIP-7702 delegation on Robinhood Chain, the co-signer on
Solana. For a sell on Base that mark is on Base, so the order id has nothing to match against and
the payout is indistinguishable from every other application Relay settles. It is a missing row
rather than a mis-attributed one — no such sell lands on the wrong chain.

Its size is bounded rather than known. Across the payout recordings held on 2026-09-15, payouts
landing in wallets FOMO's co-signer has signed for ran at about $36,000 a minute, against $33,000 a
minute of sells counted on Robinhood Chain — a single-digit share of sell volume, and an upper
bound, since a wallet can use more than one application settling through Relay and the two rates
come from different windows. A listener on the four unread chains, keyed on FOMO's marker there,
would count those sells rather than bound them, and shrink `chain unknown` at the same time.

The settlement check under the table reads both ends of the same sells. Across the recordings held
on 2026-09-15 the payout on Solana was 0.62% smaller than the deposit on Robinhood Chain, which is
what Relay and the solver keep on a sell — measured off the two legs, not quoted.

A stablecoin is worth a dollar and nothing else is priced, which values all but a per-mille of the
rows. An ETH-denominated sell, a token-for-token swap, and an amount the chain would not decode are
counted unpriced under the table rather than valued at a guess.

A window is not coverage. A file holds only what its own run was listening to — minutes inside a
window of hours — so the report prints the time actually watched beside the window asked for, and a
window with no run in it is empty rather than zero.

## What the file is not

Only `00_listen_fomo.py` records both chains, so only its files join without a merge across files.

The `PAY` and `FILL` streams are Relay's, not FOMO's: they carry every application settling through
the same contracts. A row becomes FOMO's when its `order_id` matches a leg that `app` marks as
FOMO's — which is why `00_listen_fomo.py` counts *Relay payouts for other applications* rather than
hiding them.

Every transport here subscribes. A window nobody was listening to is gone: nothing in this
repository re-reads a block range into this shape, and a file records only what its own run saw.

## Where this comes from

Read from the chains. The field list and the sentences are what `scripts/shared/feed.py` writes;
the leg latency and the absence of duplicate ids were measured over 40,571 rows recorded by the
listeners in this repository on 2026-09-09, and the shapes are the ones
`maintenance/verify_registry.py` re-verified against both mainnets the same day. The share of
payments whose delivery was seen on Robinhood Chain was measured on 2026-09-14 over those same
recordings; what Relay keeps on a sell, and the bound on sells settled from other chains, on
2026-09-15 over the sells and the payouts recorded on both chains.
