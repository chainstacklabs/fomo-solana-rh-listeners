"""Listen to Relay's payouts on Solana: the far half of a sell made on another chain.

A sell executed on Robinhood Chain hands its proceeds to Relay there, and Relay pays the
seller in USDC here. That payout is Relay's fill on the destination chain, and it is signed
by a solver rather than by FOMO's co-signer, so no listener keyed on the co-signer ever
receives it. The only handle on it is the solver's own address: the payout calls no Relay
program, emits no event, and is a plain spl-token transfer with the 32-byte order id in an
spl-memo beside it.

Not every payout on this stream is a token transfer. FOMO's own are USDC, but about one in
ten of the solver's is native SOL, sent as a system transfer with the same order-id memo and
no token account anywhere in the transaction, so reading token balances alone drops those
without a trace. Lamport gains are read whenever no token moved, which is the only way those
payouts are seen at all — they belong to other applications, and losing them uncounted makes
this stream look quieter than it is.

This is therefore not a FOMO feed and does not pretend to be one. The solver serves every
application and every origin chain Relay settles from, and roughly half of what arrives
here was paid for somewhere other than Robinhood Chain. Nothing in a payout says which
application it belongs to: the second memo it carries is a nonce whose first characters
spell the unix time, not a tag. Attribution needs the deposit behind the order id, which
lives on the other chain, so 00_listen_fomo.py is where a payout becomes a named FOMO sell
— it holds each sell it sees on Robinhood Chain and matches this order id against it.

Emits FILL, defined in shared/feed.py: money leaving Relay, with the recipient and the
order id that says which sell it settles. `Trade.app` is left empty because this chain
cannot answer it.

Solver addresses rotate the way Robinhood Chain's bundlers do. When this one is replaced
the stream goes quiet rather than wrong, so a run that reports no payouts at all is a
reason to re-check the address rather than to conclude that nobody sold; the address is in
maintenance/registry.json under `solana.settlement`, and maintenance/verify_registry.py
checks it.

Runs on an ordinary WebSocket endpoint, with no add-on. For the same solver read one
commitment earlier, see 06_listen_relay_payouts_grpc.py; both apply the same rules to the
same flow, and maintenance/compare_listeners.py measures what the difference is worth.

Reference: docs/04-listening.md (Solana, Joining the two legs), docs/05-relay.md (Solvers).

    uv run scripts/05_listen_relay_payouts_blocks.py [seconds] [wallet ...]     default 120
"""

import asyncio
import json
import time

import websockets
from shared import cli, env
from shared.feed import Trade
from shared.solana import MAX_FRAME_BYTES, MEMO_PROGRAMS, SETTLEMENT_SOLVER, SOL_DUST, block_subscription, mint_name

WEBSOCKET = env.optional("SOLANA_MAINNET_WSS")


def order_of(instructions):
    """The Relay order id a payout carries, out of the memo that holds one."""
    for instruction in instructions:
        if instruction.get("programId") not in MEMO_PROGRAMS:
            continue
        note = instruction.get("parsed")
        # The other memo is a nonce beginning with the unix time, so shape is the test.
        if isinstance(note, str) and note.startswith("0x") and len(note) == 66:
            return note.lower()
    return ""


def credited(meta, keys, solver):
    """(recipient, amount, mint) of the largest gain that is not the solver's own.

    Tokens first, then lamports. A payout can be native SOL — a system transfer carrying
    the order id in a memo, with no token account anywhere in the transaction — and
    reading only token balances drops those without noticing.
    """

    def held(side):
        totals = {}
        for balance in meta.get(side) or []:
            owner = balance.get("owner")
            if owner and owner != solver:
                key = (owner, balance["mint"])
                totals[key] = totals.get(key, 0) + float(balance["uiTokenAmount"]["uiAmountString"] or 0)
        return totals

    before, after = held("preTokenBalances"), held("postTokenBalances")
    gains = [(after[k] - before.get(k, 0), k) for k in after if after[k] - before.get(k, 0) > 1e-12]
    if gains:
        amount, (owner, mint) = max(gains)
        return owner, amount, mint
    # Read the same way as the token balances above: a payload the node serves oddly
    # yields no recipient, and the listener carries on. Subscripting here instead would
    # end the run on one transaction, and the sell leg would be gone until it was noticed.
    lamports = [
        ((post - pre) / 1e9, keys[i])
        for i, (pre, post) in enumerate(
            zip(meta.get("preBalances") or [], meta.get("postBalances") or [], strict=False)
        )
        if i < len(keys) and keys[i] != solver and (post - pre) / 1e9 > SOL_DUST
    ]
    if not lamports:
        return "", 0.0, ""
    amount, owner = max(lamports)
    return owner, amount, "SOL"


async def listen(deadline, watched, emit, counts):
    """Decode this transport until `deadline`, calling `emit` with one Trade per payout.

    `watched` is a set of base58 recipient addresses, empty for every recipient. There is
    no application filter here and there cannot be one: what identifies a payout as FOMO's
    is the deposit on Robinhood Chain that its order id names. `Trade.key` is the
    signature and `Trade.order_id` the order id, which is what joins it to that sell.
    """
    env.require("SOLANA_MAINNET_WSS")

    async with websockets.connect(WEBSOCKET, max_size=MAX_FRAME_BYTES, close_timeout=1) as socket:
        await socket.send(block_subscription(SETTLEMENT_SOLVER))
        error = json.loads(await socket.recv()).get("error")
        if error:
            # Raised rather than printed: under `00` a printed line scrolls past under the
            # header, and the run then reports a chain with nothing to say.
            raise RuntimeError(f"the node refused blockSubscribe on SETTLEMENT_SOLVER: {error}")

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
                meta = entry.get("meta") or {}
                transaction = entry.get("transaction") or {}
                accounts = (transaction.get("message") or {}).get("accountKeys") or []
                if meta.get("err") or not accounts:
                    continue
                keys = [k["pubkey"] for k in accounts]
                signers = [k["pubkey"] for k in accounts if k.get("signer")]
                if SETTLEMENT_SOLVER not in signers:
                    continue
                order = order_of(transaction["message"].get("instructions") or [])
                if not order:
                    # Relay's own plumbing rather than somebody's payout.
                    counts["solver transactions that are not payouts"] += 1
                    continue
                recipient, amount, mint = credited(meta, keys, SETTLEMENT_SOLVER)
                if not recipient:
                    # An order id but nothing credited: counted, never dropped quietly.
                    counts["payouts whose recipient could not be read"] += 1
                    continue
                if watched and recipient not in watched:
                    continue
                # Read before the tally, and out of the transaction already bound above:
                # subscripting a payload that arrived without it would end the run on one
                # transaction, and the sell leg would be gone until somebody noticed.
                signatures = transaction.get("signatures") or []
                if not signatures:
                    counts["payouts with no signature to name them by"] += 1
                    continue
                # Counted as payouts rather than as FILL rows: 00_listen_fomo.py prints
                # only the ones it can name, so a FILL tally there would overstate them.
                counts["payouts"] += 1
                signature = signatures[0]
                got = f"{amount:.6f} {mint_name(mint)}"
                emit(Trade("FILL", "", got, recipient, "", "", f"slot {value.get('slot')}", signature, order))


if __name__ == "__main__":
    cli.run(
        "payouts-blocks",
        listen,
        "every application Relay settles on Solana, not FOMO's alone",
    )
