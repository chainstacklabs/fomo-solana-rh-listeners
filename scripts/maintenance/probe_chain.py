"""Does FOMO run on this EVM chain the way it runs on Robinhood Chain, and how cleanly can it be told apart?

Run this before a chain gets a row in the listeners, and again on the day FOMO turns a new one
on. Everything the Robinhood Chain listeners rely on is a claim about one chain: that the
operations contract is version 0.8, that FOMO wallets carry one EIP-7702 delegation, that Relay's
depository, router and executor sit at fixed addresses, and that the delegation is FOMO's alone.
The last of those is the one that does not travel. The delegation points at the reference
`Simple7702Account`, deployed at the same address on every chain, and it names FOMO on Robinhood
Chain only because FOMO is nearly its only tenant there.

So this reports four things off the chain itself:

- which of the contracts the listeners key on have code here;
- what the wallets already known from Robinhood Chain carry here. A Privy wallet has the same
  address on every EVM chain, so those wallets are ground truth that costs nothing;
- over a recent window, who sends operations through version 0.8: how many senders carry the
  delegation, and how many of those are wallets known to be FOMO's. That second share is the
  marker's precision on this chain;
- how busy Relay is here: fills out of the executors, the recipients that carry the delegation,
  and deposits. A fill names its order id only in its calldata, so on a chain where Relay is
  busy every fill a listener has to join costs a transaction fetch.

Known wallets are every EVM wallet the listeners have recorded as FOMO's in `runs/`, on any
chain. With no recordings the comparisons are skipped and the run says so.

    uv run scripts/maintenance/probe_chain.py <chain> [seconds] [wallets]   default 600, 300
    chain: a key of shared/evm.py's CHAINS, or base | eth | monad | elysium
"""

import json
import sys
from collections import Counter
from pathlib import Path

import requests

# This runs from scripts/maintenance/ but imports shared/ from scripts/, so the path goes
# on before shared/ is imported. The recordings are in scripts/runs/.
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import env
from shared.evm import (
    CHAINS,
    DELEGATION,
    DEPOSIT,
    DEPOSITORY,
    ENTRYPOINT,
    EXECUTOR_TOPICS,
    EXECUTORS,
    FOMO_CODE,
    FOMO_IMPLEMENTATION,
    NATIVE_DEPOSIT,
    ROUTERS,
    TRANSFER,
    USEROP,
)

# Every chain with a row in shared/evm.py, plus the ones that do not have one yet: probing
# a chain is how it earns its row, so this cannot be limited to the table.
PREFIX = {network: chain.variable for network, chain in CHAINS.items()}
PREFIX |= {"base": "BASE", "eth": "ETHEREUM", "monad": "MONAD", "elysium": "ELYSIUM"}
RUNS = Path(__file__).parent.parent / "runs"

args = sys.argv[1:]
if not args or args[0] not in PREFIX:
    sys.exit(f"usage: probe_chain.py <{' | '.join(PREFIX)}> [seconds] [wallets]")
chain = args[0]
seconds = int(args[1]) if len(args) > 1 else 600
sample = int(args[2]) if len(args) > 2 else 300
url = env.endpoint(f"{PREFIX[chain]}_MAINNET_HTTPS")
session = requests.Session()


def rpc(method, *params):
    """One JSON-RPC call. An error is raised with the node's own message, never the URL."""
    reply = session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, timeout=60)
    body = reply.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error'].get('message', body['error'])}")
    return body["result"]


def batch(method, params_list, size=50):
    """Many calls of one method, sent in batches. A failed entry comes back as None."""
    out = []
    for i in range(0, len(params_list), size):
        chunk = params_list[i : i + size]
        payload = [{"jsonrpc": "2.0", "id": j, "method": method, "params": p} for j, p in enumerate(chunk)]
        replies = session.post(url, json=payload, timeout=60).json()
        by_id = {r["id"]: r.get("result") for r in replies} if isinstance(replies, list) else {}
        out.extend(by_id.get(j) for j in range(len(chunk)))
    return out


