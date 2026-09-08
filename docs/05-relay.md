# Relay

[Relay](https://docs.relay.link/references/protocol/overview) moves value between chains without
moving any asset across one. A user pays into a vault on the chain their money is already on, a
solver spends its own capital on the destination chain, and Relay squares up with the solver later
on a third chain of its own. Three consequences follow for anyone reading a FOMO trade: the payment
names no token, the two legs join only by a 32-byte order id, and the settlement record lands a
minute late and names no user.

## The parts

| Part | Where | What it does |
|---|---|---|
| Depository | every supported chain | Takes the user's money. On Solana this is a program with a single relevant instruction |
| Router and executor | destination chains | Where a solver executes the fill and hands the token over |
| Solvers | destination chains | Independent parties that front their own capital and are reimbursed afterwards |
| Oracle | Relay's chain | Reads both chains, verifies the fill matches the order, submits a signed attestation |
| Hub | Relay's chain | The ledger. A deposit mints a balance, a fill transfers it to the solver, a withdrawal burns it |
| Allocator | Relay's chain | Authorizes withdrawals |
| API | offchain | Quotes, order data, request status |

Relay serves many applications, and nothing onchain says which one an order belongs to. Relay's own
API tags FOMO's orders `referrer: fomo`; the chains do not.

## Nothing crosses

Relay is not a bridge. When a user buys a token on another chain:

- their money moves **within** the paying chain, into Relay's vault, and stays there;
- a solver spends **its own** money, already sitting on the destination chain, to buy the token and
  deliver it;
- Relay and the solver settle between themselves afterwards, on Relay's own chain.

Two local transactions and a ledger entry. The trade feels instant because there is no bridge to
wait for, only a solver willing to front the far side.

## A Relay intent never names the token

A deposit carries money and an identifier. The Solana instruction is a discriminator, an amount and a
32-byte order id; the EVM depository event has the same shape with the token added, because the
deposit is an ERC-20 transfer. Neither says which chain the trade lands on, or which token is bought.

The destination lives in Relay's offchain order data, keyed by that id, and is served to solvers
over Relay's API. Onchain there is nothing to read until the fill.

So a crossing buy names its token in exactly one place: the fill. Neither the origin chain nor
Relay's settlement chain carries it before then.

## Solvers

A solver holds its own capital on the destination chain and is repaid after the fact. It sources the
token inside the fill transaction itself rather than delivering from inventory, so the swap and the
delivery are one atomic action and there is no earlier footprint to watch.

On Solana every payout observed came from one address, `F7p3dFrjRTbt…`, and it looks nothing like
an EVM fill: no router, no executor, no event, just an `spl-token` transfer with the order id in a
memo beside it. Nothing in it names an application. Re-check the address rather than hardcoding it —
[`04-listening.md`](04-listening.md) covers the subscription.

The set is small and per-chain — the addresses filling on one chain are not the addresses filling on
another, so a watchlist has to be built per chain by collecting the senders of executor transfers
there.

A solver's working balance on a destination chain is readable on Relay's hub, per asset and origin
chain, and caps how much it can fill before settlement returns its money.

## Following one order

The order id is written on both chains, so the two legs join by reading them:
[`07_trace_trade.py`](../scripts/07_trace_trade.py) takes either leg or the order id itself and
finds the other.

> [!NOTE]
> Relay's own API is on its way out: version 2 was throttled on 2026-09-01 and retires on
> 2026-11-24, and version 3 requires a key.

## The settlement chain

Relay runs its own chain, publicly readable, where an oracle batches attestations of completed fills
and the hub credits the solvers that paid for them. Balances are tracked per asset and origin chain,
and each has a named view contract, so per-chain flow reads as an ordinary token balance.

An attestation carries an action type, an account, a token id and an amount — no order id, and no
address belonging to a user. A settlement entry therefore cannot be joined to a specific trade from
outside.

It also lags: entries land about a minute after the chain events they attest. Relay's chain is a
settlement record, not a feed of pending orders, and nothing on it precedes anything.

## Ordering

Deposit, then fill, then attestation, then settlement. The first two carry timing information and
the last two are bookkeeping. No stage precedes the fill in naming the token: the deposit does not
carry it, the fill is atomic, and the settlement chain is a minute late and anonymous.

Markers for telling FOMO's flow apart from other Relay integrators are in
[`04-listening.md`](04-listening.md).

## Where this comes from

Read from the chains. Relay's depository, router and executor on Robinhood Chain mainnet and its
deposit program on Solana mainnet, over Chainstack nodes, re-verified 2026-09-09 by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py). Addresses are in
[`registry.json`](../scripts/maintenance/registry.json). The settlement chain was read directly over
`rpc.chain.relay.link` on 2026-09-09; the verifier does not reach it, so nothing there is
re-checked on a run.

From Relay's documentation and API. The order lifecycle, the oracle's part in reimbursing a solver,
and how a solver receives the order data behind an id — read from `docs.relay.link` and
`api.relay.link` on 2026-08-31. Nothing in this document decodes from either.
