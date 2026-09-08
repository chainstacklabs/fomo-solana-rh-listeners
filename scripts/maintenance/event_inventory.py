"""Find out what a contract emits, when the registry no longer matches reality.

Use this when something breaks: a listener that worked yesterday goes quiet,
verify_registry.py fails, or FOMO ships an update and the addresses in registry.json
stop producing events. It walks every contract recorded in registry.json and reports what
each actually emitted over a block window — which topics fired, how often, and the decoded
signature where it can be recovered. A recorded contract emitting nothing is the registry
going stale.

The list of contracts comes from registry.json rather than a copy kept here, so an address
added to the registry is inventoried without editing this script. The EOAs are skipped
because an EOA emits nothing.

Signatures are named from the registry's own event map first and from 4byte only for the
topics it does not carry. A contract can also emit anonymous logs, which carry no topic0 at
all and are counted as `(anonymous)`: the 0x Settler on this chain does, and its events are
readable only by position.

The window is small by default: it covers the Uniswap V4 PoolManager, which carries every
V4 pool on the chain and tens of thousands of swaps an hour, so a wide span spends most of
its time re-reading one contract. Widen it when a quiet contract is the question.

Reference: docs/04-listening.md (Event surface), docs/07-addresses.md.

    uv run scripts/maintenance/event_inventory.py [blocks]      default 2000 (~3 min of chain)
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

from web3 import Web3

# This runs from scripts/maintenance/ but imports shared/ from scripts/, so the path goes
# on before shared/ is imported. registry.json is beside this file.
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import env

w3 = Web3(Web3.HTTPProvider(env.endpoint("ROBINHOOD_MAINNET_HTTPS")))
reg = json.loads((Path(__file__).parent / "registry.json").read_text())

ANONYMOUS = "(anonymous)"


def contracts():
    """(label, address, note) for every contract the registry records on this chain."""
    out = []
    for group in ("accountLayer", "intentLayer", "aggregationLayer"):
        for name, entry in reg[group].items():
            if isinstance(entry, dict) and "address" in entry and "EOA" not in entry.get("what", ""):
                out.append((name, entry["address"], entry.get("what", "")))
    venues = reg["venues"]
    manager = venues["uniswapV4PoolManager"]
    out.append(("uniswapV4PoolManager", manager["address"], manager["what"]))
    for i, factory in enumerate(venues["factories"]):
        out.append((f"factory[{i}]", factory["address"], factory.get("role", "AMM factory")))
    for address, entry in venues["nonUniswapVenues"].items():
        if not address.startswith("_"):
            out.append(("nonUniswapVenue", address, f"{entry['codesize']} bytes, its own event shape"))
    for symbol, entry in reg["tokens"].items():
        if isinstance(entry, dict) and "address" in entry:
            out.append((symbol, entry["address"], f"{entry['decimals']} decimals"))
    return out


def registry_names():
    """topic0 -> signature, from the registry's own event and swap-shape maps."""
    out = {}
    for topic, entry in reg["events"].items():
        abi = entry.get("abi", "")
        if "(" in abi:
            out[topic.lower()] = abi
    for topic, shape in reg["venues"]["swapEventShapes"].items():
        out[topic.lower()] = shape.split(" - ")[0]
    return out


KNOWN = registry_names()
_cache = {}


def resolve(topic0):
    """Name an event: the registry first, then 4byte. Cache and tolerate failure."""
    if topic0 in KNOWN:
        return KNOWN[topic0]
    if topic0 in _cache:
        return _cache[topic0]
    name = "-"
    for _ in range(2):
        try:
            u = f"https://www.4byte.directory/api/v1/event-signatures/?hex_signature={topic0}"
            req = urllib.request.Request(u, headers={"User-Agent": "robinhood-fomo/1.0"})
            d = json.load(urllib.request.urlopen(req, timeout=15))
            r = [x["text_signature"] for x in d.get("results", [])]
            if r:
                name = r[0]
            break
        except Exception:
            time.sleep(1.5)
    _cache[topic0] = name
    return name


span = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
head = w3.eth.block_number
lo = head - span
print(f"blocks {lo}..{head}  (~{span * reg['chain']['blockTimeSeconds'] / 60:.0f} min)\n")

for label, addr, note in contracts():
    try:
        logs = w3.eth.get_logs({"fromBlock": lo, "toBlock": head, "address": Web3.to_checksum_address(addr)})
    except Exception as e:
        print(f"## {label}  {addr}\n   query failed: {str(e)[:70]}\n")
        continue
    counts = {}
    for log in logs:
        topics = log["topics"]
        t = "0x" + topics[0].hex().removeprefix("0x").rjust(64, "0") if topics else ANONYMOUS
        counts[t] = counts.get(t, 0) + 1
    print(f"## {label}   {addr}")
    print(f"   {note[:76]} | {len(logs)} logs in window | {len(counts)} event types")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        name = resolve(t) if t.startswith("0x") else "no topic0 emitted, decode by position"
        print(f"     {t:66}  x{n:<7} {name}")
    print()
