"""FOMO's trading volume over a window, split by the chain each trade ran on.

Every trade is counted once, on the leg that names its size in cash:

  Solana           SWAP  a trade that stayed on Solana, both sides named
  Robinhood Chain  PAY   cash left Solana and the token was delivered on Robinhood Chain,
                         which the Relay order id on both legs proves
                   SELL  a token swapped on Robinhood Chain, proceeds handed to Relay
  chain unknown    PAY   cash left a chain and the delivery was never seen, so which chain
                         ran the swap is unknown — FOMO also trades on Base and BNB

A crossing trade has a leg on each chain and is counted on neither twice: a buy is counted
where its cash left, a sell where its token was swapped, and the other leg of each is only
ever read to attribute it. The payout that settles a sell on Solana is not a fourth source
of volume — it is the same sell, arriving a second later — so it is reported as a
settlement check underneath the table instead.

One flow is outside the count entirely: a sell that ran on a chain this does not read —
Base, BNB — and paid out to Solana. Its payout is on Solana and carries the order id, but
nothing on Solana says a payout is FOMO's. What marks a trade as FOMO's sits on the chain
the trade ran on: the EIP-7702 delegation on Robinhood Chain, the co-signer on Solana. For
a sell on Base that mark is on Base, so the order id has nothing here to match against and
`00` drops the payout along with every other application's. That is a missing row and not a
mis-attributed one — no such sell is counted on the wrong chain.

Its size is bounded rather than known. Across the payout recordings, payouts landing in
wallets FOMO's co-signer has signed for ran at about $36,000 a minute against $33,000 a
minute of sells counted on Robinhood Chain, so sells from other chains are a single-digit
share of sell volume. That is an upper bound measured over different windows: a wallet can
use more than one application settling through Relay. A listener on Base and BNB keyed on
FOMO's delegation there is what would count them rather than bound them, and it would
shrink `chain unknown` from the other side at the same time.

Volume is in dollars, and a dollar figure needs a price. The only price used here is that a
stablecoin is worth a dollar, which covers all but a per-mille of these rows because FOMO
prices everything in USDC and USDG. Anything else — an ETH-denominated sell, an amount the
chain would not decode — is counted unpriced rather than valued at a guess, and this script
makes no request to anything.

Reads `scripts/runs/00_listen_fomo-*.jsonl` and nothing else: only that listener records
both chains, so only its files hold both legs of a trade. Recordings older than the `at`
field are skipped, because a row with no wall clock cannot be put in a window.

    uv run scripts/maintenance/volume.py [hours]      default 12
"""

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

RUNS = Path(__file__).parent.parent / "runs"

# A stablecoin is worth a dollar. Every other asset is counted and reported unpriced.
STABLES = {"USDC", "USDG", "USDT"}
UNPRICED = "amounts in no stablecoin — an ETH-denominated trade, or a `?` the chain would not decode"

# The order the table reads in. The unknown row is last: it is the residue of the two above.
CHAINS = ("Solana", "Robinhood Chain", "chain unknown")


