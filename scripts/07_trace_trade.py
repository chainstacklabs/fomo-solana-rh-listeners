"""Follow one FOMO trade across both chains, using nothing but the two chains.

A cross-chain trade is two transactions that share nothing an explorer will join for you.
The link is a 32-byte order id written on both sides, so the join needs no third party.
Relay's REST API can also resolve an order id, and this script used to depend on it, but
that API is throttled, retires on 2026-11-24, and its replacement wants a key.

The two directions are not shaped alike, and the id sits somewhere different in each:

  buy    Solana pays, Robinhood Chain fills. The Relay deposit instruction carries the id
         after the amount; the solver's fill ends its router calldata with it.
  sell   Robinhood Chain executes, Solana pays out. The depository log carries the id in
         its last word; the solver's transfer carries it in an spl-memo.

A sell's payout is signed by a Relay solver and not by FOMO's co-signer, so no listener in
this repository sees it — the co-signer subscription never receives that transaction. This
script finds it by reading the solver's own recent transactions.

Takes any one of a Robinhood Chain transaction (0x, 66 chars — a solver's fill or a deposit
in either asset shape), a Solana signature (base58, either a Relay deposit or a settlement),
or an order id (0x, 66 chars, from either side), and searches for the counterpart in both
directions. Searching is bounded by the minutes argument, because without an index the only
way to find a transaction by its contents is to read a window of them.

That window is read as ranges rather than the per-block `blockHash` queries the listeners
use: this is a scan of thousands of blocks, and one request per block would be thousands of
round trips. A load-balanced endpoint can answer a range short when its end is past the
answering backend's head (docs/04-listening.md, Transports), so a miss re-reads the head and
sweeps the tail again before reporting nothing.

Reference: docs/04-listening.md (Joining the two legs), docs/05-relay.md (Following one order).

A `handleOps` bundle carries several users, so a transaction holding more than one deposit
names a transaction and not a trade: the first is followed and the count is printed.

    uv run scripts/07_trace_trade.py <tx hash | signature | order id> [minutes]     default 5
"""

import sys
import time

import base58
import requests
from shared import env
from shared.robinhood import (
    DECIMALS,
    DELEGATION,
    DEPOSIT,
    DEPOSITORY,
    EXECUTOR_TOPIC,
    NATIVE_DEPOSIT,
    ROUTER,
    SYMBOL,
    TRANSFER,
    decode_string,
    topic,
    word,
)
from shared.solana import DEPOSIT_DISCRIMINATOR, MEMO_PROGRAMS, RELAY_DEPOSIT, SETTLEMENT_SOLVER, mint_name
from web3 import Web3

RH = env.endpoint("ROBINHOOD_MAINNET_HTTPS")
SOLANA = env.endpoint("SOLANA_MAINNET_HTTPS")
w3 = Web3(Web3.HTTPProvider(RH))

BLOCK_SECONDS = 0.101
SWEEP = 300  # blocks per eth_getLogs range
# A deposit comes in two shapes and they are the same trade. The ERC-20 one names the
# token and puts the order id at word 3; the ether one names no token and puts it one
# word earlier. Reading only the first loses every buy that was paid for in ether.
ORDER_WORD = {DEPOSIT: 3, NATIVE_DEPOSIT: 2}

if len(sys.argv) < 2:
    sys.exit(__doc__)
target = sys.argv[1]
minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 5
# Two of the busiest addresses on Solana are paged here. `blockTime` can come back null,
# and the cutoff is the only thing that ends the walk — without a cap, one null entry
# turns a five-minute lookback into the whole history of the address, on a metered node.
PAGES = 20


def batch(url, calls):
    """One JSON-RPC batch. `calls` are (method, params); replies come back in order."""
    out = []
    for start in range(0, len(calls), 100):
        chunk = calls[start : start + 100]
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
        replies = requests.post(url, json=body, timeout=120).json()
        by_id = {r["id"]: r.get("result") for r in replies}
        out += [by_id.get(i) for i in range(len(chunk))]
    return out


def token(address):
    try:
        raw = batch(
            RH,
            [
                ("eth_call", [{"to": address, "data": SYMBOL}, "latest"]),
                ("eth_call", [{"to": address, "data": DECIMALS}, "latest"]),
            ],
        )
        return decode_string(bytes.fromhex(raw[0][2:])), int(raw[1], 16)
    except Exception:
        # No invented scale: a guessed 18 prints a wrong number that looks right.
        return "?", None


def data_of(log):
    return log["data"].hex().removeprefix("0x")


