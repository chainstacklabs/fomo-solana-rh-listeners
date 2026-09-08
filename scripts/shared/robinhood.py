"""What Robinhood Chain is made of, and the decoding that needs no network.

Addresses, topics and the ABI slicing that reads them. Everything here is either a fact
about the chain or a pure function over bytes, so it can be read in one sitting and changed
in one place. Nothing here makes a request: a listener's own plumbing stays in the listener,
because how 01 and 02 fetch differs and hiding that behind a shared layer would make both
harder to follow, not easier.

`maintenance/registry.json` is the verified record of these values and
`maintenance/verify_registry.py` checks it against the live chain. This module is the copy
the scripts import; when the two disagree, the chain decides.

Reference: docs/07-addresses.md, docs/04-listening.md (Event surface).
"""

# --- who is who -------------------------------------------------------------
from web3 import Web3

CHAIN_ID = 4663
ENTRYPOINT = "0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"
DEPOSITORY = "0x4cD00E387622C35bDDB9b4c962C136462338BC31"
EXECUTOR = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
EXECUTOR_TOPIC = "0x" + "0" * 24 + EXECUTOR[2:]
ROUTER = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"  # Relay's multicall entry; solvers fill through it
CASH = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
BURN = "0x0000000000000000000000000000000000000000"

# A FOMO wallet is an ordinary address carrying one EIP-7702 delegation, and the
# implementation it points at names the application behind it. FOMO_CODE is what
# eth_getCode returns for one, which is the whole of the wallet test.
DELEGATION = "0xef0100"
FOMO_IMPLEMENTATION = "e6cae83bde06e4c305530e199d7217f42808555b"
FOMO_CODE = DELEGATION + FOMO_IMPLEMENTATION
APPS = {FOMO_IMPLEMENTATION: "fomo"}

# --- what to listen for -----------------------------------------------------
USEROP = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
BEFORE_EXECUTION = "0xbb47ee3e183a558b1a2ff0874b079f3fc5478b7454eacf2bfc5af2ff5878f972"
DEPOSIT = "0x49fed1d0b752ce30eee63c7a81133f3363b532fec5d4d7dd1ccfd005de4555e1"
NATIVE_DEPOSIT = "0x8032066556caf3967d8fec4ad22a2d9e1e9576556b2903a0fcd5b1fd201e3477"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
VENUES = {
    V4_SWAP: "UniV4",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniV3",
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": "PancakeV3",
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "UniV2",
}
SYMBOL, DECIMALS = "0x95d89b41", "0x313ce567"  # symbol(), decimals()

# V4 is a singleton: one contract emits for every pool, the pool is topic 1, and the
# fee charged on the swap is the last field of the data rather than a fee() call.
V4_FIELDS = ["int128", "int128", "uint160", "uint128", "int24", "uint24"]


def word(data, i):
    """ABI word `i` of a hex payload with its 0x already stripped."""
    return data[i * 64 : (i + 1) * 64]


def app_name(code):
    """Which application a wallet belongs to, read from its EIP-7702 delegation.

    The application's name when it is one we know, the implementation address when
    it is not, "eoa" for an address with no code, and "contract" for anything else.
    FOMO is one tenant of these contracts, so this is a label rather than a test.
    """
    code = (code or "0x").lower()
    if code == "0x":
        return "eoa"
    if code.startswith(DELEGATION):
        implementation = code[len(DELEGATION) :]
        return APPS.get(implementation, implementation[:8] + "…")
    return "contract"


def topic(log, index=0):
    """Topic `index` of a log as 0x-prefixed 32 bytes, whatever the client returned.

    Raw JSON-RPC hands back a prefixed string and web3 hands back HexBytes, whose
    `hex()` drops the prefix. Stripping `0x` with `lstrip` would eat a leading zero
    of the topic itself, so the prefix is removed once and the value padded back.
    """
    raw = log["topics"][index]
    raw = raw if isinstance(raw, str) else raw.hex()
    return "0x" + raw.removeprefix("0x").rjust(64, "0")