def dollars(amount):
    """The dollar value of an `<amount> <symbol>` field, or None when it is not a stablecoin.

    `shared/feed.py` writes four things that are not figures — an empty string, `an unnamed
    token`, `nothing the wallet owns moved`, and a `?` for an amount the chain would not
    decode. None of them parses as a number, so all four fall out here as None.
    """
    parts = (amount or "").rsplit(" ", 1)
    if len(parts) != 2 or parts[1] not in STABLES:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def recorded(hours):
    """Every row of every `00` run inside the window, each trade once.

    Two runs can overlap, and a trade seen by both is one trade. `key` identifies a trade on
    its chain rather than in a run — a signature on Solana, a transaction and log index on
    Robinhood Chain — so `(chain, key)` is what a duplicate shares.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows, seen, files = [], set(), 0

    for path in sorted(RUNS.glob("00_listen_fomo-*.jsonl")):
        used = False
        for line in path.open():
            try:
                row = json.loads(line)
            except Exception:
                continue  # a run killed mid-write leaves one truncated line
            if not row.get("at"):
                continue  # recorded before the wall clock field existed
            if datetime.fromisoformat(row["at"]) < since:
                continue
            fingerprint = (row.get("chain"), row.get("key"))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            rows.append(row)
            used = True
        files += used

    return rows, files


def covered(rows):
    """Seconds of the window a run was actually listening to.

    A window is not coverage. These files hold what somebody was listening to, which is
    minutes inside a window of hours, and a total read as if it covered the whole window is
    wrong by whatever the gap is. Runs are summed separately so the quiet between two of
    them is not counted as watched.
    """
    spans = {}
    for row in rows:
        at = datetime.fromisoformat(row["at"])
        first, last = spans.get(row.get("run"), (at, at))
        spans[row.get("run")] = (min(first, at), max(last, at))
    return sum((last - first).total_seconds() for first, last in spans.values())


def count(rows):
    """(volume per chain and line, what could not be priced, sells seen settling)."""

    def of_kind(chain, kind):
        return [row for row in rows if row.get("chain") == chain and row.get("kind") == kind]

    def order_ids(kept):
        return {row["order_id"] for row in kept if row.get("order_id")}

    delivered = order_ids(of_kind("RH", "BUY"))  # buys FOMO's wallets received on Robinhood Chain
    sold = order_ids(of_kind("RH", "SELL"))  # sells FOMO's wallets made there
    paid_out = order_ids(of_kind("SOL", "FILL"))  # sells Relay was seen settling on Solana

    volume, missing = {}, Counter()

    def add(chain, line, value):
        row = volume.setdefault((chain, line), {"trades": 0, "usd": 0.0})
        row["trades"] += 1
        row["usd"] += value

    for row in of_kind("SOL", "SWAP"):
        value = dollars(row.get("gave")) or dollars(row.get("got"))
        if value is None:
            missing["swaps with no stablecoin side, so nothing here can price them"] += 1
            continue
        add("Solana", "trades that started and finished on Solana", value)

    # A payment names its size and never its token. The buy on the other chain names the
    # token and never a price, so the amount is read here and the chain is read there.
    for row in of_kind("SOL", "PAY"):
        value = dollars(row.get("gave"))
        if value is None:
            missing[UNPRICED] += 1
        elif row.get("order_id") in delivered:
            add("Robinhood Chain", "buys — paid for on Solana, token delivered here", value)
        else:
            add("chain unknown", "buys whose delivery was never seen: Base, BNB, or missed by the run", value)

    # A sell is readable end to end on Robinhood Chain: the token left the wallet and the
    # proceeds went into Relay's depository in the same transaction. That is the sell's
    # size, a second before Relay takes its cut and pays the rest out on Solana.
    for row in of_kind("RH", "SELL"):
        value = dollars(row.get("got"))
        if value is None:
            missing[UNPRICED] += 1
            continue
        add("Robinhood Chain", "sells — token swapped here, cash paid back to Solana", value)

    # Cash deposited into Relay on Robinhood Chain never touches Solana, and nothing here
    # watches where it lands, so it is a buy of unknown destination rather than an RH trade.
    for row in of_kind("RH", "PAY"):
        value = dollars(row.get("gave"))
        if value is None:
            missing[UNPRICED] += 1
            continue
        add("chain unknown", "buys paid for on Robinhood Chain, delivered somewhere unwatched", value)

    # What Relay and the solver keep, read off the two ends of the same sells rather than
    # quoted from anywhere: what went into the depository against what came out on Solana.
    deposited = {row["order_id"]: dollars(row.get("got")) for row in of_kind("RH", "SELL")}
    received = {row["order_id"]: dollars(row.get("got")) for row in of_kind("SOL", "FILL")}
    into, out = 0.0, 0.0
    for order in sold & paid_out:
        before, after = deposited.get(order), received.get(order)
        if before and after:
            into, out = into + before, out + after
    kept = (into - out) / into if into else None

    return volume, missing, len(sold & paid_out), kept


def by_chain(volume, chain):
    """The lines belonging to one chain, keyed by their label."""
    return {line: row for (where, line), row in volume.items() if where == chain}


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    rows, files = recorded(hours)
    opened = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"window   last {hours:g} h, from {opened} to now")
    print(f"source   {files} recorded run(s) of 00_listen_fomo, {len(rows)} rows")

    if not rows:
        print("\nNothing recorded in that window. Run `uv run scripts/00_listen_fomo.py 300` and try again.")
        return
    print(f"covered  {covered(rows) / 60:.1f} min of it was recorded — these totals are of that, not of the window")

    volume, missing, settled, kept = count(rows)
    total = sum(row["usd"] for row in volume.values())
    trades = sum(row["trades"] for row in volume.values())

    print(f"\n{'FOMO VOLUME BY CHAIN':<40}{'trades':>10}{'volume':>16}{'share':>8}")
    for chain in CHAINS:
        lines = by_chain(volume, chain)
        if not lines:
            continue
        here = sum(row["usd"] for row in lines.values())
        share = here / total * 100 if total else 0.0
        trades_here = sum(row["trades"] for row in lines.values())
        print(f"  {chain:<38}{trades_here:>10}{'$' + format(here, ',.0f'):>16}{share:6.0f}%")
    print(f"  {'TOTAL':<38}{trades:>10}{'$' + format(total, ',.0f'):>16}")

    # The same numbers again, in words, for a reader who has not seen the listeners. The
    # chain is named once and its lines hang under it.
    print("\nWhat each row is made of")
    for chain in CHAINS:
        lines = by_chain(volume, chain)
        name = chain
        for line, row in sorted(lines.items(), key=lambda item: -item[1]["usd"]):
            print(f"  {name:<18}{row['trades']:>6}  {line}")
            name = ""

    print("\nA trade that crosses chains is counted once, on the chain its swap ran on, so no")
    print("dollar above is counted twice. `chain unknown` is real FOMO volume that could not be")
    print("placed — most of it is Robinhood Chain, so that row is a ceiling and Robinhood a floor.")
    print("\nOne flow is missing altogether: a sell that ran on Base or BNB and paid out to Solana.")
    print("Nothing on Solana marks a payout as FOMO's, so it cannot be told from any other")
    print("application's. It is a single-digit share of sell volume — see the doc below.")

    sells = volume.get(("Robinhood Chain", "sells — token swapped here, cash paid back to Solana"), {"trades": 0})[
        "trades"
    ]
    cut = f", for {kept * 100:.2f}% less than they deposited" if kept else ""
    print(f"\n{settled} of {sells} sells were also seen being paid out on Solana{cut}.")
    if missing:
        print("\nNot priced, and so not in the totals above:")
        for label, n in missing.most_common():
            print(f"  {n:>6}  {label}")
    print("\nHow each number is read off the chains: docs/06-dataset.md (Counting volume).")


if __name__ == "__main__":
    main()