def order_from_robinhood(tx_hash):
    """(order id, what the transaction is, how many deposits it holds) for one hash."""
    transaction = w3.eth.get_transaction(tx_hash)
    calldata = transaction["input"].hex().removeprefix("0x")
    if (transaction["to"] or "").lower() == ROUTER and len(calldata) >= 64:
        return "0x" + calldata[-64:], "a solver's fill", 1
    ids = []
    for log in w3.eth.get_transaction_receipt(tx_hash)["logs"]:
        if not log["topics"] or log["address"].lower() != DEPOSITORY.lower():
            continue
        position = ORDER_WORD.get(topic(log))
        if position is not None:
            ids.append("0x" + word(data_of(log), position))
    if not ids:
        return "", "", 0
    # A bundle carries several users, so a hash names a transaction and not a trade.
    return ids[0], "a deposit on this chain", len(ids)


def order_from_solana(signature):
    """The order id a Solana Relay deposit carries, straight from the instruction."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    tx = requests.post(SOLANA, json=body, timeout=60).json().get("result")
    if not tx:
        return "", None
    for instruction in tx["transaction"]["message"]["instructions"]:
        if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
            continue
        raw = base58.b58decode(instruction["data"])
        if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
            return "0x" + raw[16:48].hex(), tx
    return "", tx


def recent_signatures(address, noun):
    """One address's recent successful signatures, newest first, and how far back it got.

    Without an index the only way to find a transaction by its contents is to read a
    window of them, and `PAGES` caps how far back that window can reach. Whether the
    walk stopped at the cutoff or ran out of pages is the difference between "not in the
    last few minutes" and "not in the pages that fit", and only the first is answered by
    widening the window, so which one happened is printed above the search it describes.
    """
    seen, before, reached = [], None, False
    cutoff = time.time() - minutes * 60
    for _ in range(PAGES):
        page = (
            requests.post(
                SOLANA,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, {"limit": 1000, "before": before}],
                },
                timeout=90,
            )
            .json()
            .get("result")
            or []
        )
        if not page or page[-1]["signature"] == before:
            reached = True  # the address has no older history left to read
            break
        seen += [s for s in page if not s["err"]]
        before = page[-1]["signature"]
        if page[-1].get("blockTime") and page[-1]["blockTime"] < cutoff:
            reached = True
            break
    if reached:
        print(f"  searching {len(seen)} Solana {noun} from the last {minutes} min")
    else:
        print(f"  searching {len(seen)} Solana {noun}, as far back as {PAGES} pages reach")
        print(f"    {noun.capitalize()} on this address are dense enough that this is short of {minutes} min.")
    return seen


def find_solana_leg(order_id):
    """Search recent Relay deposits on Solana for the payment carrying this order id."""
    seen = recent_signatures(RELAY_DEPOSIT, "deposits")
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    for start in range(0, len(seen), 100):
        chunk = seen[start : start + 100]
        fetched = batch(SOLANA, [("getTransaction", [s["signature"], options]) for s in chunk])
        for signature, tx in zip(chunk, fetched, strict=True):
            if not tx:
                continue
            for instruction in tx["transaction"]["message"]["instructions"]:
                if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
                    continue
                raw = base58.b58decode(instruction["data"])
                if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR and "0x" + raw[16:48].hex() == order_id:
                    return signature["signature"], tx, int.from_bytes(raw[8:16], "little")
    return "", None, 0


def memo_ids(tx):
    """Every 32-byte order id a transaction carries in a memo."""
    found = []
    for instruction in tx["transaction"]["message"]["instructions"]:
        if instruction.get("programId") not in MEMO_PROGRAMS:
            continue
        note = instruction.get("parsed")
        if isinstance(note, str) and note.startswith("0x") and len(note) == 66:
            found.append(note.lower())
    return found


def find_solana_settlement(order_id):
    """Search the solver's recent payouts for the one whose memo is this order id."""
    seen = recent_signatures(SETTLEMENT_SOLVER, "payouts")
    options = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    for start in range(0, len(seen), 100):
        chunk = seen[start : start + 100]
        fetched = batch(SOLANA, [("getTransaction", [s["signature"], options]) for s in chunk])
        for signature, tx in zip(chunk, fetched, strict=True):
            if tx and order_id.lower() in memo_ids(tx):
                return signature["signature"], tx
    return "", None


def find_robinhood_deposit(order_id):
    """Search recent depository logs for the deposit carrying this order id."""
    head = w3.eth.block_number
    low = head - int(minutes * 60 / BLOCK_SECONDS)
    print(f"  searching Robinhood Chain deposits over blocks {low}-{head}")
    for start in range(head, low, -SWEEP):
        criteria = {
            "fromBlock": max(start - SWEEP + 1, low),
            "toBlock": start,
            "address": Web3.to_checksum_address(DEPOSITORY),
            "topics": [[DEPOSIT, NATIVE_DEPOSIT]],
        }
        for log in w3.eth.get_logs(criteria):
            data = log["data"].hex().removeprefix("0x")
            if "0x" + word(data, ORDER_WORD[topic(log)]) == order_id:
                h = log["transactionHash"].hex()
                return h if h.startswith("0x") else "0x" + h
    return ""


