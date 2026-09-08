"""Run every transport at once and report what each one caught, missed, and by how long.

The listeners in this repository read the same flow through different pipes, and the pipes
do not behave alike: one is a paid add-on, one has been seen carrying a small fraction of
the flow on a shared endpoint, and how quickly each delivers moves from day to day. Which
one to use is a question about your endpoints rather than about this code, so nothing here
quotes a number. Run it and read your own.

Every transport runs on one event loop and decodes through the same `listen()` the
standalone scripts use, so a difference reported here is a difference in the pipe and never
in the decoding. Four of them together cost about a fifth of one core and leave a coroutine
waiting on a 50 ms timer roughly a millisecond late, two orders of magnitude below the lags
being measured; a process apiece moves none of the numbers and is not worth the machinery.

Trades are keyed by transaction hash and log index together on Robinhood Chain, and by
signature on Solana, so one trade seen through three transports is one row. A hash alone
would not do here: one handleOps bundle carries several users, and keying by it merges two
people's trades into one. Transports are compared only against others on their own chain,
over the block or slot range all of them covered, so one that took ten seconds to deliver
its first block is charged for what it missed while running, not for what happened before
it woke up.

Reported per transport: trades seen in the common window, how often it was first to see
one, its median and 90th-percentile lag behind whichever transport saw a trade soonest, and
how many trades another transport on the same chain caught and it did not. Disagreements
about the same trade are reported separately, because a transport that is merely late still
got the trade right. A transport reporting OTHER is not disagreeing — nothing the wallet
owns changed, so there is no amount to read and no side to take — so those count as seen
and stay out of the disagreement tally.

Endpoints that are not configured are skipped with a note rather than failing.

Reference: docs/04-listening.md (Transports).

    uv run scripts/maintenance/compare_listeners.py [seconds] [transport ...]     default 60, all available
"""

import asyncio
import importlib
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# This runs from scripts/maintenance/ but reads what scripts/ holds: shared/ and the
# listener modules imported by name below, so the path goes on before shared/ is imported.
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import transports
from shared.transports import CHAIN_NAME, COMPARABLE, TRANSPORTS

# Blocks are 0.1 s here and slots 0.4 s there, so both margins drop about three
# seconds from each end of the common window rather than trusting its edges.
MARGIN = {"Robinhood Chain": 30, "Solana": 8}


def chain_of(name):
    """The chain a transport reads, named as a sentence names it."""
    return CHAIN_NAME[TRANSPORTS[name].chain]


def height(where):
    """The block or slot a row names, or None when the transport does not carry one."""
    parts = where.split()
    if len(parts) == 2 and parts[0] in ("blk", "slot") and parts[1].isdigit():
        return int(parts[1])
    return None


def report(chain, names, seen, heights):
    """One chain's table. `seen` is {transport: {key: (kind, where, time)}}."""
    covered = {n: sorted(h for h in (heights.get(k) for k in seen[n]) if h) for n in names}
    if any(not covered[n] for n in names):
        empty = [n for n in names if not covered[n]]
        print(f"\n{chain}: no comparable rows from {', '.join(empty)} — nothing to compare\n")
        return
    margin = MARGIN[chain]
    low = max(c[0] for c in covered.values()) + margin
    high = min(c[-1] for c in covered.values()) - margin
    if high <= low:
        print(f"\n{chain}: the transports never overlapped for long enough to compare\n")
        return

    def inside(key):
        h = heights.get(key)
        return h is not None and low <= h <= high

    window = {n: {k for k in seen[n] if inside(k)} for n in names}
    union = set().union(*window.values())
    unit = "blocks" if chain == "Robinhood Chain" else "slots"
    print(f"\n{chain} — {len(union)} trades over {high - low + 1} {unit} every transport covered")
    print(f"{'':14}{'trades':>8}{'first':>8}{'p50 lag':>10}{'p90 lag':>10}{'missed':>8}")
    for n in names:
        keys = window[n]
        lags = sorted(seen[n][k][2] - min(seen[m][k][2] for m in names if k in seen[m]) for k in keys)
        first = sum(1 for lag in lags if lag <= 0.0005)
        share = f"{100 * first / len(keys):.0f}%" if keys else "-"
        p50 = f"{statistics.median(lags):.2f}" if lags else "-"
        p90 = f"{lags[int(len(lags) * 0.9)]:.2f}" if lags else "-"
        print(f"  {n:<12}{len(keys):>8}{share:>8}{p50:>10}{p90:>10}{len(union - keys):>8}")

    for n in names:
        missed = union - window[n]
        if missed:
            lost = Counter(heights[k] for k in missed)
            whole = sum(1 for h, c in lost.items() if c == sum(1 for k in union if heights[k] == h))
            print(f"  {n} missed {len(missed)} across {len(lost)} {unit}, {whole} of them lost whole")

    # "OTHER" is a transport saying it cannot tell, not a rival opinion: nothing the
    # wallet owns changed, so there is no amount to read and no side to take.
    def opinions(k):
        return {seen[n][k][0] for n in names if k in window[n]} - {"OTHER"}

    disagree = [(k, {n: seen[n][k][0] for n in names if k in window[n]}) for k in union if len(opinions(k)) > 1]
    print(f"  classification disagreements: {len(disagree)}")
    for key, how in disagree[:5]:
        print(f"    {key[:20]}… {how}")


async def main():
    seconds, asked = 60, []
    for arg in sys.argv[1:]:
        if arg.isdigit():
            seconds = int(arg)
        elif arg in COMPARABLE:
            asked.append(arg)
        else:
            sys.exit(f"unknown transport {arg!r} — one of {', '.join(COMPARABLE)}")
    wanted = asked or list(COMPARABLE)

    names = []
    for n in wanted:
        if why := transports.unconfigured(n):
            print(f"[skipping {n}: {why}]")
        else:
            names.append(n)
    if not names:
        sys.exit("no transport has its endpoint configured, see .env.example")

    hosts = Counter(chain_of(n) for n in names)
    for chain, n in hosts.items():
        if n > 1:
            print(f"[{n} transports share the {chain} endpoint and will contend for it]")
    print(f"\ncomparing {', '.join(names)} for {seconds} s\n")

    seen, heights = defaultdict(dict), {}

    def emitter(name):
        """One transport's `emit`: when it saw the trade, under the key they all agree on."""

        def emit(trade):
            seen[name][trade.key] = (trade.kind, trade.where, time.time())
            if trade.key not in heights or heights[trade.key] is None:
                heights[trade.key] = height(trade.where)

        return emit

    deadline = time.time() + seconds
    jobs = [importlib.import_module(TRANSPORTS[n].module).listen(deadline, set(), emitter(n), Counter()) for n in names]
    # A transport that dies takes its own measurement down and nothing else: the others
    # keep decoding, and what it managed before it stopped is still in the table.
    outcomes = await asyncio.gather(*jobs, return_exceptions=True)
    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, Exception):
            print(f"[{name} stopped: {type(outcome).__name__}: {outcome}]")

    for chain in CHAIN_NAME.values():
        here = [n for n in names if chain_of(n) == chain]
        if len(here) < 2:
            if here:
                print(f"\n{chain} — only {here[0]} ran, nothing to compare it against")
            continue
        report(chain, here, seen, heights)


if __name__ == "__main__":
    asyncio.run(main())
