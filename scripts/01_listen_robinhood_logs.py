"""Listen to FOMO trades on Robinhood Chain by subscribing to logs directly.

Six `eth_subscribe` filters run on one socket and every event is pushed the moment its
block is published; nothing is polled and no block is fetched. The filters are separate
subscriptions the node does not order against each other, so a deposit can arrive before
the transfer that makes it a sell. Fills and deposits are therefore held until the first
log of a later block arrives on any filter, which makes the classification exact.

Emits BUY, SELL and PAY, defined in shared/feed.py. Here a buy is a token arriving from
Relay's executor into a wallet carrying FOMO's delegation, and a sell is the token moving
into the executor — recognised by that move rather than by a `Swap` log, because two venues
on this chain emit their own shapes and a sell through them produces no `Swap` at all. A
pay is money into the depository with no token leaving, in either the ERC-20 or the ether
shape.

A buy also carries the Relay order id that paid for it: the fill is a call to Relay's
router whose calldata ends with the 32-byte id from the deposit on the other chain, so
the whole cross-chain join comes off the chain itself.

Buys and sells are separated by what moved inside the depositor's own log-index span,
not by the transaction: one handleOps bundle carries several users, and one user can
have both a sell's proceeds and a separate buy payment in the same bundle.

FOMO is one tenant of Relay here. A wallet's EIP-7702 delegation names the implementation
it runs, which in practice names the application, so it is both the test that keeps other
applications out of this feed and the value reported as `Trade.app`.

For the same flow grouped per block, see 02_listen_robinhood_blocks.py. `listen()` here is
the one implementation of this transport; 00_listen_fomo.py imports it.

Reference: docs/04-listening.md (Robinhood Chain).

    uv run scripts/01_listen_robinhood_logs.py [seconds] [wallet ...]     default 120, FOMO only
"""

import asyncio
import json
import time
from collections import defaultdict

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
    quantity,
    span,
    trade_key,
    venues,
    where_of,
)
from web3 import Web3

WEBSOCKET = env.optional("ROBINHOOD_MAINNET_WSS")
HTTP = env.optional("ROBINHOOD_MAINNET_HTTPS")

LOOKUP_TIMEOUT = aiohttp.ClientTimeout(total=3)
# A busy log subscription sends frames past the 1 MiB default, which closes the
# connection with a 1009 instead of delivering the message.
MAX_FRAME_BYTES = 32 * 1024 * 1024


# --------------------------------------------------------------------- asking the node
# One call each, over the HTTP endpoint rather than the socket, because the socket is
# busy delivering. Each takes the cache it should remember its answer in, so a second
# event for the same token, wallet or transaction costs nothing and so that none of
# them holds state of its own.


