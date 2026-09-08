"""What FOMO's Solana side is made of, and how a transaction is classified.

Program ids, mints, and the one rule that turns a wallet's balance changes into a kind of
trade. `03` and `04` both receive a whole transaction, so they classify from the same
evidence and there is nothing honest to gain from two copies of the rule. How each of them
gets that transaction stays in its own file.

`maintenance/registry.json` is the verified record of these values and
`maintenance/verify_registry.py` checks it against the live chain.

Reference: docs/04-listening.md (Solana), docs/01-how-it-works.md (Solana holds the cash).
"""

import json

from shared.feed import NOTHING_MOVED, UNNAMED

# A Solana block full of transactions goes well past the 1 MiB default frame, which
# closes the connection with a 1009 instead of delivering the block.
MAX_FRAME_BYTES = 32 * 1024 * 1024

# Solana has no marker on an account that says FOMO. The co-signer on the transaction
# is the only one there is, so everything here is found through it.
CO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
RELAY_DEPOSIT = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
DEPOSIT_DISCRIMINATOR = bytes.fromhex("0b9c60da27a3b413")

# The other direction. A sell executed on Robinhood Chain pays out here, and a solver
# signs that transfer rather than FOMO, so the co-signer is not in it and no listener
# above ever receives it. The order id travels in a memo.
SETTLEMENT_SOLVER = "F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe"
MEMO_PROGRAMS = {"MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}

ROUTERS = {
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH": "DFlow",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u": "proVF4…",
}
# Programs every transaction touches, which say nothing about where a swap ran.
PLUMBING = {"ComputeBudget111111111111111111111111111111", "11111111111111111111111111111111"}
PLUMBING |= {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"}

WSOL = "So11111111111111111111111111111111111111112"
SOL_DUST = 0.005  # rent of a token account is 0.002 SOL
KNOWN = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    WSOL: "wSOL",
}


def mint_name(address):
    """A ticker when the mint is a known one, the mint itself when it is not.

    Cutting an unknown mint to a prefix here would put the short form in the sidecar too,
    and six base58 characters name no token: two mints sharing a prefix become one row and
    neither can be priced. The terminal shortens what it cannot fit; the record keeps the
    mint whole.
    """
    return "SOL" if address == "SOL" else KNOWN.get(address, address)


def classify(moved, programs, relay):
    """(kind, gave, got, note) from what the wallet's own accounts gained and lost.

    A wrapped-SOL account being closed refunds its rent, so a SOL movement smaller
    than that is dropped whenever anything else moved. Relay wins over everything:
    a deposit is a payment even though the wallet also gave an asset.
    """
    if len(moved) > 1:
        moved = [m for m in moved if m[1] not in ("SOL", WSOL) or abs(m[0]) >= SOL_DUST]
    # Largest first, by size rather than by sign, so `gave[0]` and `got[0]` are each the
    # biggest movement of their direction: the leg of the trade rather than the change
    # left over beside it. Sorting by sign puts the smallest credit first instead, and a
    # SOL change above the dust floor is then reported as the token that was bought.
    moved.sort(key=lambda m: -abs(m[0]))
    gave = [m for m in moved if m[0] < 0]
    got = [m for m in moved if m[0] > 0]
    if relay:
        return "PAY", (f"{-gave[0][0]:.6f} {mint_name(gave[0][1])}" if gave else "?"), UNNAMED, ""
    if gave and got:
        router = "+".join(ROUTERS.get(p, p[:6] + "…") for p in sorted(programs - PLUMBING)) or "?"
        return "SWAP", f"{-gave[0][0]:.6f} {mint_name(gave[0][1])}", f"{got[0][0]:.6f} {mint_name(got[0][1])}", router
    if got:
        return "IN", "", f"{got[0][0]:.6f} {mint_name(got[0][1])}", ""
    if gave:
        return "OUT", f"{-gave[0][0]:.6f} {mint_name(gave[0][1])}", "", ""
    # Nothing the wallet owns changed: it signed, but the movement was somebody else's.
    # Reported rather than dropped, so the feed cannot look complete while omitting it.
    return "OTHER", "", NOTHING_MOVED, ""


def block_subscription(account):
    """The `blockSubscribe` request for whole transactions mentioning `account`.

    `04` and `05` differ in which account they watch and in what they do with what comes
    back, and in nothing else about the subscription itself. The request is a value, so
    it lives here; opening the socket and reading from it stays in each listener, where
    what to do with a block is decided.
    """
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "blockSubscribe",
            "params": [
                {"mentionsAccountOrProgram": account},
                {
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                    "showRewards": False,
                    "transactionDetails": "full",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
    )