def sweep_fills(order_id, low, high):
    """The fill in blocks low..high whose router call ends with this order id, or ""."""
    hashes = []
    for start in range(low, high + 1, SWEEP):
        criteria = {"fromBlock": start, "toBlock": min(start + SWEEP - 1, high), "topics": [TRANSFER, EXECUTOR_TOPIC]}
        for log in w3.eth.get_logs(criteria):
            h = log["transactionHash"].hex()
            hashes.append(h if h.startswith("0x") else "0x" + h)
    hashes = list(dict.fromkeys(hashes))
    print(f"  searching {len(hashes)} Robinhood Chain fills over blocks {low}-{high}")
    for start in range(0, len(hashes), 100):
        chunk = hashes[start : start + 100]
        for h, info in zip(chunk, batch(RH, [("eth_getTransactionByHash", [t]) for t in chunk]), strict=True):
            if not info or (info.get("to") or "").lower() != ROUTER:
                continue
            calldata = info["input"][2:]
            if len(calldata) >= 64 and "0x" + calldata[-64:] == order_id:
                return h
    return ""


def find_robinhood_leg(order_id):
    """Search recent solver fills for the one whose call ends with this order id.

    A backend that had not reached the end of the range answers it short and says
    nothing, so a miss is re-read against a fresh head before it is believed.
    """
    head = w3.eth.block_number
    low = head - int(minutes * 60 / BLOCK_SECONDS)
    found = sweep_fills(order_id, low, head)
    if found:
        return found
    again = w3.eth.block_number
    if again > head:
        return sweep_fills(order_id, max(low, head - SWEEP), again)
    return ""


def show_solana(signature, tx, amount):
    print(f"\n  paid on Solana   {signature}")
    if not tx:
        return
    signers = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k.get("signer")]
    user = next((s for s in signers if s != signers[0]), signers[0]) if len(signers) > 1 else signers[0]
    mint = ""
    for balance in tx["meta"].get("preTokenBalances", []):
        if balance.get("owner") == user:
            mint = balance["mint"]
            break
    balances = tx["meta"].get("preTokenBalances", [])
    # The mint's own decimals, or none at all. Six is the common case and guessing
    # it prints a wrong number for every mint that is not.
    decimals = next((b["uiTokenAmount"]["decimals"] for b in balances if b["mint"] == mint), None)
    named = mint_name(mint) if mint else "?"
    shown = f"{amount / 10**decimals:.6f} {named}" if decimals is not None else f"? {amount} raw, {named}"
    print(f"    payer          {user}")
    print(f"    amount         {shown}")


def show_robinhood(tx_hash):
    print(f"\n  filled on Robinhood Chain   {tx_hash}")
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    executor = EXECUTOR_TOPIC[-40:]
    for log in receipt["logs"]:
        if not log["topics"] or topic(log) != TRANSFER:
            continue
        if topic(log, 1)[-40:].lower() != executor:
            continue
        recipient = Web3.to_checksum_address("0x" + topic(log, 2)[-40:])
        # The executor also moves tokens through pools on its way; only a delegated
        # wallet is somebody's delivery.
        code = "0x" + w3.eth.get_code(recipient).hex().removeprefix("0x")
        if not code.startswith(DELEGATION):
            continue
        symbol, decimals = token(Web3.to_checksum_address(log["address"]))
        raw = int(log["data"].hex().removeprefix("0x") or "0", 16)
        shown = f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw} raw, {symbol}"
        print(f"    delivered      {shown}  ({log['address']})")
        print(f"    to             {recipient}")
    print(f"    block          {receipt['blockNumber']}")


def show_settlement(signature, tx):
    print(f"\n  paid out on Solana   {signature}")
    solver = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]

    def held(side):
        return {(b.get("owner"), b["mint"]): float(b["uiTokenAmount"]["uiAmountString"]) for b in tx["meta"][side]}

    before, after = held("preTokenBalances"), held("postTokenBalances")
    gained = ((after[k] - before.get(k, 0), k) for k in after)
    credited = sorted(((d, k) for d, k in gained if d > 0), reverse=True)
    if credited:
        amount, (owner, mint) = credited[0]
        print(f"    delivered      {amount:.6f} {mint_name(mint)}")
        print(f"    to             {owner}")
    print(f"    solver         {solver}")
    print(f"    slot           {tx['slot']}")


