# Trade lifecycle

One question decides what a FOMO trade looks like onchain: whether the cash and the token sit on
the same chain. If they do, it is a single swap. If they do not, it is two transactions on two
chains, and the second is signed by a stranger. The user names a token and an amount; the route,
the price and — on a crossing trade — the capital all come from parties they never chose.

## Four shapes

The app shows a single cash balance. The user never picks a chain, and nothing asks where the money
should come from.

| Shape | What happens onchain |
|---|---|
| **Cross-chain buy** | A deposit into [Relay](https://docs.relay.link/references/protocol/overview) on the chain holding the cash, then a solver swaps and delivers on the chain holding the token |
| **Cross-chain sell** | The user's own operation swaps the token on its chain and hands the proceeds to Relay; the payout follows on the cash chain |
| **Same-chain trade** | One ordinary swap on that chain's venues. Relay plays no part |
| **Withdrawal** | A deposit on one chain, then a solver pays an external address on another |

A cross-chain trade is two transactions on two chains joined by an identifier. Money leaves on one
side; something arrives on the other. No transaction on either chain holds both halves.

## Before anything touches a chain

The app asks FOMO's server to build the trade, sending a token, an amount and nothing else. The
response is the entire plan: the price, the route, the fees, the slippage tolerance, the minimum
trade size, the order id that ties the legs together, and a ready-to-sign transaction. Every
parameter is the server's choice.

## Buying

The user signs one transaction on the chain their cash is on. It is a deposit into Relay carrying an
amount and a 32-byte order id — **the token being bought appears nowhere in it**. On Solana that
transaction is co-signed by FOMO, which also pays the network fee.

A solver then delivers, in a transaction of its own, on the chain where the token lives. It spends
its own capital, pays its own gas, and the user signs nothing. This is the first moment the token is
named onchain, and the first moment the trade is readable.

The gap between the two is usually seconds, occasionally minutes. A buy paid from an existing in-app
balance produces no user transaction at all until the solver's fill appears.

## Selling

The swap half of a sell runs inside the user's own transaction, so it is legible in one receipt on
Robinhood Chain. The cash half is not on that chain at all. The operation approves Relay's router,
routes through Relay's executor into the pools, and hands the proceeds to Relay's depository. One
receipt carries the pool, the direction and both amounts.

The proceeds never return to the wallet, which ends the trade holding nothing. They become a credit
with Relay, paid out on Solana a second or two later by a solver. That payout is a separate
transaction carrying no FOMO marker — the one leg of a FOMO trade no FOMO-keyed subscription can
reach, which is why it gets its own listener ([`04-listening.md`](04-listening.md)). A sell that
stays on one chain behaves differently: the cash lands directly in the wallet.

## Trades that stay on one chain

When the cash and the token are already on the same chain, nothing has to cross. Relay is absent, no
order id exists, and the trade is a single swap through an aggregator with the proceeds landing in
the user's own wallet.

This path exists on Solana. It does not exist on Robinhood Chain: cash lives on Solana, so a
Robinhood Chain token is always bought from the other side. Every FOMO swap on that chain is
therefore either a solver filling someone's buy or a user's own sell, never a same-chain trade.

The catalog keys tokens by address and chain, so the same ticker on two chains is two different
assets. The user picks a listing, not a symbol.

## Fees, and who takes them

Six parties are paid on a crossing trade and the user is billed for none of them by a transfer.
Everything FOMO and Relay take is inside the amounts the quote sets, and everything the pools take
is inside the price the swap executes at.

| Cost | Who pays it | Where it shows up |
|---|---|---|
| Robinhood Chain gas | FOMO's bundler | nowhere on the user's account: the operation declares a zero fee, `actualGasCost` is zero and the paymaster field is the zero address on every FOMO operation |
| Solana fee and rent | FOMO's co-signer | fee payer on every transaction FOMO sends, and it funds the 0.00186 SOL of rent when a trade has to create the user's USDC token account — two orders of magnitude above the fee itself |
| the fill's gas | the solver | its own transaction, on the destination chain |
| FOMO's fee | the user | a basis-point tier chosen per trade by FOMO's server and priced into the quote. No fee-collector contract, no identifiable recipient on either chain |
| Relay's fee | the user | flat rather than proportional: a fixed charge per movement plus the destination chain's gas, priced into the quote the same way. A wallet's first Solana payout costs more, because it creates the token account |
| the pool's fee | the user | inside the executed price, and set per pool rather than by FOMO. Read it from the last word of a Uniswap V4 `Swap` log, or from `fee()` on a V3 pool |

FOMO wallets hold no ether and are never asked for any, so nothing on Robinhood Chain can charge
them. The pool fee is the one a reader is most likely to assume is constant and is not: across 150
swaps inside FOMO bundles, V3 pools charged between 0.004% and 1%, and V4's fee field ran from zero
to 6%.

Small trades are therefore fee-dominated, which is why the app enforces a minimum trade size. That
minimum arrives in the quote, from FOMO's server, and is not readable from either chain.

## Slippage

Slippage is set by FOMO's server and is not adjustable in the app. It arrives with the quote and is
enforced onchain as the minimum acceptable output compiled into the route, so a pool that moves too
far reverts the transaction rather than filling it badly.

Relay applies its own, much wider tolerance to the crossing leg.

## Watching this happen

[`07_trace_trade.py`](../scripts/07_trace_trade.py) takes either leg — a Robinhood Chain
transaction or a Solana signature — or the order id itself, and prints both legs of one trade.
[`04-listening.md`](04-listening.md) covers the live feeds.

## Where this comes from

Read from the chains. Robinhood Chain and Solana mainnet, over Chainstack nodes, with the scripts
in [`../scripts/`](../scripts/). The fee payers, the zero `actualGasCost` and the zero paymaster
field were read again on 2026-09-08. The token-account rent is
`getMinimumBalanceForRentExemption(165)` = 1,855,569 lamports, read 2026-09-09. The pool fees are
from the 150 swaps inside FOMO bundles over 300 Robinhood Chain blocks on 2026-09-09: 104 Uniswap
V4, 44 V3, 2 Pancake V3, with V4's fee field read from the last word of each `Swap` log and the V3
tiers from `fee()` on each pool.

From FOMO's app. The quote, the route, the fee tier, the slippage tolerance and the minimum trade
size — iOS v1.87.1, captured 2026-08-31. The server picks each of them per trade, and none of them
is readable from either chain.
