"""Who is actually trading on FOMO, mined out of every run this repository has recorded.

Testing anything against a live chain needs a wallet that trades, and a wallet picked out of
one listener run is usually somebody who traded once. This ranks by persistence instead: how
many separate runs an address appears in, which is the closest thing the recordings hold to
"still active". A wallet seen in eight runs across a week is worth pointing a script at; one
seen forty times inside a single run is one busy afternoon.

It also recovers what FOMO's backend does not publish. The Relay order id is written on both
chains, so a row on each side carrying the same id is one person: their Robinhood Chain
address and their Solana address, linked with no help from the app. Buys join a Solana
payment to a Robinhood fill, sells join a Robinhood deposit to a Solana payout.

Reads `scripts/runs/*.jsonl`, which the listeners write, and puts `wallets.local.json` beside
this script rather than beside them: nothing else reads it, and the top level of `scripts/` is
for what a person watching FOMO runs. Both are gitignored — recordings and the addresses mined
from them stay local, and only this script is committed. Nothing here touches the network: run
the listeners to gather more, then run this again.

Reference: docs/04-listening.md (Joining the two legs).

    uv run scripts/maintenance/active_wallets.py [count]      default 25 per section
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# The recordings stay in scripts/, beside the listeners that write them. What is mined out
# of them lands here, because this script is the only thing that knows it exists.
RUNS = Path(__file__).parent.parent / "runs"
OUT = Path(__file__).parent / "wallets.local.json"
STAMP = re.compile(r"(\d{8}-\d{6})")


def day_of(path):
    found = STAMP.search(path.name)
    return datetime.strptime(found.group(1), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d") if found else ""


def read():
    """(wallets, pairs) out of every recorded row."""
    wallets = defaultdict(lambda: {"chain": "", "kinds": Counter(), "runs": set(), "trades": 0, "last": ""})
    legs, pairs = defaultdict(dict), Counter()
    seen_days = {}
    for path in sorted(RUNS.glob("*.jsonl")):
        day = day_of(path)
        for line in path.open():
            try:
                row = json.loads(line)
            except Exception:
                continue  # a run killed mid-write leaves one truncated line
            who, chain = row.get("who"), row.get("chain")
            if not who or not chain:
                continue
            entry = wallets[who]
            entry["chain"] = chain
            entry["kinds"][row.get("kind", "?")] += 1
            entry["trades"] += 1
            entry["runs"].add(path.name)
            entry["last"] = max(entry["last"], day)
            # Runs recorded before the field was renamed carry the id as `link`. Both
            # names mean the Relay order id, and most of the corpus predates the rename,
            # so reading only the new name loses most of the pairs it should have found.
            order = row.get("order_id") or row.get("link")
            if order:
                legs[order][chain] = who
                seen_days[order] = day
    for order, sides in legs.items():
        if "RH" in sides and "SOL" in sides:
            pairs[(sides["RH"], sides["SOL"], seen_days[order])] += 1
    return wallets, pairs


def rank(wallets, chain, count):
    rows = [(w, e) for w, e in wallets.items() if e["chain"] == chain]
    rows.sort(key=lambda x: (len(x[1]["runs"]), x[1]["trades"]), reverse=True)
    return [
        {
            "address": w,
            "runs": len(e["runs"]),
            "trades": e["trades"],
            "kinds": {k: n for k, n in e["kinds"].most_common()},
            "lastSeen": e["last"],
        }
        for w, e in rows[:count]
    ]


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    files = sorted(RUNS.glob("*.jsonl"))
    if not files:
        sys.exit("no recordings in scripts/runs — run a listener first")
    wallets, pairs = read()

    # A pair is worth more than a single address: it is one person on two chains, which
    # nothing public states. Rank by how many separate trades joined the two.
    joined = {}
    for (rh, sol, day), n in pairs.most_common():
        joined.setdefault((rh, sol), {"robinhood": rh, "solana": sol, "joins": 0, "lastSeen": ""})
        joined[(rh, sol)]["joins"] += n
        joined[(rh, sol)]["lastSeen"] = max(joined[(rh, sol)]["lastSeen"], day)
    best = sorted(joined.values(), key=lambda p: (p["joins"], p["lastSeen"]), reverse=True)[:count]

    OUT.write_text(
        json.dumps(
            {
                "_what": "Wallets seen trading through FOMO, mined from scripts/runs/*.jsonl "
                "by scripts/maintenance/active_wallets.py.",
                "_rank": "By how many separate runs an address appears in, not by trade count: persistence is the "
                "closest thing a recording holds to still being active.",
                "_pairs": "One Relay order id seen on both chains is one person. FOMO does not publish this mapping.",
                "_refresh": "Run the listeners to record more, then: uv run scripts/maintenance/active_wallets.py",
                "_builtAt": datetime.now().strftime("%Y-%m-%d"),
                "_from": {"runs": len(files), "wallets": len(wallets), "pairs": len(joined)},
                "pairs": best,
                "robinhood": rank(wallets, "RH", count),
                "solana": rank(wallets, "SOL", count),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{len(wallets)} wallets over {len(files)} runs, {len(joined)} cross-chain pairs")
    print(f"kept the top {count} of each in {OUT}\n")
    for pair in best[:5]:
        print(f"  {pair['robinhood']}  {pair['solana']}  {pair['joins']} joins  {pair['lastSeen']}")


if __name__ == "__main__":
    main()