def show_deposit(tx_hash, order_id):
    """One handleOps bundle carries several users, so only the matching deposit is this trade."""
    print(f"\n  sold on Robinhood Chain   {tx_hash}")
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    for log in receipt["logs"]:
        if not log["topics"] or log["address"].lower() != DEPOSITORY.lower() or topic(log) not in ORDER_WORD:
            continue
        data = log["data"].hex().removeprefix("0x")
        if "0x" + word(data, ORDER_WORD[topic(log)]) != order_id:
            continue
        depositor = Web3.to_checksum_address("0x" + word(data, 0)[24:])
        if topic(log) == DEPOSIT:
            symbol, decimals = token(Web3.to_checksum_address("0x" + word(data, 1)[24:]))
            raw = int(word(data, 2), 16)
        else:
            symbol, decimals, raw = "ETH", 18, int(word(data, 1), 16)
        shown = f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw} raw, {symbol}"
        print(f"    proceeds       {shown}")
        print(f"    from           {depositor}")
    print(f"    block          {receipt['blockNumber']}")


order_id, source = "", ""
solana_signature, solana_tx, solana_amount = "", None, 0
robinhood_tx = ""
settlement_signature, settlement_tx, robinhood_deposit = "", None, ""
direction = ""

if target.startswith("0x") and len(target) == 66:
    deposits = 0
    try:
        order_id, source, deposits = order_from_robinhood(target)
    except Exception:
        order_id = ""
    if order_id and source == "a deposit on this chain":
        # Proceeds going into Relay: a sell, whose other half is a payout on Solana.
        robinhood_deposit, direction = target, "sell"
        print(f"Robinhood Chain transaction {target}\n  it is {source}")
        if deposits > 1:
            print(f"  {deposits} deposits in this transaction, following the first")
    elif order_id:
        robinhood_tx, direction = target, "buy"
        print(f"Robinhood Chain transaction {target}\n  it is {source}")
    else:
        order_id, direction = target, ""
        print(f"order id {target}")
elif len(target) > 60:
    print(f"Solana signature {target}")
    order_id, solana_tx = order_from_solana(target)
    if order_id:
        solana_signature, direction = target, "buy"
        print("  it is a Relay deposit")
        for instruction in solana_tx["transaction"]["message"]["instructions"]:
            if instruction.get("programId") == RELAY_DEPOSIT and "data" in instruction:
                raw = base58.b58decode(instruction["data"])
                if len(raw) >= 48 and raw[:8] == DEPOSIT_DISCRIMINATOR:
                    solana_amount = int.from_bytes(raw[8:16], "little")
    elif solana_tx and memo_ids(solana_tx):
        # No Relay deposit, but a memo carrying an order id: this is the payout half.
        order_id = memo_ids(solana_tx)[0]
        settlement_signature, settlement_tx, direction = target, solana_tx, "sell"
        print("  it is a solver's payout")
    else:
        sys.exit("  no Relay deposit and no order id memo — this is not a leg of a cross-chain trade")
else:
    sys.exit("give a Robinhood Chain transaction, a Solana signature, or an order id")

if order_id != target:
    print(f"  order id {order_id}")

# A bare order id says nothing about which way the trade ran, so both shapes are tried.
if direction in ("", "buy"):
    if not solana_signature:
        solana_signature, solana_tx, solana_amount = find_solana_leg(order_id)
    if not robinhood_tx:
        robinhood_tx = find_robinhood_leg(order_id)
    if solana_signature or robinhood_tx:
        direction = "buy"

if direction in ("", "sell"):
    if not robinhood_deposit:
        robinhood_deposit = find_robinhood_deposit(order_id)
    if not settlement_signature:
        settlement_signature, settlement_tx = find_solana_settlement(order_id)
    if robinhood_deposit or settlement_signature:
        direction = "sell"

if direction == "sell":
    if robinhood_deposit:
        show_deposit(robinhood_deposit, order_id)
    else:
        print(f"\n  sold on Robinhood Chain   not found in the last {minutes} min — widen the window")
    if settlement_signature:
        show_settlement(settlement_signature, settlement_tx)
    else:
        print(f"\n  paid out on Solana   not found in the last {minutes} min — not settled yet, or widen the window")
elif direction == "buy":
    if solana_signature:
        show_solana(solana_signature, solana_tx, solana_amount)
    else:
        print(f"\n  paid on Solana   not found in the last {minutes} min — widen the window")
    if robinhood_tx:
        show_robinhood(robinhood_tx)
    else:
        print(f"\n  filled on Robinhood Chain   not found in the last {minutes} min — not filled yet")
else:
    print(f"\n  neither leg found in the last {minutes} min — widen the window")
