"""How a listener is started from the command line, and what it prints when it stops.

Five listeners were each carrying the same fifteen lines: refuse a first argument that is
not a duration, collect the wallets after it, check the endpoints, print a banner, open the
feed, run until the deadline, and tally what was counted. Five copies had already drifted —
two parsed argv one way and folded their wallets to lower case, three did neither — so an
argument bug had five places to be fixed and a reader had five versions to compare. The
shell is here; the decoding stays in each listener, where the wire format it reads is.

Nothing here makes a request: `run()` calls the `listen()` the caller hands it.
"""

import asyncio
import sys
import time
from collections import Counter

from shared import env, feed
from shared.transports import TRANSPORTS

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def wallet_shaped(arg):
    """Is this an address at all? A typo here silences a chain rather than erroring."""
    if arg.startswith("0x"):
        return len(arg) == 42 and all(c in "0123456789abcdefABCDEF" for c in arg[2:])
    return 32 <= len(arg) <= 44 and all(c in BASE58 for c in arg)


def host(url):
    """The host of an endpoint, which is the only part worth printing: the path is a key."""
    return url.split("://")[-1].split("/")[0]


def arguments(argv, chain):
    """(seconds, watched) from a listener's positional arguments.

    Addresses on Robinhood Chain are folded to lower case because the chain does not
    distinguish them; base58 does, so a Solana address is kept exactly as it was given.
    """
    seconds = 120
    rest = list(argv)
    if rest and rest[0].isdigit():
        seconds = int(rest.pop(0))
    elif rest:
        sys.exit(f"first argument is the number of seconds, not {rest[0]!r}")
    for arg in rest:
        # A placeholder pasted out of the README filters the feed down to a wallet that
        # cannot exist, which looks exactly like a chain that has gone quiet.
        if not wallet_shaped(arg):
            sys.exit(f"{arg!r} is not an address: 0x and 40 hex digits, or 32 to 44 base58 characters")
    return seconds, {a.lower() for a in rest} if chain == "RH" else set(rest)


def run(name, listen, aside=""):
    """Run one transport's `listen()` from the command line and report what it counted.

    `name` is its key in shared/transports.py, which is where the chain it reads, the
    variables it cannot start without, and what it subscribes to are all recorded.
    `aside` is a second line for a listener with something further to say.
    """
    transport = TRANSPORTS[name]
    seconds, watched = arguments(sys.argv[1:], transport.chain)
    # Every variable at once, before anything is printed: the first of them is the
    # endpoint subscribed to, and the rest are what that subscription needs to be opened.
    endpoint, *_ = env.require(*transport.needs)

    who = f", filtering {len(watched)} wallet(s)" if watched else ""
    print(f"subscribed to {host(endpoint)}, {transport.what}, {seconds} s{who}")
    if aside:
        print(aside)
    print()

    counts = Counter()
    show, done = feed.start(transport.module, transport.chain)
    try:
        asyncio.run(listen(time.time() + seconds, watched, show, counts))
    finally:
        # A transport failure raises, and what it decoded before it died is still worth
        # having: the sidecar is closed and the tally printed on the way out either way.
        done()
        print(f"\n{seconds} s")
        for label, n in counts.most_common():
            print(f"  {n:>5}  {label}")
