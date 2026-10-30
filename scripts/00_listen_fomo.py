"""Every FOMO trade on every chain it trades on, in one feed. Start here.

FOMO is cross-chain: Solana holds the cash and is where a buy is paid for, and each EVM
chain holds the tokens issued there and is where a buy is finally named. This runs one
listener per chain, side by side, and prints them as a single stream. The EVM chains are
the rows of shared/evm.py's `CHAINS`.

Every kind reaches this feed — BUY, SELL and PAY off each EVM chain, SWAP, PAY, IN, OUT and
OTHER off Solana. shared/feed.py defines what each one means.

Every chain runs in one process, so the halves of a cross-chain buy can be put back
together: the Solana payment carries the Relay order id, and the solver's fill on the EVM
chain ends its calldata with the same id. A filled buy prints with the payment that bought
it and how long it took, off the chains themselves, with no Relay API. The EVM listeners
are told which order ids FOMO's co-signer paid for, so a token delivered to a wallet that
has never operated on that chain, and so carries no delegation there yet, is still named.

A sell joins the same way, from the other direction. Its payout on Solana is signed
by a Relay solver rather than by FOMO's co-signer, so it arrives over a third
subscription keyed on that solver, carrying the order id in a memo. That stream is
every application Relay settles on Solana, not FOMO's alone, and nothing in a payout
says which application it belongs to — a payout is named here only when its order id
matches a sell this run sees on an EVM chain, before or after it. The rest are counted.

One transport per subject, named to override the default. The defaults are the three that
run on an ordinary endpoint; a default whose endpoint is missing from .env exits saying
which variable it wants, rather than listening over something else or dropping the subject.
Which transport is faster or more complete is a property of your endpoint, so measure
yours with maintenance/compare_listeners.py.

One subject per EVM chain, plus FOMO on Solana and Relay's payouts, two pipes each,
either/or. Relay's payouts are a subject rather than a third choice of Solana pipe: they
key on the solver rather than on FOMO. Every EVM chain is on by default, so a chain whose
endpoint is missing exits naming the variable rather than leaving a hole in the feed; turn
one off by name. Each takes the same three, shown here for Robinhood Chain:

  rh-logs         six eth_subscribe log filters; nothing polled or fetched  [default]
  rh-blocks       newHeads plus per-block eth_getLogs; groups a block together
  rh-off          leave Robinhood Chain out

  sol-grpc        Yellowstone gRPC, whole transactions pushed; needs Geyser
  sol-blocks      blockSubscribe over a standard WSS; no paid add-on        [default]
  sol-off         leave Solana out

  payouts-blocks  Relay's payout solver over blockSubscribe                 [default]
  payouts-grpc    the same solver over Yellowstone gRPC; needs Geyser
  payouts-off     leave the payouts out; a sell then ends at the deposit into Relay

FOMO is one tenant of Relay's contracts on every EVM chain, so rows from there are the
wallets carrying FOMO's delegation, plus the deliveries whose order FOMO paid for on
Solana. Solana has no such marker, and everything reached through FOMO's co-signer is
FOMO's. Wallet arguments filter by user: a 0x address filters every EVM chain, because a
FOMO wallet has the same address on all of them, and a base58 one filters Solana. A chain
given no wallet keeps streaming every FOMO trade. One user is two addresses, so watching a
person means passing both.

This decodes nothing itself. It imports `listen()` from 01 through 06, so a
decoding rule lives in exactly one place, and it is the only script here that imports
another. Rows are laid out for the eye; every field in full goes to
`scripts/runs/00_listen_fomo-<time>.jsonl`, one JSON object per trade.

Reference: docs/04-listening.md, docs/02-trade-lifecycle.md (Four shapes).

    uv run scripts/00_listen_fomo.py [seconds] [transport ...] [wallet ...]   default 120
"""

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

from shared import feed, transports
from shared.cli import wallet_shaped
from shared.evm import CHAINS
from shared.transports import TRANSPORTS

sys.path.insert(0, str(Path(__file__).parent))

JOIN_WINDOW = 600  # seconds a payment waits for its fill before it is forgotten
OFF = (*(f"{network}-off" for network in CHAINS), "sol-off", "payouts-off")
SLOTS = (*(f"{network}-" for network in CHAINS), "sol-", "payouts")


