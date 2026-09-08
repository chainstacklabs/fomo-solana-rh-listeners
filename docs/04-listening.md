# Listening

No single subscription sees a whole FOMO trade. A cross-chain trade is two transactions on two
chains, and the half that reveals what was bought is not the half the user signed. This is what each
vantage point gives, and the decoding rules that keep a listener honest.

## What is knowable, and when

| What | Selling | Buying across chains | Buying on Solana |
|---|---|---|---|
| Where the swap runs | inside the user's own operation | inside a solver's transaction, later | inside the user's own transaction |
| What the first event says | token, venue, both amounts | that someone paid, and how much | token, venue, both amounts |
| When the token is named | at execution | only at the fill | at execution |
| Who signs the last leg | a Relay solver, on Solana | a Relay solver, on Robinhood Chain | the user |
| What that leg costs to see | a second subscription, on the solver | nothing — it runs through a contract | nothing |

Relay fills on the destination chain, and FOMO trades in both directions, so every crossing trade
has a solver's leg. The two are not equally visible. A buy fills on Robinhood Chain through Relay's
executor, a fixed contract emitting a `Transfer`, so watching the contract catches it. A sell fills
on Solana as a bare `spl-token` transfer with a memo — no program, no event — and the only account
that identifies it is the solver's own. On Robinhood Chain the subscriptions are on contracts and
see both sides; on Solana the subscription is on a signer and sees only what FOMO signs.