def logs(criteria, low, high):
    """Every log matching `criteria` over [low, high], paging and halving on a refused range.

    Providers cap ranges differently per chain, and some answer a range that is too wide
    with an error rather than a short list, so the step shrinks until it is accepted.
    """
    found, start, step = [], low, 2000
    while start <= high:
        end = min(start + step - 1, high)
        try:
            found += rpc("eth_getLogs", {**criteria, "fromBlock": hex(start), "toBlock": hex(end)})
            start = end + 1
        except RuntimeError:
            if step == 1:
                raise
            step = max(1, step // 2)
    return found


def label(code):
    """What an address's code says it is: fomo, another delegation, an EOA, or a contract."""
    code = (code or "0x").lower()
    if code == "0x":
        return "eoa"
    if code == FOMO_CODE:
        return "fomo delegation"
    if code.startswith(DELEGATION):
        return "other delegation " + code[len(DELEGATION) : len(DELEGATION) + 8] + "…"
    return "contract"


def codes(addresses):
    """Each address's label, read in batches at `latest`."""
    found = batch("eth_getCode", [[a, "latest"] for a in addresses])
    return dict(zip(addresses, map(label, found), strict=True))


def share(part, whole):
    return f"{part}/{whole}" + (f" ({100 * part / whole:.0f}%)" if whole else "")


head = int(rpc("eth_blockNumber"), 16)
print(f"{chain}: chain id {int(rpc('eth_chainId'), 16)}, head {head}\n")

print("-- contracts the listeners key on --")
contracts = {
    "EntryPoint v0.8": ENTRYPOINT,
    "Simple7702Account (FOMO's implementation)": "0x" + FOMO_IMPLEMENTATION,
    "Relay depository": DEPOSITORY,
    **{f"Relay router {i + 1}": a for i, a in enumerate(ROUTERS)},
    **{f"Relay executor {i + 1}": a for i, a in enumerate(EXECUTORS)},
}
for name, address in contracts.items():
    size = (len(rpc("eth_getCode", address, "latest")) - 2) // 2
    print(f"  {'yes' if size else 'NO ':3}  {name:44} {address}  {size} bytes")

known = set()
for path in RUNS.glob("*.jsonl"):
    for line in path.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("chain") != "SOL" and row.get("app") == "fomo" and row.get("who", "").startswith("0x"):
            known.add(row["who"].lower())
if not known:
    print("\nno FOMO wallets recorded in runs/: run an EVM listener to compare against them")

if known:
    picked = sorted(known)[:sample]
    print(f"\n-- {len(picked)} of {len(known)} FOMO wallets recorded on EVM chains, read here --")
    for kind, n in Counter(codes(picked).values()).most_common():
        print(f"  {n:5}  {kind}")

# A window of time rather than of blocks, because block times differ by two orders of
# magnitude across these chains. Block time is measured here, not assumed.
older = int(rpc("eth_getBlockByNumber", hex(head - 1000), False)["timestamp"], 16)
newer = int(rpc("eth_getBlockByNumber", hex(head), False)["timestamp"], 16)
block_time = (newer - older) / 1000
low = head - int(seconds / block_time)
print(f"\n-- last {seconds} s: blocks {low}–{head}, {block_time:.2f} s a block --")

operations = logs({"address": ENTRYPOINT, "topics": [USEROP]}, low, head)
senders = sorted({"0x" + log["topics"][2][-40:] for log in operations})
sender_codes = codes(senders)
delegated = [s for s in senders if sender_codes[s] == "fomo delegation"]
print(f"  v0.8 operations {len(operations)}, distinct senders {len(senders)}")
for kind, n in Counter(sender_codes.values()).most_common(6):
    print(f"    {n:5}  {kind}")
print(f"  senders carrying the delegation      {share(len(delegated), len(senders))}")
if known:
    print(f"  ...of which known FOMO wallets       {share(sum(s in known for s in delegated), len(delegated))}")

# Who submitted each operation. FOMO's bundlers are a rotating fleet, so they are no filter,
# but a delegation that is FOMO's alone arrives through one fleet and nobody else's.
hashes = sorted({log["transactionHash"] for log in operations})
submitted = batch("eth_getTransactionByHash", [[h] for h in hashes])
bundler = {h: (tx or {}).get("from", "?").lower() for h, tx in zip(hashes, submitted, strict=True)}
fleets = Counter()
for log in operations:
    fomo = sender_codes["0x" + log["topics"][2][-40:]] == "fomo delegation"
    fleets[("fomo delegation" if fomo else "anything else", bundler[log["transactionHash"]].startswith("0x4337"))] += 1
print("  operations by sender's code and bundler (0x4337… is the vanity prefix FOMO's fleet uses)")
for kind in ("fomo delegation", "anything else"):
    print(f"    {kind:16} via 0x4337… {fleets[(kind, True)]:5}   via any other bundler {fleets[(kind, False)]:5}")

# The paymaster is topic 3 of `UserOperationEvent`. FOMO's bundler absorbs the gas and its
# operations name none; another application on the same delegation can be told apart by
# the paymaster sponsoring it.
paymasters = Counter(
    "none" if int(log["topics"][3], 16) == 0 else "0x" + log["topics"][3][-40:][:10] + "…"
    for log in operations
    if sender_codes["0x" + log["topics"][2][-40:]] == "fomo delegation"
)
print("  operations from senders carrying the delegation, by paymaster")
for paymaster, n in paymasters.most_common(6):
    print(f"    {n:5}  {paymaster}")

fills = logs({"topics": [TRANSFER, EXECUTOR_TOPICS]}, low, head)
recipients = sorted({"0x" + log["topics"][2][-40:] for log in fills if len(log["topics"]) > 2})
recipient_codes = codes(recipients)
fomo_recipients = [r for r in recipients if recipient_codes[r] == "fomo delegation"]
print(f"  executor Transfers {len(fills)} ({len(fills) / seconds:.2f}/s), {len(recipients)} distinct recipients")
print(f"  recipients carrying the delegation   {share(len(fomo_recipients), len(recipients))}")
if known:
    print(f"  recipients known as FOMO wallets     {share(sum(r in known for r in recipients), len(recipients))}")
    print(f"  ...known and carrying no delegation  {sum(r in known and r not in fomo_recipients for r in recipients)}")

deposits = logs({"address": DEPOSITORY, "topics": [[DEPOSIT, NATIVE_DEPOSIT]]}, low, head)
depositors = sorted({"0x" + log["data"][26:66] for log in deposits})
depositor_codes = codes(depositors)
print(f"  depository deposits {len(deposits)}, {len(depositors)} distinct depositors")
for kind, n in Counter(depositor_codes.values()).most_common(6):
    print(f"    {n:5}  {kind}")
delegated_depositors = sum(v == "fomo delegation" for v in depositor_codes.values())
print(f"  depositors carrying the delegation   {share(delegated_depositors, len(depositors))}")
if known:
    print(f"  depositors known as FOMO wallets     {share(sum(d in known for d in depositors), len(depositors))}")