async def rpc(http, method, params):
    """One JSON-RPC call; a stalled request is retried once. None on an RPC error."""
    for attempt in range(2):
        try:
            async with http.post(HTTP, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                return (await r.json()).get("result")
        except TimeoutError:
            if attempt:
                raise


async def token_meta(http, cache, counts, address):
    """(symbol, decimals) for an ERC-20, or ("?", None) when the token will not say.

    A token that reverts on `symbol()` is remembered as "?"; a lookup that timed out is
    not, so the next event for that token tries again. Nothing is substituted for a
    scale that could not be read — `quantity` prints the raw integer instead.
    """
    key = Web3.to_checksum_address(address)
    if key not in cache:
        try:
            symbol, decimals = await asyncio.gather(
                rpc(http, "eth_call", [{"to": key, "data": SYMBOL}, "latest"]),
                rpc(http, "eth_call", [{"to": key, "data": DECIMALS}, "latest"]),
            )
        except Exception:
            counts["lookups failed"] += 1
            return "?", None
        if symbol is None or decimals is None:
            counts["lookups failed"] += 1
            cache[key] = ("?", None)
        else:
            cache[key] = (decode_string(bytes.fromhex(symbol[2:])), int(decimals, 16))
    return cache[key]


async def app_of(http, cache, counts, address, block):
    """Which application a wallet belongs to, read at the log's own block.

    Never at "latest": the endpoint is served by several backends whose heads differ, and
    one that is behind would answer with no code for a wallet delegated in that very
    block. Such a backend errors on a block it lacks, which is retried, rather than
    caching a wrong answer. An address with no code yet may be delegated in a later
    block, so that answer is never remembered.
    """
    key = Web3.to_checksum_address(address)
    if key not in cache:
        code = None
        for _ in range(3):
            try:
                code = await rpc(http, "eth_getCode", [key, block])
            except Exception:
                break
            if code is not None:
                break
            await asyncio.sleep(0.1)
        if code is None:
            counts["lookups failed"] += 1
            return "?"
        if code == "0x":
            return "eoa"
        cache[key] = app_name(code)
    return cache[key]


async def order_of(http, cache, tx):
    """The Relay order id the fill's router call carries, which is the cross-chain join."""
    if tx not in cache:
        try:
            info = await rpc(http, "eth_getTransactionByHash", [tx])
        except Exception:
            return ""
        cache[tx] = order_from_calldata(info)
    return cache[tx]


# ------------------------------------------------------------------- reading one log
# No network and no state: each answers from what it is given. The rest of the decoding
# is in shared/robinhood.py, which every Robinhood Chain script reads logs through.


def wanted(watched, address):
    """Is this wallet in the filter? An empty filter watches every wallet."""
    return not watched or address.lower() in watched


def is_fomo(app):
    """FOMO's delegation is what keeps Relay's other tenants out of this feed.

    A fill delivers to somebody's wallet, and a wallet is an EOA with or without a
    delegation; the applications settling through these same contracts are not FOMO's.
    """
    return app == "fomo"


def sold_in_span(sold, user, start, end):
    """(token, raw amount) this user moved out inside their own operation, or None.

    What makes a deposit a sell rather than a payment is a token leaving the same wallet
    between the operation boundaries around it. A `handleOps` bundle carries several
    users, so scoping to the transaction would credit one person's sale to another's.
    """
    for index, seller, address, moved in sold:
        if start < index < end and seller.lower() == user.lower():
            return address, int(moved, 16)
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
    # Per transaction, and each entry carries its log index: a deposit is scoped to
    # the operation it sits in, never to the whole bundle.
    sold_in_tx, venue_in_tx, ops_in_tx = defaultdict(list), defaultdict(list), defaultdict(list)
    async with aiohttp.ClientSession(timeout=LOOKUP_TIMEOUT) as http:
        # The lookups above, bound to this run's session and caches, and the four steps
        # that turn one log into one Trade. Everything past them is the transport: what
        # to subscribe to, and when a log has enough of its transaction to be decoded.
        async def symbol_of(address):
            return await token_meta(http, meta, counts, address)

        async def owner_app(address, block):
            return await app_of(http, codes, counts, address, block)

        async def buyer_of(log):
            """The wallet a fill delivered to, or None when it is not FOMO's to report."""
            buyer = address_in_topic(log, 2)
            if buyer == BURN or not wanted(watched, buyer):
                return None, ""
            app = await owner_app(buyer, log["blockNumber"])
            return (buyer, app) if is_fomo(app) else (None, "")

        async def depositor_of(log):
            """The wallet a deposit came from, or None when it is not FOMO's to report."""
            user = address_in_word(log, 0)
            if not wanted(watched, user):
                return None, ""
            app = await owner_app(user, log["blockNumber"])
            return (user, app) if is_fomo(app) else (None, "")

        async def show_fill(log, tx):
            """A token arriving from Relay's executor into a FOMO wallet: a buy."""
            buyer, app = await buyer_of(log)
            if not buyer:
                return
            symbol, decimals = await symbol_of(log["address"])
            counts["BUY"] += 1
            emit(
                Trade(
                    kind="BUY",
                    got=quantity(int(log["data"], 16), decimals, symbol),
                    who=buyer,
                    app=app,
                    note=venues(venue_in_tx.get(tx, [])),
                    where=where_of(log),
                    key=trade_key(log),
                    order_id=await order_of(http, orders, tx),
                )
            )

        async def show_deposit(log, tx):
            """A deposit of an ERC-20: a sell's proceeds, or cash paying for a buy."""
            user, app = await depositor_of(log)
            if not user:
                return
            address, raw, order = erc20_deposit(log)
            symbol, decimals = await symbol_of(address)
            await settle(log, tx, user, app, quantity(raw, decimals, symbol), order)

        async def show_native_deposit(log, tx):
            """The same trade paid for in ether, where the deposit names no token at all."""
            user, app = await depositor_of(log)
            if not user:
                return
            raw, order = native_deposit(log)
            await settle(log, tx, user, app, quantity(raw, 18, "ETH"), order)

        async def settle(log, tx, user, app, paid, order):
            """A deposit is a sell when a token also left this wallet, a payment when not.

            The deposit names the order it pays for, so a buy on this chain is the other
            half of one of these seen from the paying side. Both the sold token and the
            venue are scoped to the depositor's own operation rather than to the bundle.
            """
            start, end = span(ops_in_tx.get(tx, []), int(log["logIndex"], 16))
            went_out = sold_in_span(sold_in_tx.get(tx, []), user, start, end)
            if went_out is None:
                counts["PAY"] += 1
                emit(Trade("PAY", paid, UNNAMED, user, app, "", where_of(log), trade_key(log), order))
                return
            address, raw = went_out
            symbol, decimals = await symbol_of(address)
            counts["SELL"] += 1
            venue = venues(venue_in_tx.get(tx, []), start, end)
            emit(
                Trade(
                    "SELL",
                    quantity(raw, decimals, symbol),
                    paid,
                    user,
                    app,
                    venue,
                    where_of(log),
                    trade_key(log),
                    order,
                )
            )

        filters = [
            ("userop", {"address": ENTRYPOINT, "topics": [USEROP]}),
            ("deposit", {"address": DEPOSITORY, "topics": [DEPOSIT]}),
            ("native", {"address": DEPOSITORY, "topics": [NATIVE_DEPOSIT]}),
            ("fill", {"topics": [TRANSFER, EXECUTOR_TOPIC]}),
            ("sold", {"topics": [TRANSFER, None, EXECUTOR_TOPIC]}),
            ("swap", {"topics": [list(VENUES)]}),
        ]

        async with websockets.connect(WEBSOCKET, max_size=MAX_FRAME_BYTES, close_timeout=1) as socket:
            pending, subscriptions = {}, {}
            for i, (name, criteria) in enumerate(filters, start=1):
                await socket.send(
                    json.dumps({"jsonrpc": "2.0", "id": i, "method": "eth_subscribe", "params": ["logs", criteria]})
                )
                pending[i] = name

            # The socket gets its own task so it is always drained; anything that costs an
            # RPC call runs as a bounded task rather than stalling the queue behind it.
            queue = asyncio.Queue()
            limit = asyncio.Semaphore(25)
            work = set()

            def later(decode, *args):
                async def bounded():
                    async with limit:
                        await decode(*args)

                task = asyncio.create_task(bounded())
                work.add(task)
                task.add_done_callback(work.discard)

            failure = None

            async def reader():
                nonlocal failure
                try:
                    async for raw in socket:
                        message = json.loads(raw)
                        if "id" in message and message["id"] in pending:
                            which = pending.pop(message["id"])
                            # A null result is a refusal too. Keying the filter under
                            # None would install it, match no delivery against it, and
                            # drop that filter's logs for the rest of the run.
                            if not message.get("result"):
                                raise RuntimeError(f"the node refused the {which} subscription: {message.get('error')}")
                            subscriptions[message["result"]] = which
                        elif message.get("method") == "eth_subscription":
                            queue.put_nowait(message["params"])
                    else:
                        # A socket closed politely ends this loop by returning rather than by
                        # raising, so without this the run drains an empty queue to the
                        # deadline and finishes with a summary that reads like a quiet chain.
                        failure = ConnectionError("the node closed the subscription")
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    # Both endings are fatal for the same reason: a subscription that stopped
                    # delivering has to be told apart from a chain with nothing to say.
                    failure = err

            # Fills and deposits wait here until a later block shows up on any filter, or
            # for half a second on a quiet chain, so the rest of their transaction is known.
            held, newest = [], 0
            hold_seconds = 0.5

            def release(everything=False):
                nonlocal held
                now = time.time()
                keep = []
                for block, decode, log, tx, arrived in held:
                    if everything or block < newest or now - arrived > hold_seconds:
                        later(decode, log, tx)
                    else:
                        keep.append((block, decode, log, tx, arrived))
                held = keep

            pump = asyncio.create_task(reader())
            try:
                while time.time() < deadline:
                    if failure is not None:
                        raise failure
                    try:
                        params = await asyncio.wait_for(queue.get(), timeout=0.2)
                    except TimeoutError:
                        release()
                        continue
                    kind = subscriptions.get(params["subscription"])
                    log = params["result"]
                    tx = log["transactionHash"]
                    newest = max(newest, int(log["blockNumber"], 16))

                    if kind == "userop":
                        counts["user operations"] += 1
                        # The boundary between two users' logs in one bundle. Recorded, not
                        # just counted, because a deposit is classified by what moved inside
                        # the depositor's own span.
                        ops_in_tx[tx].append(int(log["logIndex"], 16))
                    elif kind == "swap":
                        venue_in_tx[tx].append((int(log["logIndex"], 16), VENUES[log["topics"][0]]))
                    elif kind == "sold":
                        if log["address"].lower() != CASH:
                            sold_in_tx[tx].append(
                                (int(log["logIndex"], 16), "0x" + log["topics"][1][-40:], log["address"], log["data"])
                            )
                    elif kind == "fill":
                        held.append((int(log["blockNumber"], 16), show_fill, log, tx, time.time()))
                    elif kind == "deposit":
                        held.append((int(log["blockNumber"], 16), show_deposit, log, tx, time.time()))
                    elif kind == "native":
                        held.append((int(log["blockNumber"], 16), show_native_deposit, log, tx, time.time()))
                    release()

                    if len(sold_in_tx) > 20000:
                        sold_in_tx.clear()
                        venue_in_tx.clear()
                        ops_in_tx.clear()
                        orders.clear()

                release(everything=True)
            finally:
                pump.cancel()
                # Whatever is still held or mid-lookup at the deadline is counted, not
                # drained. A large number here means the decoder could not keep up with
                # the feed. A transport failure arrives here too, so the socket and the
                # HTTP session are never closed underneath a task still using them.
                left = queue.qsize() + len(held) + len(work)
                if left:
                    counts["still in flight at the deadline"] += left
                for task in list(work):
                    task.cancel()
                await asyncio.gather(pump, *work, return_exceptions=True)


if __name__ == "__main__":
    cli.run("rh-logs", listen)
