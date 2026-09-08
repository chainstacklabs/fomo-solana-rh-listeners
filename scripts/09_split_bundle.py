"""One transaction, several people's trades. This shows which part belongs to whom.

FOMO submits trades in bundles: a single `handleOps` transaction carries the operations of
a dozen unrelated users, and its receipt is one flat list of logs. Read that list as a
whole and you will credit somebody else's swap to your user — wrong token, wrong amount,
and nothing errors.

The boundary is `UserOperationEvent`: every log before one, back to the previous one,
belongs to that operation. Run this on any bundle to see the split, or with an address to
see only that user's slice.

Reference: docs/04-listening.md (Reading a bundle).

    uv run scripts/09_split_bundle.py <tx hash> [address]
"""

import contextlib
import sys

from eth_abi import decode as abi_decode
from shared import env
from shared.robinhood import (
    BEFORE_EXECUTION,
    DEPOSIT,
    TRANSFER,
    USEROP,
    V4_FIELDS,
    V4_SWAP,
    VENUES,
    topic,
)
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(env.endpoint("ROBINHOOD_MAINNET_HTTPS")))


if len(sys.argv) < 2:
    sys.exit(__doc__)
tx_hash = sys.argv[1]
want = Web3.to_checksum_address(sys.argv[2]) if len(sys.argv) > 2 else None
try:
    logs = w3.eth.get_transaction_receipt(tx_hash)["logs"]
except Exception as e:  # not on this chain, or not a transaction hash at all
    sys.exit(f"cannot read {tx_hash}: {type(e).__name__}")


def meta(address):
    fields = (("symbol", "string"), ("decimals", "uint8"), ("fee", "uint24"))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=[
            {"name": n, "type": "function", "inputs": [], "outputs": [{"type": t}], "stateMutability": "view"}
            for n, t in fields
        ],
    )
    out = {}
    for name, _ in fields:
        with contextlib.suppress(Exception):  # a pool has a fee, a token does not
            out[name] = getattr(contract.functions, name)().call()
    return out


start = next((i for i, log in enumerate(logs) if topic(log) == BEFORE_EXECUTION), -1)
spans, current = [], []
for log in logs[start + 1 :]:
    if topic(log) == USEROP:
        spans.append((Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:]), current))
        current = []
    else:
        current.append(log)

print(f"{tx_hash}")
if not spans:
    sys.exit(f"{len(logs)} logs and no UserOperationEvent, so this is not a bundle of operations")
attributed = sum(len(span) for _, span in spans)
print(
    f"{len(logs)} logs, {len(spans)} users in this bundle, "
    f"{len(logs) - attributed} boundary logs belonging to none of them\n"
)

for sender, span in spans:
    if want and sender != want:
        print(f"  {sender}  {len(span):>3} logs")
        continue
    print(f"  {sender}  {len(span):>3} logs" + ("   <- asked for" if want else ""))
    for log in span:
        kind = topic(log)
        address = Web3.to_checksum_address(log["address"])
        if kind == TRANSFER and len(log["topics"]) >= 3:
            info = meta(address)
            raw = int(log["data"].hex().replace("0x", "") or "0", 16)
            decimals = info.get("decimals")
            # Unscaled and marked when decimals could not be read, rather than
            # divided by one and printed as though it were a token amount.
            shown = f"{raw / 10**decimals:>24.9f}" if decimals is not None else f"{'? ' + str(raw):>24}"
            print(
                f"      moved {shown} {info.get('symbol', '?'):<10} "
                f"0x{log['topics'][1].hex()[-40:][:8]}… -> 0x{log['topics'][2].hex()[-40:][:8]}…"
            )
        elif kind in VENUES:
            # V4 is one singleton contract for every pool, so the emitting address
            # names nothing and has no fee() to ask. The pool is topic 1, and the fee
            # actually charged on this swap is the last field of the data, which is
            # better than a pool's static rate because a hook can override it. V3 and
            # its forks are one contract per pool, where the address and the call hold.
            if kind == V4_SWAP:
                pool = topic(log, 1)
                fee = abi_decode(V4_FIELDS, bytes.fromhex(log["data"].hex().replace("0x", "")))[5]
            else:
                pool, fee = address, meta(address).get("fee")
            # Both are hundredths of a basis point, so 3000 is 0.30%.
            rate = f"{fee / 10000:.4f}%" if fee is not None else "not exposed"
            print(f"      swap  {VENUES[kind]:<22} pool {pool}  fee {rate}")
        elif kind == DEPOSIT:
            data = log["data"].hex().replace("0x", "")
            print(f"      paid into Relay, amount {int(data[128:192], 16)}, order id 0x{data[192:256]}")
    print()

if want and all(sender != want for sender, _ in spans):
    print(f"  {want} has no operation in this bundle")