def choose(argv):
    """(seconds, one transport per slot, wallets).

    The slots are every EVM chain, then Solana, then the payouts, each holding a
    transport name or its `-off`. The defaults are fixed rather than chosen from what
    `.env` happens to hold, and they are the transports an ordinary endpoint serves. A
    transport whose endpoint is missing is an error worth reading, not a reason to
    quietly listen over something else or to drop a subject: pass the transport you
    want, or `<slot>-off` to mean it.
    """
    seconds, chosen, wallets = 120, [], []
    rest = list(argv)
    # Only the first argument can be the duration. Base58 runs on digits alone, so a
    # Solana address of all digits would otherwise be read as seconds and the wallet
    # lost, leaving the chain unfiltered.
    if rest and rest[0].isdigit():
        seconds = int(rest.pop(0))
    for arg in rest:
        if arg in TRANSPORTS or arg in OFF:
            chosen.append(arg)
        elif arg.startswith((*SLOTS, "fills")):
            # A near miss is a typo, not a wallet. Silently watching a wallet called
            # "sol-grpcx" would look like a chain that had gone quiet. Bare "payouts" and
            # "fills" land here too: both named the payout feed when it had one transport,
            # and both now have to say which of the two they mean.
            sys.exit(f"unknown transport {arg!r} — one of {', '.join([*TRANSPORTS, *OFF])}")
        elif arg.isdigit() and not wallet_shaped(arg):
            sys.exit(f"seconds go first: {arg!r} came after {argv[0]!r}")
        elif not wallet_shaped(arg):
            # A placeholder pasted out of the README filters a chain down to a wallet
            # that cannot exist, which looks exactly like a quiet chain.
            sys.exit(f"{arg!r} is not an address: 0x and 40 hex digits, or 32 to 44 base58 characters")
        else:
            wallets.append(arg)

    defaults = {**{f"{network}-": f"{network}-logs" for network in CHAINS}, "sol-": "sol-blocks"}
    defaults["payouts"] = "payouts-blocks"
    slots = [next((c for c in chosen if c.startswith(slot)), default) for slot, default in defaults.items()]
    return seconds, slots, wallets


