"""Listen to FOMO trades on Robinhood Chain block by block, behind a newHeads subscription.

New heads arrive over the socket and each sweep then fetches the logs of every block since
the last one, so this listener groups a block's activity together and the same code can be
pointed at history — at the cost of a few round trips per sweep. For a push-only stream
that fetches nothing, see 01_listen_robinhood_logs.py. A confirmed log is the first sight
of a trade either way.

Emits BUY, SELL and PAY, defined in shared/feed.py. Here a buy is a Transfer out of Relay's
executor into an address carrying FOMO's delegation, and a sell is the user's own operation
moving a token into the executor — not classified by the presence of a `Swap` log, because
two venues on this chain emit their own shapes and a sell through them has no `Swap` at
all. A pay is money into the depository with no token leaving, and never names the token.

Buys and sells are separated by what moved inside the user's own log-index span, not by
the transaction: one handleOps bundle carries several users, so a hash join credits one
person's swap to another person's deposit. A buy also carries the Relay order id that paid
for it, in the last word of the fill's calldata.

The HTTPS endpoint is served by several backends whose heads differ by a few blocks, and
one that has not reached the end of a range answers with silently fewer logs. Logs are
therefore asked for block by block, by the hash the head subscription delivered, and
lookups are pinned to the block: a backend that lacks it refuses instead of answering
short, and the sweep is retried.

A wallet's EIP-7702 delegation names the application behind it and is reported as
`Trade.app`; FOMO is one tenant of Relay here, so that delegation is also the test that
keeps the other tenants out. `listen()` here is the one implementation of this transport;
00_listen_fomo.py imports it.

Reference: docs/04-listening.md (Robinhood Chain).

    uv run scripts/02_listen_robinhood_blocks.py [seconds] [wallet ...]     default 120, FOMO only
"""

import asyncio
import json
import time

import aiohttp
import websockets
from shared import cli, env
from shared.feed import UNNAMED, Trade
from shared.robinhood import (
    BURN,
    CASH,
    DECIMALS,
    DEPOSIT,
    DEPOSITORY,
    ENTRYPOINT,
    EXECUTOR_TOPIC,
    NATIVE_DEPOSIT,
    SYMBOL,
    TRANSFER,
    USEROP,
    VENUES,
    address_in_topic,
    address_in_word,
    app_name,
    decode_string,
    erc20_deposit,
    native_deposit,
    order_from_calldata,
    padded_topic,
    quantity,
    span,
    trade_key,
    venues,
    where_of,
)
from web3 import Web3

WEBSOCKET = env.optional("ROBINHOOD_MAINNET_WSS")  # push: new heads
HTTP = env.optional("ROBINHOOD_MAINNET_HTTPS")  # pull: logs and lookups

RPC_TIMEOUT = aiohttp.ClientTimeout(total=10)
BATCH = 50  # calls per request; a catch-up sweep asks for hundreds of things at once
BEHIND = ("unknown block", "header not found")  # how a backend refuses a block it lacks
MAX_SWEEP_BLOCKS = 300  # if the listener falls behind, jump forward rather than queue up
STALL = 30  # seconds without a head before the subscription is called dead


def result(reply):
    """One batched reply's result, or raise. A sweep is wrong if any call in it silently failed."""
    if "result" not in reply:
        raise RuntimeError(reply.get("error") or "no reply")
    return reply["result"]


# ------------------------------------------------------------------- reading a sweep
# No network and no state: each takes the logs a sweep fetched and answers from them.
# The rest of the decoding is in shared/robinhood.py.


def operation_boundaries(op_logs):
    """The log index of every `UserOperationEvent`, per transaction.

    A handleOps bundle is one flat list of logs delimited by these, so they are what
    scopes a deposit to the user it belongs to. `span` turns them into a range.
    """
    boundaries = {}
    for log in op_logs:
        boundaries.setdefault(log["transactionHash"], []).append(int(log["logIndex"], 16))
    return boundaries


def swaps_by_transaction(swap_logs):
    """(log index, venue) per transaction, so a venue can be scoped to one operation."""
    grouped = {}
    for log in swap_logs:
        grouped.setdefault(log["transactionHash"], []).append((int(log["logIndex"], 16), VENUES[log["topics"][0]]))
    return grouped