The deposit that pays for a crossing buy carries an amount and an opaque identifier. Nothing public
names the token before the fill — not the origin chain, and not
[Relay](https://docs.relay.link/references/protocol/overview)'s settlement chain. A crossing buy
becomes readable at the moment it is already done. A buy that stays on Solana has no deposit and no
order id: it is one swap, and it names both sides as it executes.

## Telling FOMO apart from everything else

FOMO is one tenant of these contracts rather than the owner of them. On Robinhood Chain the
delegation designator is `0xef0100` followed by the implementation a wallet runs, and that
implementation names the application, so the delegation is a label and not only a test. Apply the
test on every feed, depositors included — how much of a feed is FOMO varies widely:

| Feed | FOMO's share, counted by address | counted by event |
|---|---|---|
| the [operations contract](01-how-it-works.md) | almost all of it | almost all of it |
| addresses receiving executor fills | about two thirds, the rest other applications, plain EOAs, and the executor's own routing contracts | far less: most `Transfer`s out of the executor are routing hops between contracts, not deliveries to a user |
| deposits into Relay's depository | about nine in ten, the rest carrying other delegations | the same, since a deposit is one event per depositor |

The two columns differ most where it matters most. A count of fill *recipients* says two thirds; a
count of fill *events* says a small minority, because the executor's own routing contracts appear
on both sides of the hops that reach the user. Say which one a number is.

The listeners apply the test to every trade they emit and record the application they read beside
it. One buy per wallet escapes it: the delegation is set by the wallet's first operation, so a new
user's first buy lands before there is any code to read.

| Chain | Marker |
|---|---|
| Robinhood Chain | the wallet's code is the EIP-7702 delegation every FOMO wallet carries |
| Solana | the co-signer that signs and pays for every FOMO transaction |
| Relay's API | the `referrer` field on each request record |
| Relay's chain | none — solvers and assets are visible, the application behind an order is not |

> [!WARNING]
> Do not filter by bundler address. The fleet rotates and any allowlist goes stale silently.
> Wallets that spam FOMO users with junk tokens delegate to a different implementation, so the
> code test excludes them.

## Robinhood Chain

The operations contract is effectively a FOMO-only feed: almost every operation on it comes from a
FOMO wallet. That is a property of that contract alone — Relay's depository and executor are
shared, so the wallet code test still has to be applied there. Two older versions of it are
deployed on the chain and carry other traffic entirely, so subscribing to the wrong address yields
nothing useful.

### The four things to subscribe to

**Everything FOMO does.** The operations contract's `UserOperationEvent`, with the sender at
`topic[2]`.

**Sells, fully described.** The same subscription. A sell is an operation where a non-cash token
leaves the user's wallet *and* a deposit into Relay records the proceeds, both inside that user's
log-index span. Read the venue from the swap logs when they are present — two of FOMO's venues are
owned contracts rather than pools, and a sell through them produces no `Swap` log at all.

**Buy fills.** An ERC-20 `Transfer` out of Relay's executor into an address that passes the wallet
code test. This is the first sight of the token bought; the venue comes from the swap logs in the
same transaction, and the order id from the router's calldata.

**Buy payments.** Deposits into Relay's depository name the payer and the amount but never the
destination. There are two deposit events, one per asset kind, and they are the same trade: watching
only the ERC-20 shape silently loses every buy that was paid for in ether. Most buys landing on
Robinhood Chain are paid from Solana, so this is early warning on a minority of them.

### Reading a bundle

Key a trade by transaction hash *and* log index, and scope its logs to the span between
`UserOperationEvent` boundaries. A `handleOps` transaction can carry more than one user's
operations and its receipt is one flat list, so a hash alone credits one user's swap to another
user's deposit.
Most bundles hold a single operation, which is what makes the ones that do not easy to miss.
[`09_split_bundle.py`](../scripts/09_split_bundle.py) shows the split.

Pick operations out of the decoded operation array by sender. A selector searched for in the
bundle's raw calldata matches inside other users' operations too.

### Event surface

| Contract | Event | Use |
|---|---|---|
| operations contract | `UserOperationEvent` | the primary feed; sender at `topic[2]` |
| operations contract | `BeforeExecution` | bundle boundary — execution logs follow it |
| operations contract | `UserOperationRevertReason` | a failed action |
| Relay depository | `RelayErc20Deposit` | a buy payment *and* a sell's proceeds — separate them by span |
| Relay depository | `RelayNativeDeposit` | the same, paid in ether. One word shorter: amount at word 1, order id at word 2 |
| any ERC-20 | `Transfer` | a buy fill when the sender is Relay's executor |

Topics for each of these are in
[`../scripts/maintenance/registry.json`](../scripts/maintenance/registry.json), and
[`maintenance/event_inventory.py`](../scripts/maintenance/event_inventory.py) reports what every
address in there actually emitted over a window, which is how a topic that moved gets found. The
registry also holds four `Swap` shapes to decode: Uniswap V4, Uniswap V3, a Pancake V3 fork, and
Uniswap V2. All four carry FOMO flow, so a decoder that handles a subset drops the rest without
saying so. Every V4 pool lives inside one pool manager, `0x8366a39C…` rather than Uniswap's
canonical singleton address, so V4 filters by address while V3 spreads across pool contracts and
filters by topic alone.

Check that a log has topics before reading `topics[0]`. An anonymous event carries none, and the 0x
Settler in FOMO's route emits them — rarely inside a bundle, steadily on its own address — so a
decoder that indexes the first topic unconditionally dies on a real transaction rather than a
malformed one.

## Solana

The co-signer is the whole product. It signs and pays for every FOMO transaction and is the first
account of each one, so one filter on that account covers everything — including the trades that
stay on Solana and therefore touch no Relay contract. There is no code-based marker here: a FOMO
wallet is an ordinary keypair.

### The two things to subscribe to

**Everything FOMO does.** Whole transactions mentioning the co-signer,
`AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51`. One filter, and it carries the buy payments and the
same-chain trades together, because both are transactions FOMO signed. Ask for whole transactions
rather than logs: the classification below reads balances, and a log line does not carry them.

**Sell payouts.** Whole transactions mentioning Relay's settlement solver,
`F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe`. The co-signer's filter cannot reach these — see
below — and this one is not a FOMO feed, so a payout is FOMO's only by its order id.

Both are the same subscription shape, differing only in the account named, and both are reachable
over either Solana transport: `mentionsAccountOrProgram` on `blockSubscribe` and `account_include`
on Yellowstone gRPC match an account anywhere in a transaction, signer or not.
[`shared/solana.py`](../scripts/shared/solana.py) builds the WebSocket request for
[`04`](../scripts/04_listen_solana_blocks.py) and
[`05`](../scripts/05_listen_relay_payouts_blocks.py);
[`shared/geyser.py`](../scripts/shared/geyser.py) builds the gRPC one for
[`03`](../scripts/03_listen_solana_grpc.py) and
[`06`](../scripts/06_listen_relay_payouts_grpc.py). Each sends its own and reads what comes back.

### Reading a transaction

Classify by what the wallet's own accounts gained and lost, then read the program to name the venue:

| What moved | Meaning |
|---|---|
| cash into Relay's deposit program | a cross-chain buy or withdrawal being paid for; the instruction carries an amount and an order id, never the token |
| one asset out and another in, no Relay | a trade that stays on Solana |
| one side only | funds moving in or out of the wallet |

> [!WARNING]
> Do not key a swap on a single router program. FOMO routes same-chain trades through at least
> three: DFlow, Jupiter, and a third program with no public name. A classifier keyed on DFlow alone
> files every Jupiter trade as a plain transfer. All three are in
> [`../scripts/maintenance/registry.json`](../scripts/maintenance/registry.json).

### Instruction surface

Two instructions have to be decoded by hand; everything else is read from balances.

| Where | What to read | Layout |
|---|---|---|
| Relay's deposit program, `99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2` | a crossing buy being paid for | 48 bytes: an 8-byte discriminator `0b9c60da27a3b413`, a u64 amount little-endian at 8, the 32-byte order id at 16. Nothing follows |
| the memo programs beside a solver's transfer | which sell a payout settles | two memos: the 32-byte order id as hex, and a unix timestamp. The order id is the one that joins |

A payout itself needs no instruction decoding: it is a plain `spl-token` transfer, not
`transferChecked`, so the instruction names no mint. Read the mint from the source token account
instead, and the recipient from the balance that grew.

One thing the co-signer does not cover: the payout of a sell executed on Robinhood Chain. A Relay
solver signs and pays for that transfer, FOMO's co-signer appears nowhere in it, and a
subscription filtered on the co-signer therefore never receives it. That payout has its own
subscription, keyed on the solver —
[`05_listen_relay_payouts_blocks.py`](../scripts/05_listen_relay_payouts_blocks.py) over
WebSocket, [`06_listen_relay_payouts_grpc.py`](../scripts/06_listen_relay_payouts_grpc.py) over
gRPC — and it is not a FOMO feed: the solver serves every application and every origin chain Relay
settles from, and much of what arrives was paid for somewhere other than Robinhood Chain. Nothing
in a payout names an application, so one is FOMO's only when its order id matches a sell seen on
the other chain.

Closing a wrapped-SOL account refunds its rent, about 0.002 SOL, to the wallet, and that refund
lands in the same balance delta as the trade itself. Taken at face value it becomes the token
bought, so a two-lamport rent refund is reported as the purchase. Ignore SOL and wrapped-SOL
movements under 0.005 whenever anything else moved.

## Transports

Working listeners are in [`../scripts/`](../scripts/), and they implement everything above.
[`00_listen_fomo.py`](../scripts/00_listen_fomo.py) runs both chains in one feed, one transport per
subject; `01` through `06` are the same decoding one subject and one transport at a time — `01` and
`02` on Robinhood Chain, `03` and `04` on Solana, `05` and `06` on the payout leg. Name a transport
to override the default; a default whose endpoint is missing from `.env` exits saying which variable
it wants rather than listening over something else. All of them accept wallet addresses to narrow
the feed to specific users.

Every subscription above arrives over one of two pipes, and the pipes differ in what they carry and
in how they fail. The subsections below are what `00` names on the command line: Robinhood Chain is
`rh-logs` or `rh-blocks`, Solana is `sol-grpc` or `sol-blocks`, and Relay's payouts is
`payouts-blocks` or `payouts-grpc` — a third subject rather than a third Solana pipe, because it
reads the solver, not FOMO.

Raise the WebSocket frame limit on any of them. A busy log subscription on Robinhood Chain and a
Solana subscription carrying full transactions both send frames past the 1 MiB default of most
clients, which closes the connection with a 1009 instead of delivering the message.

### Robinhood Chain

Run [`01_listen_robinhood_logs.py`](../scripts/01_listen_robinhood_logs.py). Run
[`02_listen_robinhood_blocks.py`](../scripts/02_listen_robinhood_blocks.py) when a block's events
have to arrive together, or when the same code has to read past blocks.

| Behavior | [`01_listen_robinhood_logs.py`](../scripts/01_listen_robinhood_logs.py) | [`02_listen_robinhood_blocks.py`](../scripts/02_listen_robinhood_blocks.py) |
|---|---|---|
| Shape | log subscriptions | heads, then a sweep per block |
| Subscribes to | six `eth_subscribe` log filters on one socket | `newHeads` |
| Logs arrive | pushed as each block is published | fetched per block with `eth_getLogs` |
| Ordering | filters are unordered against each other, so a fill waits for a later block's log before it is classified | a block arrives complete |
| Past blocks | no | yes, the sweep takes any range |
| Falls behind | never, nothing is fetched to keep up | one HTTP round trip per sweep |

Both resolve a token's symbol and decimals and read a wallet's delegation over HTTP. The log
listener does it off a queue, so a slow lookup never stalls the socket; the sweeping one folds
those calls into the sweep.

The sweeping listener loses ground whenever a sweep takes longer than the blocks it covers, and
blocks here arrive about every tenth of a second. Past 300 blocks it stops trying to catch up: it
prints how far behind it is and skips forward, so the trades in that range are never emitted.

Two rules for writing a sweeping reader. Drain the head queue to the newest before sweeping, or you
fall a block further behind on every notification. And ask for logs by `blockHash`, because
Robinhood Chain HTTPS is load-balanced across backends whose heads differ by a few blocks, and a
range query ending past the answering backend's head returns fewer logs with no error.

### Solana

Two transports for FOMO's own transactions, in order of what they deliver:

| Script | Transport | What arrives |
|---|---|---|
| [`03_listen_solana_grpc.py`](../scripts/03_listen_solana_grpc.py) | Yellowstone gRPC, a paid add-on | the whole transaction, pushed, nothing to fetch |
| [`04_listen_solana_blocks.py`](../scripts/04_listen_solana_blocks.py) | `blockSubscribe`, on a standard endpoint | the whole transaction, once the block is assembled |

### Relay's payouts

Two transports again, over the same pair of pipes as FOMO's own transactions:

| Script | Transport | What arrives |
|---|---|---|
| [`05_listen_relay_payouts_blocks.py`](../scripts/05_listen_relay_payouts_blocks.py) | `blockSubscribe`, on a standard endpoint | the whole transaction, once the block is assembled |
| [`06_listen_relay_payouts_grpc.py`](../scripts/06_listen_relay_payouts_grpc.py) | Yellowstone gRPC, a paid add-on | the whole transaction, pushed, nothing to fetch |

Standalone either one prints every Relay payout on Solana, narrowing to recipients given as
arguments; [`00_listen_fomo.py`](../scripts/00_listen_fomo.py) runs one of them beside the two
chains and prints only the payouts whose order id matches a sell it has already seen, counting the
rest.

Three facts are read differently over gRPC. A memo arrives as raw instruction data rather than as a
parsed string, so the order id is decoded out of the bytes. Balances are protobuf fields rather than
JSON. And `message.account_keys` holds only the accounts a transaction carries itself — whatever it
loaded from an address lookup table follows in `meta.loaded_writable_addresses` and
`meta.loaded_readonly_addresses`, while `meta.pre_balances` is indexed over the three together.
Reading lamports against the short list credits a payout to whichever account sits at that index,
which silently misattributes exactly the native-SOL payouts described below.

Because the stream is every application's, the payouts in it are not all one shape. FOMO's own
settle in USDC, as an `spl-token` transfer, but a minority of the solver's payouts are native SOL:
a system transfer carrying the same order-id memo, with no token account in the transaction at all.
A handful settle in other tokens. A decoder reading only token balance changes drops the SOL ones silently, so
read lamport gains when no token moved: those payouts are somebody else's sell, and dropping them
uncounted makes the solver look quieter than it is.

A rotated solver address makes this stream go quiet rather than wrong, so a run reporting sells and
no payouts means the address, not the market. `maintenance/verify_registry.py` checks it, and
`00_listen_fomo.py` prints `sells with no payout seen` for the same reason.

### Measuring your own endpoints

How well a transport is served is a property of the endpoint behind it as much as of the protocol.
[`maintenance/compare_listeners.py`](../scripts/maintenance/compare_listeners.py) subscribes to
the four FOMO-keyed transports at once on one event loop, keys every event by transaction and log
index on Robinhood Chain and by signature on Solana, and compares transports only against others
on their own chain over the range all of them covered. The payout pair stays out of it: grouping
is by chain, so the two Solana subjects would each be charged for rows the other was never
subscribed to. Per transport it reports what it saw, how often it was first, its median and
90th-percentile lag behind whichever saw a trade soonest, and how many trades another transport on
that chain caught and it did not. Run it against your own endpoints before committing to one.

## Joining the two legs

The order id is written on both chains, so nothing else is needed. It sits somewhere different in
each direction, and in each direction one half is the user's transaction and the other is a solver's:

| Direction | Robinhood Chain | Solana |
|---|---|---|
| Buy | solver's fill — the last 32 bytes of the call to Relay's router | user's payment — bytes 16 to 48 of the Relay deposit instruction, after the discriminator and the amount |
| Sell | user's deposit — the last word of the depository log | solver's payout — an `spl-memo` carrying the id |

Read those last 32 bytes only when the transaction's `to` is Relay's router. Executor `Transfer`s
also appear inside `handleOps` bundles, where the end of the calldata is unrelated data.

A sell's payout is reachable only by going to the solver. It is a plain `spl-token` transfer of
USDC into the seller's token account, so `getSignaturesForAddress` on the seller's wallet does not
list it either — an SPL transfer names the token account, and `getTokenAccountsByOwner` is what
turns a wallet into one. The solver's address is in
[`registry.json`](../scripts/maintenance/registry.json) under `solana.settlement`; Relay's solvers
rotate the way this chain's bundlers do, so re-check it rather than building an allowlist on it.
The second memo a payout carries is a nonce whose first characters spell the unix time, not an
application tag: there is nothing on this side to attribute with.

That makes the join local and free, in both directions: hold recent deposits by order id and match
each fill against them. [`00_listen_fomo.py`](../scripts/00_listen_fomo.py) does exactly that while
both chains are streaming, printing a filled buy together with the payment that bought it and a
payout together with the sell it settles.
[`07_trace_trade.py`](../scripts/07_trace_trade.py) does it after the fact in either direction,
taking a transaction hash, a Solana signature or an order id and printing both legs — for a sell it
reads the solver's own recent transactions, since nothing indexes a memo. For a wallet rather than
one trade, [`08_wallet.py`](../scripts/08_wallet.py) reads its past on Robinhood Chain, and
[`maintenance/active_wallets.py`](../scripts/maintenance/active_wallets.py) mines recorded runs
for the addresses that keep trading, joining each one's two chains by the same order id.

## Where this comes from

Read from the chains. Every subscription and decoding rule here is implemented by the scripts in
[`../scripts/`](../scripts/), run against Chainstack endpoints on both mainnets. Addresses, topics
and event shapes are [`registry.json`](../scripts/maintenance/registry.json), re-verified
2026-09-09; the transport comparison was last run 2026-09-05. The tenancy shares count three
separate populations, sampled over the operations contract's own traffic, addresses receiving
executor fills, and depository deposits; the first was re-measured on 2026-09-08 over 600 blocks,
where 246 of 247 operations came from wallets carrying FOMO's delegation. The sell payout was
confirmed on 2026-09-08: over Robinhood blocks 57767865–57782865 one solver settled 166 of the
deposits in that window, and all 120 memo-carrying deliveries into a sample of 15 sellers' token
accounts came from the same address.

The Solana instruction layouts — the deposit's discriminator, amount and order-id offsets, and the
two memos beside a payout — were re-read from mainnet transactions on 2026-09-09.

The two payout transports were measured against each other on 2026-09-09, over 150 seconds and
Solana slots 445622460–445622934: both decoded the same 1,143 payouts, agreeing on kind, recipient,
amount and order id for every one, with gRPC ahead by a median of 0.33 s. 159 of those payouts were
native SOL rather than a token.

From FOMO's app and Relay's API. That Relay tags FOMO's requests with `referrer: fomo`, read from
`api.relay.link` on 2026-08-31. Nothing in this document decodes from it.
