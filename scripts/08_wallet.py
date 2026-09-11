"""One wallet's past on Robinhood Chain: is it FOMO's, and what has it done?

The listeners only show what is happening now. This looks backwards at a single address
and answers three questions about it: what kind of account it is, which FOMO operations it
has run, and which tokens have moved in and out. Reach for it when a listener shows a
wallet worth a closer look, or to check that an address is FOMO's at all before building
anything around it.

A FOMO wallet is an ordinary address carrying one specific EIP-7702 delegation, and that
code is the only reliable marker: bundler addresses rotate, and `getNonce()` on the account
always reads zero because FOMO derives a fresh nonce namespace per operation. Every FOMO
operation arrives as `executeBatch`, the wrapper the wallet runs, so that is what the
operation column names; pass a transaction to 09_split_bundle.py to see the calls inside
one, and which logs in a shared bundle belong to this wallet.

Robinhood Chain only, so the address is a 0x one. There is no Solana equivalent and there
cannot be: nothing marks a Solana account as FOMO's, because the co-signer identifies the
transaction rather than the wallet. To follow a Solana wallet, pass its base58 address as a
filter to 00_listen_fomo.py or 04_listen_solana_blocks.py, and use 07_trace_trade.py to
cross one trade between the chains.

Both windows are answered whole at the default span, unlike the range reads warned about in
docs/04-listening.md: over 50000 blocks one eth_getLogs returns the same logs as the same
range split in two or in four.

Reference: docs/01-how-it-works.md (The wallet), docs/04-listening.md (Telling FOMO apart from everything else).

    uv run scripts/08_wallet.py <0x address> [blocks]     default 50000 blocks (~1.4 h)
"""

import datetime
import sys

from eth_abi import decode as abi_decode
from shared import env
from shared.cli import wallet_shaped
from shared.robinhood import DELEGATION, ENTRYPOINT, FOMO_CODE, TRANSFER, USEROP
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(env.endpoint("ROBINHOOD_MAINNET_HTTPS")))


SELECTORS = {
    "095ea7b3": "approve",
    "e8017952": "depositErc20",
    "49290c1c": "depositNative",
    "b61d27f6": "execute",
    "34fcd5be": "executeBatch",
}

if len(sys.argv) < 2:
    sys.exit(__doc__)
if not sys.argv[1].startswith("0x"):
    # A base58 address here is somebody looking for the Solana half of a wallet, which
    # this script cannot answer. Say where to go rather than failing inside web3.
    sys.exit(
        f"{sys.argv[1]} is not a Robinhood Chain address, and this script reads only that chain.\n"
        "For a Solana wallet, pass it to 00_listen_fomo.py or 04_listen_solana_blocks.py as a filter."
    )
# A 0x that is not 40 hex digits is a typo, and web3 answers one with a traceback out of
# its checksum helper. Say what the argument should look like instead, as every other
# script here does.
if not wallet_shaped(sys.argv[1]):
    sys.exit(f"{sys.argv[1]!r} is not an address: 0x and 40 hex digits")
who = Web3.to_checksum_address(sys.argv[1])
if len(sys.argv) > 2 and not sys.argv[2].isdigit():
    sys.exit(f"the second argument is a number of blocks, not {sys.argv[2]!r}")
span = int(sys.argv[2]) if len(sys.argv) > 2 else 50000
head = w3.eth.block_number
lo = head - span

code = "0x" + w3.eth.get_code(who).hex().removeprefix("0x")
print(f"address  {who}")
print(f"balance  {w3.eth.get_balance(who) / 1e18:.9f} ETH")
if code == "0x":
    print("type     plain address, no code")
elif code == FOMO_CODE:
    print("type     FOMO wallet — carries the EIP-7702 delegation FOMO uses")
elif code.startswith(DELEGATION):
    print(f"type     EIP-7702 delegation to 0x{code[len(DELEGATION) :]} — not FOMO's")
else:
    print(f"type     contract, {len(code[2:]) // 2} bytes")


def erc20(address, field, kind):
    abi = [{"name": field, "outputs": [{"type": kind}], "inputs": [], "stateMutability": "view", "type": "function"}]
    try:
        return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi).functions[field]().call()
    except Exception:
        # No invented scale: a guessed 18 prints a wrong number that looks right.
        return "?" if kind == "string" else None


print(f"\n-- FOMO operations, blocks {lo}-{head} --")
ops = w3.eth.get_logs(
    {
        "fromBlock": lo,
        "toBlock": head,
        "address": ENTRYPOINT,
        "topics": [USEROP, None, "0x" + "0" * 24 + who[2:].lower()],
    }
)
if not ops:
    print("  none in this window")
for log in ops:
    data = log["data"].hex().replace("0x", "")
    nonce, success, cost = (int(data[i * 64 : (i + 1) * 64], 16) for i in range(3))
    when = datetime.datetime.fromtimestamp((nonce >> 64) / 1000, datetime.UTC)
    tx = w3.eth.get_transaction(log["transactionHash"])
    # A bundle carries several users. Decode the whole array and pick this sender's
    # operation; searching the calldata for a selector finds somebody else's. The
    # selector is in callData, field 3. Field 2 is initCode, which an already
    # delegated wallet leaves empty, so reading it names every operation nothing.
    calls = []
    try:
        decoded, _ = abi_decode(
            ["(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]", "address"],
            bytes.fromhex(tx["input"].hex().replace("0x", "")[8:]),
        )
        for op in decoded:
            if Web3.to_checksum_address(op[0]) != who:
                continue
            calls = [SELECTORS.get(op[3].hex()[:8], op[3].hex()[:8])]
    except Exception:
        pass
    print(
        f"  blk {log['blockNumber']}  {when:%Y-%m-%d %H:%M:%S}  "
        f"{'ok' if success else 'FAILED':<7} gas paid by bundler: {cost == 0}  {' '.join(calls)}"
    )
    print(f"      tx {log['transactionHash'].hex()}")

print(f"\n-- token movements, blocks {lo}-{head} --")
padded = "0x" + "0" * 24 + who[2:].lower()
moves = 0
for label, topics in (("IN ", [TRANSFER, None, padded]), ("OUT", [TRANSFER, padded])):
    for log in w3.eth.get_logs({"fromBlock": lo, "toBlock": head, "topics": topics}):
        moves += 1
        decimals = erc20(log["address"], "decimals", "uint8")
        raw = int(log["data"].hex().replace("0x", "") or "0", 16)
        # Unscaled and marked when decimals could not be read, rather than divided by
        # a guessed 18, which prints a wrong number that looks right.
        shown = f"{raw / 10**decimals:>22.6f}" if decimals is not None else f"{'? ' + str(raw):>22}"
        other = "0x" + log["topics"][1 if label == "IN " else 2].hex()[-40:]
        print(
            f"  {label} {shown} {erc20(log['address'], 'symbol', 'string'):<10} "
            f"{'from' if label == 'IN ' else 'to'} {other[:12]}…  blk {log['blockNumber']}"
        )

if not moves:
    print("  none in this window")
