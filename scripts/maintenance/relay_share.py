"""How much of Relay's deposit volume is FOMO's, and who else deposits into Relay.

Every other script here reads what FOMO does. This reads how large FOMO is against the
protocol it settles through, which needs a denominator FOMO's own traffic cannot supply:
every other application, on every other origin chain.

Three reads, and no Relay API:

  relay chain      Relay runs its own chain, and a deposit from any origin is attested
                   there as an ERC-6909 mint on the hub. That is the denominator, about
                   twenty origin chains in one pass.
  robinhood chain  the depository's two deposit events, with every depositor sorted by
                   the EIP-7702 code it carries. FOMO's delegation is the same marker the
                   listeners filter on, so this split is the one docs/04-listening.md
                   describes.
  solana           every deposit into Relay's program, with the amount and the mint read
                   out of the instruction and FOMO's co-signer read off the signer list.
                   Both halves come out of the same transactions, so the Solana split is
                   measured rather than inferred from a count.

Prices are the one thing no chain here can answer. Stablecoins are held at $1 and ETH,
SOL and BTC are quoted by CoinGecko — the only offchain source any script in this
repository reads. Everything else is counted and reported unpriced rather than guessed,
and a quote that fails is printed as a failure with its assets left unpriced, so a total
never silently shrinks. Every dollar figure below therefore rests on an offchain quote,
which is why none of them belongs in docs/ without that said out loud.

Addresses, topics and program ids come from maintenance/registry.json and shared/ rather
than from copies kept here, so verify_registry.py covers what this reads.

Reference: docs/05-relay.md, docs/04-listening.md (Telling FOMO apart from everything else).

    uv run scripts/maintenance/relay_share.py [minutes]      default 15
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import base58
from shared.env import endpoint
from shared.robinhood import DELEGATION, DEPOSIT, DEPOSITORY, FOMO_CODE, NATIVE_DEPOSIT
from shared.solana import CO_SIGNER, DEPOSIT_DISCRIMINATOR, RELAY_DEPOSIT, mint_name

REGISTRY = json.loads((Path(__file__).parent / "registry.json").read_text())
RELAY_RPC = REGISTRY["relayChain"]["rpc"]
HUB = REGISTRY["relayChain"]["contracts"]["hub"]["address"]
# The hub's ERC-6909 Transfer, found by where it is recorded rather than by its hash, so
# a topic corrected in the registry is corrected here too.
HUB_TRANSFER = next(topic for topic, event in REGISTRY["events"].items() if event.get("at") == "relayChain hub")
ZERO = "0x" + "00" * 32

# A stablecoin is worth a dollar. Everything else is priced or reported unpriced — never guessed.
STABLES = {
    "USDC",
    "USDG",
    "USDT",
    "DAI",
    "USDS",
    "USDE",
    "PYUSD",
    "FDUSD",
    "RLUSD",
    "USDC.e",
    "USDT0",
    "USDbC",
    "USDC (Perp)",
}
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,solana,bitcoin&vs_currencies=usd"
PRICE_IDS = {"ETH": "ethereum", "WETH": "ethereum", "SOL": "solana", "BTC": "bitcoin", "WBTC": "bitcoin"}


def rpc(url):
    """A JSON-RPC caller for one endpoint. Raises on transport failure, returns the error on a protocol one."""

    def call(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(url, method="POST", data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=90) as response:
            answer = json.load(response)
        return {"error": answer["error"]} if "error" in answer else answer["result"]

    return call


def prices():
    """USD per symbol, and the line saying where those prices came from.

    The one offchain read in this repository, and the reason every dollar figure here is
    a measurement against a quote rather than against a chain. A stablecoin is held at $1
    without asking anybody; ETH, SOL and BTC are quoted once. A quote that fails leaves
    those assets out of the table altogether, so their deposits are reported unpriced
    instead of folded in at an invented number — and the header says so, because a total
    that quietly lost a third of its assets looks like a quiet market.
    """
    table = {symbol: 1.0 for symbol in STABLES}
    try:
        with urllib.request.urlopen(COINGECKO, timeout=30) as response:
            quoted = json.load(response)
        for symbol, coin in PRICE_IDS.items():
            table[symbol] = float(quoted[coin]["usd"])
    except Exception as failure:
        return table, f"stablecoins at $1; ETH/SOL/BTC UNPRICED - coingecko did not answer ({type(failure).__name__})"
    quoted_line = ", ".join(f"{symbol} ${table[symbol]:,.0f}" for symbol in ("ETH", "SOL", "BTC") if symbol in table)
    return table, f"stablecoins at $1; {quoted_line}  (coingecko, offchain)"


def block_at(call, target, head):
    """The first block at or after `target`, by binary search on timestamps."""

    def stamp(number):
        return int(call("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)

    low, high = 1, head
    while low < high:
        middle = (low + high) // 2
        if stamp(middle) < target:
            low = middle + 1
        else:
            high = middle
    return low


def hub_logs(call, first, last):
    """Every Hub log over the range, paged with the cursor the Relay Chain node asks for."""
    out, cursor = [], None
    while True:
        query = {"address": HUB, "fromBlock": hex(first), "toBlock": hex(last)}
        if cursor:
            query["cursor"] = cursor
        answer = call("eth_getLogsWithCursor", [query])
        if isinstance(answer, dict) and "error" in answer:
            raise RuntimeError(f"relay chain refused a log page: {answer['error']}")
        page = answer.get("logs", [])
        out.extend(page)
        cursor = answer.get("cursor")
        if not page or not cursor:
            return out


def token_meta(call, token_id, cache):
    """`name`, `symbol` and `decimals` for one Hub token id, read off the Hub itself."""
    if token_id in cache:
        return cache[token_id]

    def read(selector, decode):
        try:
            raw = call("eth_call", [{"to": HUB, "data": selector + token_id[2:]}, "latest"])
            return decode(raw)
        except Exception:
            return None

    def as_text(raw):
        body = bytes.fromhex(raw[2:])
        length = int.from_bytes(body[32:64], "big")
        return body[64 : 64 + length].decode("utf8", "replace")

    # selectors for name(uint256), symbol(uint256), decimals(uint256)
    meta = (
        read("0x00ad800c", as_text),
        read("0x4e41a1fb", as_text),
        read("0x3f47e662", lambda raw: int(raw, 16)),
    )
    cache[token_id] = meta
    return meta


def usd(amount, symbol, decimals, table):
    """The dollar value, or None when the asset has no price or no decimals."""
    if decimals is None or symbol is None or symbol not in table:
        return None
    return amount / (10**decimals) * table[symbol]


def relay_totals(call, first, last, table):
    """Relay's global deposit, fill and withdrawal flow over the range, by origin chain and asset."""
    cache = {}
    minted, filled, burned = {}, 0.0, 0.0
    unpriced = 0

    for log in hub_logs(call, first, last):
        if not log["topics"] or log["topics"][0] != HUB_TRANSFER:
            continue
        sender, receiver, token_id = log["topics"][1], log["topics"][2], log["topics"][3]
        amount = int(log["data"][66:130], 16)
        name, symbol, decimals = token_meta(call, token_id, cache)
        # The hub names every token `<origin chain>-<SYMBOL>`. Price the asset, not the pair.
        origin, asset = symbol.split("-", 1) if symbol and "-" in symbol else ("?", symbol or "?")
        value = usd(amount, asset, decimals, table)

        if sender == ZERO:
            row = minted.setdefault((origin, asset), {"deposits": 0, "usd": 0.0, "unpriced": 0, "name": name})
            row["deposits"] += 1
            if value is None:
                row["unpriced"] += 1
                unpriced += 1
            else:
                row["usd"] += value
        elif receiver == ZERO:
            burned += value or 0.0
        else:
            filled += value or 0.0

    return minted, filled, burned, unpriced


