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
`maintenance/verify_registry.py` re-verified against both mainnets the same day.