async def main():
    seconds, slots, wallets = choose(sys.argv[1:])
    *evm, solana, payouts = slots
    for name in slots:
        if name in TRANSPORTS and (why := transports.unconfigured(name)):
            sys.exit(f"{name} {why}")
    if all(name.endswith("-off") for name in [*evm, solana]):
        sys.exit("nothing to listen to: every chain is off")

    # A FOMO wallet has one address on every EVM chain, so a 0x address filters all of
    # them; base58 is Solana's alone.
    evm_wallets = {w.lower() for w in wallets if w.startswith("0x")}
    watched = {chain.code: evm_wallets for chain in CHAINS.values()}
    watched["SOL"] = {w for w in wallets if not w.startswith("0x")}
    picked = [name for name in slots if name in TRANSPORTS]
    tally = {name: Counter() for name in picked}

    for name in picked:
        print(f"{name:<15} {TRANSPORTS[name].summary}")
    for network, name in zip(CHAINS, evm, strict=True):
        if name.endswith("-off"):
            print(f"{name:<15} {CHAINS[network].name} is off, so no buy delivered there will be named")
    if solana == "sol-off":
        print(f"{'sol-off':<15} Solana is off, so payments and Solana-native swaps are invisible")
    if payouts == "payouts-off":
        print(f"{'payouts-off':<15} payouts are off, so a sell ends at the deposit into Relay")
    # A wallet filters the chains its address belongs to and no other. A user's EVM
    # address and their Solana address are unrelated, so passing one leaves the other
    # side streaming every FOMO trade — which reads as a filter that did nothing. Print
    # the state of each chain rather than a total.
    print(f"\nlistening {seconds} s, FOMO only")
    for name in sorted({TRANSPORTS[n].chain for n in picked}):
        held = len(watched[name])
        print(f"  {name:<5}{f'filtering {held} wallet(s)' if held else 'every FOMO wallet'}")
    print()
    show, done = feed.start("00_listen_fomo", *(c.code for c in CHAINS.values()), "SOL")

    # A Solana payment names the order it pays for; the fill on the EVM chain ends its
    # calldata with the same id. Holding payments briefly lets the two halves be printed
    # as one trade. Anything older than the window is a payment whose fill will not
    # arrive while this is running.
    # Both directions join on the order id, and in both the half that arrives second
    # is the one worth printing whole. A buy is paid on Solana and named on an EVM
    # chain; a sell is made on an EVM chain and paid out on Solana.
    paid, sold, payouts = {}, {}, {}
    joins = Counter()

    def remember(held, trade):
        held[trade.order_id] = (trade, time.time())
        for key in [k for k, (_, at) in held.items() if time.time() - at > JOIN_WINDOW]:
            del held[key]
            if held is payouts:
                joins["Relay payouts for other applications"] += 1

    def settles(sell, gap):
        joins["sells settled on Solana"] += 1
        return f"settles {feed.amount(sell.gave or sell.got)} sold by {feed.short(sell.who)}, seen {gap}"

    def run(name):
        chain = TRANSPORTS[name].chain

        def emit(trade):
            tail = ""
            if trade.order_id and chain == "SOL" and trade.kind == "PAY":
                remember(paid, trade)
            elif trade.order_id and chain != "SOL" and trade.kind == "SELL":
                if trade.order_id not in payouts:
                    remember(sold, trade)
                else:
                    # The payout lands about a second after the sell, and a stream at
                    # `processed` hears it before the sell's block has been decoded. The
                    # payout was held for this sell, so it prints now, under it.
                    payout, at = payouts.pop(trade.order_id)
                    show(trade, chain, "")
                    show(payout, "SOL", settles(trade, f"{time.time() - at:.1f}s later"))
                    return
            elif trade.kind == "FILL":
                # This stream carries every application Relay settles on Solana, and a
                # payout says nothing about which. Only one whose order id matches a sell
                # seen here can be called FOMO's. One that arrives first is held for its
                # sell, and whatever is still unmatched when it expires is counted.
                if trade.order_id not in sold:
                    remember(payouts, trade)
                    return
                sell, at = sold.pop(trade.order_id)
                tail = settles(sell, f"{time.time() - at:.1f}s earlier")
            elif trade.order_id and chain != "SOL" and trade.order_id in paid:
                pay, at = paid.pop(trade.order_id)
                joins["cross-chain trades seen whole"] += 1
                # The join is the row worth reading, so it gets a line of its own.
                # Seen, not settled: the gap is between this process noticing each
                # half, so it carries whatever the two transports differ by.
                paid_with = feed.amount(pay.gave or pay.got)
                tail = f"paid {paid_with} by {feed.short(pay.who)}, seen {time.time() - at:.1f}s earlier"
            show(trade, chain, tail)

        # An EVM listener is told which orders FOMO's co-signer paid for, so a delivery to a
        # wallet with no delegation on that chain yet is still named rather than dropped.
        extra = {"claimed": paid.__contains__} if TRANSPORTS[name].network else {}
        listen = transports.listener(name, **extra)
        return listen(time.time() + seconds, watched[chain], emit, tally[name])

    outcomes = await asyncio.gather(*(run(name) for name in picked), return_exceptions=True)
    for name, outcome in zip(picked, outcomes, strict=True):
        if isinstance(outcome, Exception):
            print(f"[{name} stopped: {type(outcome).__name__}: {outcome}]")

    done()
    print(f"{seconds} s")
    # A sell still held at the end was never settled while this was running. A run
    # where every sell is unsettled is the solver address having rotated, not a chain
    # that went quiet.
    if sold:
        joins["sells with no payout seen"] += len(sold)
    if payouts:
        joins["Relay payouts for other applications"] += len(payouts)
    merged = Counter(joins)
    # Keyed by transport, not by chain: `sol-blocks` and `payouts-blocks` both count
    # blocks, over different account filters, and one line adding the two together says
    # nothing about either. Two transports on one chain are two subscriptions, and read
    # as two.
    for name in picked:
        for label, n in tally[name].items():
            merged[f"{name} {label}"] += n
    for label, n in merged.most_common():
        print(f"  {n:>5}  {label}")


if __name__ == "__main__":
    asyncio.run(main())
