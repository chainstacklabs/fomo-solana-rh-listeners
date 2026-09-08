"""Listen to FOMO trades on Solana with blockSubscribe over a standard WebSocket.

Blocks arrive with every transaction and its metadata attached, filtered to the ones
mentioning FOMO's co-signer — which signs and pays for every FOMO transaction on this
chain, and is the only marker there is, since a FOMO wallet is an ordinary keypair. The
amounts, the mints and the programs are all in the pushed block, so nothing has to be
fetched per transaction.

Emits SWAP, PAY, IN, OUT and OTHER, defined in shared/feed.py and decided by
shared/solana.py from what the wallet's balances did. FOMO routes swaps through more than
one program — DFlow, Jupiter and a third, unnamed router have all been seen — so a swap is
recognised by what moved rather than by the program called, and the router is reported.

Closing a wrapped-SOL account refunds its rent to the wallet, which shows up as a few
thousandths of a SOL next to the real trade; SOL movements that small are ignored whenever
anything else moved.

This needs no paid add-on, which is the reason to use it over 03_listen_solana_grpc.py.
How quickly and how completely a given endpoint serves it has varied, so measure yours
with maintenance/compare_listeners.py rather than assuming. `listen()` here is the one
implementation of this transport; 00_listen_fomo.py imports it.

Reference: docs/04-listening.md (Solana, Transports).

    uv run scripts/04_listen_solana_blocks.py [seconds] [wallet ...]     default 120
"""

import asyncio
import json
import time

import base58
import websockets
from shared import cli, env
from shared.feed import Trade
from shared.solana import CO_SIGNER, DEPOSIT_DISCRIMINATOR, MAX_FRAME_BYTES, RELAY_DEPOSIT, block_subscription, classify

WEBSOCKET = env.optional("SOLANA_MAINNET_WSS")


async def listen(deadline, watched, emit, counts):
    """Decode this transport until `deadline`, calling `emit` with one Trade per trade.

    `watched` is a set of base58 wallet addresses, empty for every wallet. There is no
    application filter here as there is on Robinhood Chain: Solana has no code-based
    marker, so everything reached through FOMO's co-signer is FOMO's. `Trade.key` is
    the signature, which identifies one trade across transports.
    """
    env.require("SOLANA_MAINNET_WSS")

    async with websockets.connect(WEBSOCKET, max_size=MAX_FRAME_BYTES, close_timeout=1) as socket:
        await socket.send(block_subscription(CO_SIGNER))
        error = json.loads(await socket.recv()).get("error")
        if error:
            # Raised rather than printed: under `00` a printed line scrolls past under the
            # header, and the run then reports a chain with nothing to say.
            raise RuntimeError(f"the node refused blockSubscribe on CO_SIGNER: {error}")

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.time()))
            except TimeoutError:
                break
            value = json.loads(raw).get("params", {}).get("result", {}).get("value", {})
            block = value.get("block")
            if not block:
                continue
            counts["blocks"] += 1
            for entry in block.get("transactions", []):
                meta, transaction = entry.get("meta") or {}, entry["transaction"]
                if meta.get("err"):
                    continue
                keys = [k["pubkey"] for k in transaction["message"]["accountKeys"]]
                signers = [k["pubkey"] for k in transaction["message"]["accountKeys"] if k.get("signer")]
                if CO_SIGNER not in signers:
                    continue
                user = next((s for s in signers if s != CO_SIGNER), None)
                if not user or (watched and user not in watched):
                    continue

                loaded = meta.get("loadedAddresses") or {}
                every = keys + loaded.get("writable", []) + loaded.get("readonly", [])
                programs = {i.get("programId") for i in transaction["message"]["instructions"]}
                order = ""
                for instruction in transaction["message"]["instructions"]:
                    if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
                        continue
                    raw = base58.b58decode(instruction["data"])
                    if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
                        order = "0x" + raw[16:48].hex()
                        break

                def amounts(balances):
                    return {
                        (b["accountIndex"], b["mint"]): float(b["uiTokenAmount"]["uiAmountString"] or 0)
                        for b in balances
                    }

                before, after = amounts(meta.get("preTokenBalances", [])), amounts(meta.get("postTokenBalances", []))
                seen = meta.get("preTokenBalances", []) + meta.get("postTokenBalances", [])
                owners = {(b["accountIndex"], b["mint"]): b.get("owner") for b in seen}
                moved = []
                for account in set(before) | set(after):
                    if owners.get(account) != user:
                        continue
                    change = after.get(account, 0) - before.get(account, 0)
                    if abs(change) > 1e-12:
                        moved.append((change, account[1]))
                # A side denominated in native SOL never appears in token balances, so
                # read it from the account's own lamports.
                if user in keys:
                    i = keys.index(user)
                    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
                    if i < len(pre) and i < len(post):
                        lamports = (post[i] - pre[i]) / 1e9
                        if abs(lamports) > 1e-9:
                            moved.append((lamports, "SOL"))

                relay = RELAY_DEPOSIT in programs or RELAY_DEPOSIT in every
                kind, gave, got, note = classify(moved, programs, relay)
                counts[kind] += 1
                signature = entry["transaction"]["signatures"][0]
                emit(Trade(kind, gave, got, user, "fomo", note, f"slot {value.get('slot')}", signature, order))


if __name__ == "__main__":
    cli.run("sol-blocks", listen)
