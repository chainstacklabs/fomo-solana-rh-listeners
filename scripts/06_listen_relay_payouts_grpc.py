"""Listen to Relay's payouts on Solana over a Yellowstone gRPC (Geyser) subscription.

The far half of a sell made on another chain. A sell executed on Robinhood Chain hands its
proceeds to Relay there, and Relay pays the seller in USDC here, signed by a solver rather
than by FOMO's co-signer. `account_include` matches an account anywhere in a transaction,
signer or not, so the solver is as reachable over gRPC as it is over `blockSubscribe`, and
this reads the same flow as 05_listen_relay_payouts_blocks.py one commitment earlier.

Payouts call no Relay program and emit no event. The order id travels in an spl-memo, and
about one in ten of the solver's payouts is native SOL rather than a token, so lamport
gains are read whenever no token moved. Both rules are 05's; what differs here is the wire.

Three things are read differently from a Geyser update than from a parsed block:

  * A memo arrives as raw instruction data rather than as a parsed string, so the order id
    is decoded out of the bytes.
  * `message.account_keys` stops at the accounts the transaction carries itself. Anything
    loaded from an address lookup table follows it in `meta`, and the balance arrays are
    indexed over the two together — see `keys_of`.
  * Balances are protobuf fields rather than JSON, and the amounts are strings either way.

The tally counts slots rather than blocks: this stream delivers one transaction at a time,
so the block 05 counts has no equivalent here.

This is not a FOMO feed and does not pretend to be one. The solver serves every application
and every origin chain Relay settles from, and nothing in a payout says which application
it belongs to. Attribution needs the deposit behind the order id, which lives on the other
chain, so 00_listen_fomo.py is where a payout becomes a named FOMO sell.

Emits FILL, defined in shared/feed.py: money leaving Relay, with the recipient and the
order id that says which sell it settles. `Trade.app` is left empty because this chain
cannot answer it.

Solver addresses rotate. When this one is replaced the stream goes quiet rather than wrong,
so a run that reports no payouts at all is a reason to re-check the address rather than to
conclude that nobody sold; it is in maintenance/registry.json under `solana.settlement`,
and maintenance/verify_registry.py checks it.

Needs a Geyser endpoint, a paid add-on at most providers. For a standard WebSocket instead,
see 05_listen_relay_payouts_blocks.py; both read the same solver by the same rules.

Reference: docs/04-listening.md (Solana, Joining the two legs), docs/05-relay.md (Solvers).

    uv run scripts/06_listen_relay_payouts_grpc.py [seconds] [wallet ...]     default 120
"""

import asyncio
import time

import base58
import grpc
from geyser.generated import geyser_pb2_grpc
from shared import cli, env, geyser
from shared.feed import Trade
from shared.solana import MEMO_PROGRAMS, SETTLEMENT_SOLVER, SOL_DUST, mint_name

ENDPOINT = env.optional("GEYSER_ENDPOINT")


def keys_of(message, meta):
    """Every account the transaction touched, in the order the balance arrays index them.

    `message.account_keys` holds only what the transaction carries; whatever it loaded
    from an address lookup table follows, writable first. `meta.pre_balances` is indexed
    over both together, so reading lamports against the short list credits a payout to
    whichever account happens to sit at that index — which is how a native-SOL payout ends
    up attributed to the wrong wallet. `05` never meets this because a parsed block hands
    over the accounts already joined.
    """
    loaded = list(meta.loaded_writable_addresses) + list(meta.loaded_readonly_addresses)
    return [base58.b58encode(key).decode() for key in list(message.account_keys) + loaded]


def order_of(message, keys):
    """The Relay order id a payout carries, out of the memo that holds one.

    The memo is raw instruction data here rather than a parsed string, so it is decoded
    before it can be recognised. The other memo a payout carries is a nonce beginning
    with the unix time, so shape is the test, as it is in `05`.
    """
    for instruction in message.instructions:
        if instruction.program_id_index >= len(keys):
            continue
        if keys[instruction.program_id_index] not in MEMO_PROGRAMS:
            continue
        note = bytes(instruction.data).decode("utf-8", "ignore")
        if note.startswith("0x") and len(note) == 66:
            return note.lower()
    return ""


def credited(meta, keys, solver):
    """(recipient, amount, mint) of the largest gain that is not the solver's own.

    Tokens first, then lamports. A payout can be native SOL — a system transfer carrying
    the order id in a memo, with no token account anywhere in the transaction — and
    reading only token balances drops those without noticing.
    """

    def held(balances):
        totals = {}
        for balance in balances:
            if balance.owner and balance.owner != solver:
                key = (balance.owner, balance.mint)
                totals[key] = totals.get(key, 0) + float(balance.ui_token_amount.ui_amount_string or 0)
        return totals

    before, after = held(meta.pre_token_balances), held(meta.post_token_balances)
    gains = [(after[k] - before.get(k, 0), k) for k in after if after[k] - before.get(k, 0) > 1e-12]
    if gains:
        amount, (owner, mint) = max(gains)
        return owner, amount, mint
    lamports = [
        ((post - pre) / 1e9, keys[i])
        for i, (pre, post) in enumerate(zip(meta.pre_balances, meta.post_balances, strict=False))
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
    env.require("GEYSER_ENDPOINT", "GEYSER_API_TOKEN")

    seen = set()  # the slots counted, so a slot delivering two payouts counts once
    async with grpc.aio.secure_channel(ENDPOINT, geyser.credentials()) as channel:
        stub = geyser_pb2_grpc.GeyserStub(channel)
        request = geyser.transaction_subscription(SETTLEMENT_SOLVER)

        async def requests():
            yield request
            while time.time() < deadline:
                await asyncio.sleep(1)

        # Read one update at a time rather than with `async for`, because the deadline has
        # to be honoured on a quiet minute too: nobody sells, no update arrives, and a loop
        # that only checks the clock on arrival never returns.
        stream = stub.Subscribe(requests()).__aiter__()
        while (left := deadline - time.time()) > 0:
            try:
                update = await asyncio.wait_for(anext(stream), timeout=left)
            except TimeoutError:
                break  # the deadline, which is the only thing that ends a quiet run
            except StopAsyncIteration:
                # The server ended the stream while there was still time on the clock: an
                # idle drop, a plan limit, a restart. Returning here would summarise the
                # rest of the run as a solver with nothing to say.
                raise ConnectionError("the Geyser stream ended before the deadline") from None
            if not update.HasField("transaction"):
                continue
            info = update.transaction.transaction
            message, meta = info.transaction.message, info.meta
            keys = keys_of(message, meta)
            if SETTLEMENT_SOLVER not in keys[: message.header.num_required_signatures]:
                # Mentions the solver without being signed by it: somebody paying it,
                # rather than it paying out.
                continue
            if update.transaction.slot not in seen:
                seen.add(update.transaction.slot)
                counts["slots carrying a solver transaction"] += 1
            order = order_of(message, keys)
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
            if not info.signature:
                counts["payouts with no signature to name them by"] += 1
                continue
            # Counted as payouts rather than as FILL rows: 00_listen_fomo.py prints only
            # the ones it can name, so a FILL tally there would overstate them.
            counts["payouts"] += 1
            signature = base58.b58encode(info.signature).decode()
            got = f"{amount:.6f} {mint_name(mint)}"
            emit(Trade("FILL", "", got, recipient, "", "", f"slot {update.transaction.slot}", signature, order))


if __name__ == "__main__":
    cli.run(
        "payouts-grpc",
        listen,
        "every application Relay settles on Solana, not FOMO's alone",
    )