def code_of(url, addresses):
    """`eth_getCode` for many addresses, batched so a few thousand depositors cost tens of round trips."""
    out = {}
    listed = list(addresses)
    for start in range(0, len(listed), 100):
        chunk = listed[start : start + 100]
        body = json.dumps(
            [
                {"jsonrpc": "2.0", "id": index, "method": "eth_getCode", "params": [address, "latest"]}
                for index, address in enumerate(chunk)
            ]
        ).encode()
        request = urllib.request.Request(url, method="POST", data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                for answer in json.load(response):
                    out[chunk[answer["id"]]] = answer.get("result", "?")
        except Exception:
            for address in chunk:
                out[address] = "?"
    return out


def classify(code):
    """Which population a depositor belongs to, read from its code alone."""
    if code == "?":
        return "unreadable"
    if code == FOMO_CODE:
        return "FOMO"
    if code.startswith(DELEGATION):
        return "other app (7702)"
    if code == "0x":
        return "individual (plain EOA)"
    return "contract"


def robinhood_deposits(url, call, first, last, table):
    """Every deposit into Relay's depository on Robinhood Chain over the range, grouped by depositor population."""
    logs = []
    for start in range(first, last + 1, 5000):
        stop = min(start + 4999, last)
        answer = call(
            "eth_getLogs",
            [
                {
                    "address": DEPOSITORY,
                    "fromBlock": hex(start),
                    "toBlock": hex(stop),
                    "topics": [[DEPOSIT, NATIVE_DEPOSIT]],
                }
            ],
        )
        if isinstance(answer, dict) and "error" in answer:
            raise RuntimeError(f"robinhood chain refused blocks {start}-{stop}: {answer['error']}")
        logs.extend(answer)

    deposits = []
    for log in logs:
        words = [log["data"][2 + index * 64 : 66 + index * 64] for index in range(len(log["data"][2:]) // 64)]
        native = log["topics"][0] == NATIVE_DEPOSIT
        depositor = "0x" + words[0][24:]
        token = None if native else "0x" + words[1][24:]
        amount = int(words[1] if native else words[2], 16)
        deposits.append((depositor, token, amount, native))

    meta = {}
    for _, token, _, _ in deposits:
        if token and token not in meta:
            try:
                symbol_raw = call("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
                body = bytes.fromhex(symbol_raw[2:])
                length = int.from_bytes(body[32:64], "big")
                symbol = body[64 : 64 + length].decode("utf8", "replace")
                decimals = int(call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
                meta[token] = (symbol, decimals)
            except Exception:
                meta[token] = (None, None)

    codes = code_of(url, {depositor for depositor, _, _, _ in deposits})
    groups, apps = {}, {}
    for depositor, token, amount, native in deposits:
        symbol, decimals = ("ETH", 18) if native else meta.get(token, (None, None))
        value = usd(amount, symbol, decimals, table)
        code = codes.get(depositor, "?")
        group = groups.setdefault(classify(code), {"depositors": set(), "deposits": 0, "usd": 0.0, "unpriced": 0})
        group["depositors"].add(depositor)
        group["deposits"] += 1
        if value is None:
            group["unpriced"] += 1
        else:
            group["usd"] += value

        # One 7702 implementation is one application. That is what tells the other tenants apart.
        if code.startswith(DELEGATION):
            entry = apps.setdefault("0x" + code[len(DELEGATION) :], {"wallets": set(), "deposits": 0, "usd": 0.0})
            entry["wallets"].add(depositor)
            entry["deposits"] += 1
            entry["usd"] += value or 0.0
    return groups, apps


def signatures(call, address, since):
    """Every signature mentioning `address` back to `since`, newest first."""
    out, before = [], None
    while True:
        query = {"limit": 1000}
        if before:
            query["before"] = before
        page = call("getSignaturesForAddress", [address, query])
        if isinstance(page, dict) and "error" in page:
            raise RuntimeError(f"solana refused a signature page for {address}: {page['error']}")
        if not page:
            return out
        for entry in page:
            if entry.get("blockTime") and entry["blockTime"] < since:
                return out
            out.append(entry["signature"])
        before = page[-1]["signature"]


def transactions(url, wanted):
    """`getTransaction` for many signatures, batched the way code_of batches eth_getCode.

    A hundred at a time costs a fraction of a second and half a megabyte, which is what
    makes reading every deposit affordable rather than a reason to sample.
    """
    out = []
    for start in range(0, len(wanted), 100):
        chunk = wanted[start : start + 100]
        body = json.dumps(
            [
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "getTransaction",
                    "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }
                for index, signature in enumerate(chunk)
            ]
        ).encode()
        request = urllib.request.Request(url, method="POST", data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            answers = json.load(response)
        out.extend(answer.get("result") for answer in answers)
    return out


def deposit_instructions(transaction):
    """(amount, mint) for every Relay deposit in one transaction, and its decimals.

    The instruction is eight bytes of discriminator, the amount as a little-endian u64,
    then the 32-byte order id; the mint is the instruction's fifth account. Decimals come
    from the transaction's own token balances, so nothing is scaled by a guessed 18 —
    a deposit whose mint or decimals cannot be read is returned unscaled and priced by
    nobody.
    """
    message = transaction["transaction"]["message"]
    meta = transaction.get("meta") or {}
    decimals = {
        balance["mint"]: balance["uiTokenAmount"]["decimals"] for balance in (meta.get("postTokenBalances") or [])
    }
    for instruction in message["instructions"]:
        if instruction.get("programId") != RELAY_DEPOSIT or "data" not in instruction:
            continue
        raw = base58.b58decode(instruction["data"])
        if len(raw) < 48 or raw[:8] != DEPOSIT_DISCRIMINATOR:
            continue
        accounts = instruction.get("accounts") or []
        mint = accounts[4] if len(accounts) > 4 else None
        yield int.from_bytes(raw[8:16], "little"), mint, decimals.get(mint)


def solana_deposits(url, call, since, table):
    """Relay's Solana deposits over the window, split by whether FOMO's co-signer signed.

    Both halves are read out of the same transactions by the same rule, so the split is a
    measurement rather than a share by count applied to a total read somewhere else. FOMO
    on Solana is the co-signer and nothing else — there is no code marker on a Solana
    account — so the test is whether it signed, taken off the signer list of the
    transaction itself.

    A transaction that mentions the program but carries no deposit instruction is counted
    rather than dropped: it is the difference between a quiet window and a decode that
    has stopped matching.
    """
    wanted = signatures(call, RELAY_DEPOSIT, since)
    groups = {
        name: {"deposits": 0, "usd": 0.0, "unpriced": 0, "assets": {}}
        for name in ("FOMO (co-signer present)", "everyone else")
    }
    not_deposits = unreadable = 0

    for transaction in transactions(url, wanted):
        if not transaction:
            unreadable += 1
            continue
        signers = {key["pubkey"] for key in transaction["transaction"]["message"]["accountKeys"] if key.get("signer")}
        group = groups["FOMO (co-signer present)" if CO_SIGNER in signers else "everyone else"]
        found = False
        for amount, mint, decimals in deposit_instructions(transaction):
            found = True
            symbol = mint_name(mint) if mint else None
            value = usd(amount, symbol, decimals, table)
            group["deposits"] += 1
            if value is None:
                group["unpriced"] += 1
            else:
                group["usd"] += value
                group["assets"][symbol] = group["assets"].get(symbol, 0.0) + value
        if not found:
            not_deposits += 1

    return groups, len(wanted), not_deposits, unreadable


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    robinhood_url = endpoint("ROBINHOOD_MAINNET_HTTPS")
    solana_url = endpoint("SOLANA_MAINNET_HTTPS")

    since = int(time.time() - minutes * 60)
    opened = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(since))
    print(f"window   last {minutes:g} min, from {opened} to now")

    table, source = prices()
    print(f"prices   {source}\n")

    relay = rpc(RELAY_RPC)
    relay_head = int(relay("eth_blockNumber", []), 16)
    relay_first = block_at(relay, since, relay_head)
    print(f"relay chain      blocks {relay_first}-{relay_head} ({relay_head - relay_first} blocks)")

    robinhood = rpc(robinhood_url)
    robinhood_head = int(robinhood("eth_blockNumber", []), 16)
    robinhood_first = block_at(robinhood, since, robinhood_head)
    print(f"robinhood chain  blocks {robinhood_first}-{robinhood_head} ({robinhood_head - robinhood_first} blocks)")
    print("\nreading…\n")

    minted, filled, burned, unpriced = relay_totals(relay, relay_first, relay_head, table)
    total_usd = sum(row["usd"] for row in minted.values())
    total_deposits = sum(row["deposits"] for row in minted.values())

    print(f"{'RELAY, ALL ORIGINS — deposits attested on the hub':<52}{'deposits':>10}{'volume':>18}")
    by_origin = {}
    for (origin, asset), row in minted.items():
        entry = by_origin.setdefault(origin, {"deposits": 0, "usd": 0.0, "assets": []})
        entry["deposits"] += row["deposits"]
        entry["usd"] += row["usd"]
        entry["assets"].append((asset, row["deposits"], row["usd"], row["unpriced"]))
    ranked = sorted(by_origin.items(), key=lambda item: -item[1]["usd"])
    for origin, entry in ranked[:10]:
        share = entry["usd"] / total_usd * 100 if total_usd else 0.0
        print(f"  {origin:<48}{entry['deposits']:>10}{'$' + format(entry['usd'], ',.0f'):>18}   {share:5.1f}%")
        for asset, count, value, missing in sorted(entry["assets"], key=lambda item: -item[2]):
            mark = f"  ({missing} unpriced)" if missing else ""
            print(f"    {asset:<46}{count:>10}{'$' + format(value, ',.0f'):>18}{mark}")
    if len(ranked) > 10:
        rest_usd = sum(entry["usd"] for _, entry in ranked[10:])
        rest_count = sum(entry["deposits"] for _, entry in ranked[10:])
        share = rest_usd / total_usd * 100 if total_usd else 0.0
        label = f"{len(ranked) - 10} smaller origin chains"
        print(f"  {label:<48}{rest_count:>10}{'$' + format(rest_usd, ',.0f'):>18}   {share:5.1f}%")
    print(f"  {'TOTAL':<48}{total_deposits:>10}{'$' + format(total_usd, ',.0f'):>18}")
    print(f"\n  fills settled to solvers  ${filled:,.0f}")
    print(f"  withdrawals burned        ${burned:,.0f}")
    if unpriced:
        print(f"  {unpriced} deposits in assets with no price — excluded from the totals above")

    groups, apps = robinhood_deposits(robinhood_url, robinhood, robinhood_first, robinhood_head, table)
    rh_usd = sum(group["usd"] for group in groups.values())
    rh_count = sum(group["deposits"] for group in groups.values())
    print(f"\n{'ROBINHOOD-ORIGIN DEPOSITS — who paid':<44}{'addresses':>11}{'deposits':>10}{'volume':>16}{'share':>8}")
    for name, group in sorted(groups.items(), key=lambda item: -item[1]["usd"]):
        share = group["usd"] / rh_usd * 100 if rh_usd else 0.0
        print(
            f"  {name:<42}{len(group['depositors']):>11}{group['deposits']:>10}"
            f"{'$' + format(group['usd'], ',.0f'):>16}{share:7.1f}%"
        )
    print(f"  {'TOTAL':<42}{'':>11}{rh_count:>10}{'$' + format(rh_usd, ',.0f'):>16}")

    heading = "THE APPLICATIONS BEHIND THOSE WALLETS — by 7702 implementation"
    print(f"\n{heading:<55}{'wallets':>9}{'deposits':>10}{'volume':>14}")
    for implementation, entry in sorted(apps.items(), key=lambda item: -item[1]["usd"]):
        label = f"{implementation}{'   <- FOMO' if implementation in FOMO_CODE else ''}"
        print(f"  {label:<53}{len(entry['wallets']):>9}{entry['deposits']:>10}{'$' + format(entry['usd'], ',.0f'):>14}")

    solana = rpc(solana_url)
    solana_groups, program_txs, not_deposits, unreadable = solana_deposits(solana_url, solana, since, table)
    solana_measured = sum(group["usd"] for group in solana_groups.values())
    solana_count = sum(group["deposits"] for group in solana_groups.values())
    print(f"\n{'SOLANA-ORIGIN DEPOSITS — who paid':<44}{'deposits':>10}{'volume':>16}{'share':>8}")
    for name, group in sorted(solana_groups.items(), key=lambda item: -item[1]["usd"]):
        share = group["usd"] / solana_measured * 100 if solana_measured else 0.0
        mark = f"  ({group['unpriced']} unpriced)" if group["unpriced"] else ""
        print(f"  {name:<42}{group['deposits']:>10}{'$' + format(group['usd'], ',.0f'):>16}{share:7.1f}%{mark}")
    print(f"  {'TOTAL':<42}{solana_count:>10}{'$' + format(solana_measured, ',.0f'):>16}")
    if not_deposits or unreadable:
        print(f"  {not_deposits} transactions touched the program without depositing; {unreadable} could not be read")

    fomo_rh = groups.get("FOMO", {"usd": 0.0})["usd"]
    fomo_solana = solana_groups["FOMO (co-signer present)"]["usd"]
    solana_hub = by_origin.get("solana", {"usd": 0.0})["usd"]
    hub_rh = by_origin.get("robinhood", {"usd": 0.0})["usd"]

    # Each origin is read twice, once on Relay's chain and once on the chain itself, and
    # the two are never quite equal: the hub attests about a minute late, so a short
    # window clips one side of each. Printing both is what keeps either honest.
    print("\nCROSS-CHECK — each origin's volume, read two ways")
    print(f"{'':<36}{'on relay chain':>18}{'on the origin':>18}{'gap':>10}")
    for label, hub_side, origin_side in (
        ("robinhood", hub_rh, rh_usd),
        ("solana", solana_hub, solana_measured),
    ):
        gap = f"{(origin_side - hub_side) / hub_side * 100:+.1f}%" if hub_side else "-"
        print(f"  {label:<34}{'$' + format(hub_side, ',.0f'):>18}{'$' + format(origin_side, ',.0f'):>18}{gap:>10}")

    print("\nFOMO'S SHARE OF RELAY")
    print(f"  robinhood-origin, measured        ${fomo_rh:,.0f}")
    print(f"  solana-origin, measured           ${fomo_solana:,.0f}")
    print(f"  both origins                      ${fomo_rh + fomo_solana:,.0f}")
    if total_usd:
        print(f"  against all of Relay               {(fomo_rh + fomo_solana) / total_usd * 100:.1f}%")
    print("\n  Both halves are measured on the chain the money left, against a denominator")
    print("  attested on Relay's. FOMO also trades on Base and BNB; deposits originating")
    print("  there are in the denominator and not in FOMO's share, so this is a floor.")


if __name__ == "__main__":
    main()