def sold_in_span(sent, user, tx, start, end):
    """(token, raw amount) this user moved into the executor inside their own operation.

    None when nothing left, which is what makes the deposit a payment rather than a
    sell. Cash leaving is skipped: that is the same money going the other way, not a
    sale of anything.
    """
    for moved in sent:
        if moved["transactionHash"] != tx:
            continue
        if not start < int(moved["logIndex"], 16) < end:
            continue
        if address_in_topic(moved, 1).lower() != user.lower():
            continue
        if moved["address"].lower() == CASH:
            continue
        return moved["address"], int(moved["data"][2:] or "0", 16)
    return None


async def listen(deadline, watched, emit, counts):
    """Decode this transport until `deadline`, calling `emit` with one Trade per trade.

    `watched` is a set of lowercased addresses, empty for every wallet. `Trade.key` is
    the transaction hash and the log index together, which is what identifies one trade
    across transports: the hash alone is not enough, because one handleOps bundle
    carries several users' trades.
    """
    env.require("ROBINHOOD_MAINNET_WSS", "ROBINHOOD_MAINNET_HTTPS")

    meta, codes, orders = {}, {}, {}
    async with aiohttp.ClientSession(timeout=RPC_TIMEOUT) as http:
        checksum = Web3.to_checksum_address
        hashes = {}  # block number -> hash, from the head subscription

        async def rpc(method, params):
            async with http.post(HTTP, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                return (await r.json()).get("result")

        async def batched(calls):
            """One reply per call, in order, or None when a backend refused a block it lacks."""

            async def part(chunk):
                body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
                async with http.post(HTTP, json=body) as r:
                    replies = {reply["id"]: reply for reply in await r.json()}
                return [replies.get(i, {}) for i in range(len(chunk))]

            parts = await asyncio.gather(*(part(calls[i : i + BATCH]) for i in range(0, len(calls), BATCH)))
            replies = [reply for p in parts for reply in p]
            if any(str((reply.get("error") or {}).get("message", "")).startswith(BEHIND) for reply in replies):
                return None
            return replies

        def wanted(address):
            return not watched or address.lower() in watched

        # A fill delivers to somebody's wallet, and a wallet is an EOA with or without a
        # delegation. FOMO's delegation is what keeps the other applications settling
        # through these same contracts out of the feed.
        def mine(app):
            return app == "fomo"

        # ---- one sweep, as four round trips and two decoding passes ----
        # Each returns None or False when a backend had not reached the range yet, which
        # means the same blocks are asked for again rather than reported short.

        async def block_hashes(blocks):
            """Fill in the hash of every block whose head the socket did not deliver.

            Heads that arrived while the socket was quiet have no hash yet, and a backend
            that does not have the block yet answers null for it.
            """
            unknown = [n for n in blocks if n not in hashes]
            if not unknown:
                return True
            replies = await batched([("eth_getBlockByNumber", [hex(n), False]) for n in unknown])
            if replies is None or any(result(r) is None for r in replies):
                return False
            for n, reply in zip(unknown, replies, strict=True):
                hashes[n] = reply["result"]["hash"]
            return True

        async def sweep_logs(blocks):
            """The five filters' logs for a range of blocks, asked for by block hash.

            By hash rather than by range: a load-balanced endpoint whose answering backend
            has not reached the end of a range answers with silently fewer logs, while one
            that lacks a block by hash refuses, and a refusal can be retried.
            """
            filters = (
                {"address": ENTRYPOINT, "topics": [USEROP]},
                {"address": DEPOSITORY, "topics": [DEPOSIT]},
                {"topics": [TRANSFER, EXECUTOR_TOPIC]},
                {"topics": [list(VENUES)]},
                {"address": DEPOSITORY, "topics": [NATIVE_DEPOSIT]},
            )
            calls = [("eth_getLogs", [{"blockHash": hashes[n], **f}]) for n in blocks for f in filters]
            replies = await batched(calls)
            if replies is None:
                return None
            # One filter's logs are every len(filters)'th reply, in the order asked for.
            return [[log for reply in replies[k :: len(filters)] for log in result(reply)] for k in range(len(filters))]

        async def resolve_wallets(payments, fill_logs, at):
            """(app per wallet, transfers out of every depositor), or None to retry.

            A deposit is a sell if a token also left the depositor inside the same user
            operation, and a payment if nothing did. Fetching a receipt per bundle to find
            out is far too slow at 0.1 s blocks; one filtered query for transfers out of
            every depositor answers it instead. The fills' order ids come along for the
            ride, since they are one call each on the same trip.
            """
            depositors = {address_in_word(log, 0) for log, _ in payments}
            buyers = {address_in_topic(log, 2) for log in fill_logs}
            unseen = sorted(w for w in buyers | depositors if w not in codes)
            fill_txs = sorted({log["transactionHash"] for log in fill_logs} - set(orders))
            calls = [("eth_getCode", [w, at]) for w in unseen]
            calls += [("eth_getTransactionByHash", [tx]) for tx in fill_txs]
            per_block = {}
            for log, _ in payments:
                per_block.setdefault(log["blockNumber"], set()).add(padded_topic(address_in_word(log, 0)))
            for number, senders in per_block.items():
                calls.append(
                    ("eth_getLogs", [{"blockHash": hashes[int(number, 16)], "topics": [TRANSFER, sorted(senders)]}])
                )

            replies = await batched(calls) if calls else []
            if replies is None:
                return None
            wallets_end = len(unseen)
            txs_end = wallets_end + len(fill_txs)
            # An address with no code yet may be delegated in a later block, so that
            # answer is never remembered; the rest is decided for this sweep.
            app = {}
            for wallet, reply in zip(unseen, replies[:wallets_end], strict=True):
                code = result(reply) or "0x"
                app[wallet] = app_name(code)
                if code != "0x":
                    codes[wallet] = app[wallet]
            app |= {w: codes[w] for w in buyers | depositors if w in codes}
            for tx, reply in zip(fill_txs, replies[wallets_end:txs_end], strict=True):
                orders[tx] = order_from_calldata(result(reply))
            sent = [log for reply in replies[txs_end:] for log in result(reply)]
            return app, sent

        async def resolve_tokens(payments, fill_logs, sent, at):
            """Cache the symbol and decimals of every token this sweep names. False to retry.

            A token that reverts on either is remembered as "?" and counted, never given a
            guessed scale: a wrong number that looks right is worse than a missing one.
            """
            addresses = {erc20_deposit(log)[0] for log, native in payments if not native}
            addresses |= {log["address"] for log in fill_logs} | {log["address"] for log in sent}
            tokens = sorted(a for a in addresses if checksum(a) not in meta)
            calls = [
                (m, p)
                for a in tokens
                for m, p in (
                    ("eth_call", [{"to": checksum(a), "data": SYMBOL}, at]),
                    ("eth_call", [{"to": checksum(a), "data": DECIMALS}, at]),
                )
            ]
            replies = await batched(calls) if calls else []
            if replies is None:
                return False
            for n, a in enumerate(tokens):
                symbol, decimals = replies[2 * n].get("result"), replies[2 * n + 1].get("result")
                if not symbol or not decimals:
                    counts["lookups failed"] += 1
                    meta[checksum(a)] = ("?", None)
                else:
                    meta[checksum(a)] = (decode_string(bytes.fromhex(symbol[2:])), int(decimals, 16))
            return True

        def emit_deposits(payments, app, sent, ops, swaps):
            """A deposit is a sell when a token also left the same wallet, a payment when not.

            The deposit names the order it pays for either way, which is what joins it to
            its other chain. Both the sold token and the venue are scoped to the depositor's
            own operation rather than to the bundle, which carries several users.
            """
            for log, native in payments:
                user = address_in_word(log, 0)
                if not wanted(user) or not mine(app[user]):
                    continue
                if native:
                    # Ether names no token, and its 18 decimals are a fact about the
                    # chain rather than a guess standing in for a failed lookup.
                    raw, order = native_deposit(log)
                    symbol, decimals = "ETH", 18
                else:
                    address, raw, order = erc20_deposit(log)
                    symbol, decimals = meta[checksum(address)]
                paid = quantity(raw, decimals, symbol)
                tx = log["transactionHash"]
                start, end = span(ops.get(tx, []), int(log["logIndex"], 16))

                went_out = sold_in_span(sent, user, tx, start, end)
                if went_out is None:
                    counts["PAY"] += 1
                    emit(Trade("PAY", paid, UNNAMED, user, app[user], "", where_of(log), trade_key(log), order))
                    continue
                out_address, out_raw = went_out
                symbol_out, decimals_out = meta[checksum(out_address)]
                counts["SELL"] += 1
                emit(
                    Trade(
                        "SELL",
                        quantity(out_raw, decimals_out, symbol_out),
                        paid,
                        user,
                        app[user],
                        venues(swaps.get(tx, []), start, end),
                        where_of(log),
                        trade_key(log),
                        order,
                    )
                )

        def emit_fills(fill_logs, app, swaps):
            """A token arriving from Relay's executor into a FOMO wallet: a buy.

            A fill is the solver's own transaction rather than a bundle, so every swap in
            it belongs to this delivery and there is no span to scope by.
            """
            for log in fill_logs:
                buyer = address_in_topic(log, 2)
                if buyer == BURN or not wanted(buyer) or not mine(app[buyer]):
                    continue
                symbol, decimals = meta[checksum(log["address"])]
                tx = log["transactionHash"]
                counts["BUY"] += 1
                emit(
                    Trade(
                        kind="BUY",
                        got=quantity(int(log["data"][2:] or "0", 16), decimals, symbol),
                        who=buyer,
                        app=app[buyer],
                        note=venues(swaps.get(tx, [])),
                        where=where_of(log),
                        key=trade_key(log),
                        order_id=orders.get(tx, ""),
                    )
                )

        async def handle(start_block, end_block):
            """One sweep. False when a backend was behind and the same range must be retried."""
            blocks = list(range(start_block, end_block + 1))
            at = hex(end_block)
            if not await block_hashes(blocks):
                return False
            logs = await sweep_logs(blocks)
            if logs is None:
                return False
            op_logs, deposit_logs, fill_logs, swap_logs, native_logs = logs
            counts["user operations"] += len(op_logs)

            # A payment in ether and a payment in a token are the same trade seen from the
            # paying side, so they go through one list; the flag says which shape to read
            # it with, because the ether one names no token and puts its fields earlier.
            payments = [(log, False) for log in deposit_logs] + [(log, True) for log in native_logs]
            ops, swaps = operation_boundaries(op_logs), swaps_by_transaction(swap_logs)

            resolved = await resolve_wallets(payments, fill_logs, at)
            if resolved is None:
                return False
            app, sent = resolved
            if not await resolve_tokens(payments, fill_logs, sent, at):
                return False

            emit_deposits(payments, app, sent, ops, swaps)
            emit_fills(fill_logs, app, swaps)
            return True

        async with websockets.connect(WEBSOCKET, close_timeout=1) as socket:
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]})
            )
            latest = last = int(await rpc("eth_blockNumber", []), 16)

            # Heads arrive one per block and queue in order, so reading them inside the
            # work loop falls a block further behind on every pass. A reader task drains
            # the queue and keeps only the newest; the loop then sweeps whole ranges.
            failure = None

            async def reader():
                nonlocal latest, failure
                try:
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("method") == "eth_subscription":
                            head = message["params"]["result"]
                            hashes[int(head["number"], 16)] = head["hash"]
                            latest = max(latest, int(head["number"], 16))
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    # A dead socket ends the run. Carrying on by polling would answer a
                    # different question than the one this script exists to answer, and
                    # would do it without the reader ever saying the subscription died.
                    failure = err

            pump = asyncio.create_task(reader())
            idle = 0.0
            while time.time() < deadline:
                if failure is not None:
                    raise failure
                if latest > last:
                    # Pin the target before the sweep. Reading `latest` again afterwards
                    # would skip every block that arrived while the sweep was running.
                    target = latest
                    if target - last > MAX_SWEEP_BLOCKS:
                        print(f"[behind by {target - last} blocks - skipping to the last {MAX_SWEEP_BLOCKS}]")
                        last = target - MAX_SWEEP_BLOCKS
                    try:
                        done = await handle(last + 1, target)
                    except Exception as err:
                        print(f"[sweep {last + 1}-{target} failed: {type(err).__name__}: {err} - retrying]")
                        await asyncio.sleep(0.2)
                        continue
                    if not done:  # a backend had not reached the target yet; ask again
                        counts["sweeps retried behind a lagging backend"] += 1
                        await asyncio.sleep(0.05)
                        continue
                    counts["blocks swept"] += target - last
                    last = target
                    idle = 0.0
                    for n in [n for n in hashes if n < last - 1000]:
                        del hashes[n]
                    if len(orders) > 20000:
                        orders.clear()
                else:
                    await asyncio.sleep(0.05)
                    idle += 0.05
                    # Nothing is fetched to cover for a subscription that has gone quiet.
                    # Blocks are a tenth of a second apart, so this long without one is a
                    # subscription that is not delivering, and that is worth stopping for.
                    if idle > STALL:
                        raise RuntimeError(f"no new head for {STALL} s — the subscription is not delivering")
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)


if __name__ == "__main__":
    cli.run("rh-blocks", listen)
