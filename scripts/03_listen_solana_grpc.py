"""Listen to FOMO trades on Solana over a Yellowstone gRPC (Geyser) subscription.

The stream pushes each matching transaction with its metadata attached, so nothing has to
be fetched afterwards. Every FOMO transaction on Solana is signed and paid for by one
co-signer account, which is what the subscription filters on: there is no code-based
wallet marker here, because a FOMO wallet is an ordinary keypair.

Emits SWAP, PAY, IN, OUT and OTHER, defined in shared/feed.py and decided by
shared/solana.py from what the wallet's balances did. FOMO routes swaps through more than
one program — DFlow, Jupiter and a third, unnamed router have all been seen — so a swap is
recognised by what moved rather than by the program called, and the router is reported.

Closing a wrapped-SOL account refunds its rent to the wallet, which shows up as a few
thousandths of a SOL next to the real trade; SOL movements that small are ignored whenever
anything else moved.

Needs a Geyser endpoint, a paid add-on at most providers. For a standard WebSocket
instead, see 04_listen_solana_blocks.py; both classify through the same rule. `listen()`
here is the one implementation of this transport, and 00_listen_fomo.py imports it.

Reference: docs/04-listening.md (Solana), docs/02-trade-lifecycle.md (Trades that stay on one chain).

    uv run scripts/03_listen_solana_grpc.py [seconds] [wallet ...]     default 120
"""

import asyncio
import time

import base58
import grpc
from geyser.generated import geyser_pb2_grpc
from shared import cli, env, geyser
from shared.feed import Trade
from shared.solana import CO_SIGNER, DEPOSIT_DISCRIMINATOR, RELAY_DEPOSIT, classify

ENDPOINT = env.optional("GEYSER_ENDPOINT")


async def listen(deadline, watched, emit, counts):
    """Decode this transport until `deadline`, calling `emit` with one Trade per trade.

    `watched` is a set of base58 wallet addresses, empty for every wallet. There is no
    application filter here as there is on Robinhood Chain: Solana has no code-based
    marker, so everything reached through FOMO's co-signer is FOMO's. `Trade.key` is
    the signature, which identifies one trade across transports.
    """
    env.require("GEYSER_ENDPOINT", "GEYSER_API_TOKEN")

    async with grpc.aio.secure_channel(ENDPOINT, geyser.credentials()) as channel:
        stub = geyser_pb2_grpc.GeyserStub(channel)
        request = geyser.transaction_subscription(CO_SIGNER)

        async def requests():
            yield request
            while time.time() < deadline:
                await asyncio.sleep(1)

        # The stream is read one update at a time rather than with `async for`, because
        # the deadline has to be honoured on a quiet minute too: FOMO makes no trade, no
        # update arrives, and a loop that only checks the clock on arrival never returns.
        stream = stub.Subscribe(requests()).__aiter__()
        while (left := deadline - time.time()) > 0:
            try:
                update = await asyncio.wait_for(anext(stream), timeout=left)
            except TimeoutError:
                break  # the deadline, which is the only thing that ends a quiet run
            except StopAsyncIteration:
                # The server ended the stream while there was still time on the clock:
                # an idle drop, a plan limit, a restart. Returning here would summarise
                # the rest of the run as a chain with nothing to say.
                raise ConnectionError("the Geyser stream ended before the deadline") from None
            if not update.HasField("transaction"):
                continue
            info = update.transaction.transaction
            message, meta = info.transaction.message, info.meta
            keys = [base58.b58encode(k).decode() for k in message.account_keys]
            signers = keys[: message.header.num_required_signatures]
            user = next((s for s in signers if s != CO_SIGNER), None)
            if not user or (watched and user not in watched):
                continue

            programs = {keys[i.program_id_index] for i in message.instructions if i.program_id_index < len(keys)}
            order = ""
            for instruction in message.instructions:
                if instruction.program_id_index >= len(keys):
                    continue
                if keys[instruction.program_id_index] != RELAY_DEPOSIT:
                    continue
                raw = bytes(instruction.data)
                if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
                    order = "0x" + raw[16:48].hex()
                    break

            def amounts(balances):
                return {(b.account_index, b.mint): float(b.ui_token_amount.ui_amount_string or 0) for b in balances}

            before, after = amounts(meta.pre_token_balances), amounts(meta.post_token_balances)
            every = list(meta.pre_token_balances) + list(meta.post_token_balances)
            owners = {(b.account_index, b.mint): b.owner for b in every}
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
                if i < len(meta.pre_balances) and i < len(meta.post_balances):
                    lamports = (meta.post_balances[i] - meta.pre_balances[i]) / 1e9
                    if abs(lamports) > 1e-9:
                        moved.append((lamports, "SOL"))

            kind, gave, got, note = classify(moved, programs, RELAY_DEPOSIT in programs)
            counts[kind] += 1
            signature = base58.b58encode(info.signature).decode()
            emit(Trade(kind, gave, got, user, "fomo", note, f"slot {update.transaction.slot}", signature, order))


if __name__ == "__main__":
    cli.run("sol-grpc", listen)