def span(boundaries, index):
    """The user operation a log belongs to, as an exclusive (start, end) range.

    A handleOps bundle is one flat list of logs delimited by `UserOperationEvent`,
    so a log belongs to whoever's boundary comes next. Joining by transaction hash
    instead credits one user's swap to another user's deposit.
    """
    ordered = sorted(boundaries)
    return (max([b for b in ordered if b < index], default=-1), next((b for b in ordered if b > index), float("inf")))


def decode_string(raw):
    """An ABI-encoded string return value, or a bytes32 symbol from an older token."""
    if len(raw) >= 96 and int.from_bytes(raw[:32]) == 32:
        length = int.from_bytes(raw[32:64])
        return raw[64 : 64 + length].decode(errors="replace")
    return raw.rstrip(b"\x00").decode(errors="replace") or "?"


def trade_key(log):
    """What identifies one trade: the log that names it, not its transaction.

    A handleOps bundle carries several users, so two trades share a hash.
    """
    return f"{log['transactionHash']}#{int(log['logIndex'], 16)}"


def quantity(raw, decimals, symbol):
    """An amount when the token's decimals are known, the raw integer when they are not.

    A failed decimals lookup used to become 18, which prints a number that looks
    right and is wrong by whatever the real scale was. An unscaled value marked `?`
    is the honest answer, and the sidecar keeps it whole.
    """
    return f"{raw / 10**decimals:.6f} {symbol}" if decimals is not None else f"? {raw}"


def padded_topic(address):
    """An address as a 32-byte topic, for matching it in an indexed log position."""
    return "0x" + "0" * 24 + address[2:].lower()


def where_of(log):
    """`blk 58394188` — the block a log sits in, as a row reports it."""
    return f"blk {int(log['blockNumber'], 16)}"


def address_in_topic(log, index):
    """The address indexed topic `index` holds, checksummed: 32 bytes carrying 20."""
    return Web3.to_checksum_address("0x" + log["topics"][index][-40:])


def address_in_word(log, index):
    """The address data word `index` holds, checksummed."""
    return Web3.to_checksum_address("0x" + word(log["data"].removeprefix("0x"), index)[24:])


def erc20_deposit(log):
    """(token, raw amount, order id) out of a `RelayErc20Deposit`.

    Word 0 is the depositor, which `address_in_word` reads.
    """
    data = log["data"].removeprefix("0x")
    return "0x" + word(data, 1)[24:], int(word(data, 2), 16), "0x" + word(data, 3)


def native_deposit(log):
    """(raw amount, order id) out of a `RelayNativeDeposit`, which names no token.

    The token is ether, so the amount sits one word earlier than in the ERC-20 shape and
    the order id with it. Reading only the ERC-20 shape loses every buy paid for in ether.
    """
    data = log["data"].removeprefix("0x")
    return int(word(data, 1), 16), "0x" + word(data, 2)


def order_from_calldata(info):
    """The Relay order id a solver's fill carries: the last word of the router call.

    This is the whole cross-chain join — the same id the deposit on the other chain
    names — with no Relay API in the path. Empty for any transaction that is not a
    router call, which is every transaction that is not a fill. `info` is an
    `eth_getTransactionByHash` result.
    """
    if not info or (info.get("to") or "").lower() != ROUTER:
        return ""
    calldata = info["input"][2:]
    return "0x" + calldata[-64:] if len(calldata) >= 64 else ""


def venues(swaps, start=None, end=None):
    """The venues a trade ran through, in order, joined for the `note` column.

    `swaps` is (log index, venue) pairs from one transaction. With no span every swap in
    it counts, which is right for a solver's fill: that is its own transaction rather
    than a bundle, so there is nothing to scope by. With a span only the swaps inside the
    depositor's own user operation count, because a bundle carries several users and a
    hash join would credit one person's swap to another person's deposit.
    """
    inside = swaps if start is None else [(i, v) for i, v in swaps if start < i < end]
    return "+".join(dict.fromkeys(v for _, v in inside))
