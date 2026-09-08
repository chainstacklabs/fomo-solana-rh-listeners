# How it works

FOMO deploys no contracts. Its onchain footprint is assembled entirely from contracts it does not
own, spread across two chains that do different jobs: Solana holds the cash, and the tokens live
wherever they were issued.

## FOMO owns nothing onchain

No address in this document was deployed by FOMO: the ERC-4337 account standard, a
cross-chain settlement protocol called
[Relay](https://docs.relay.link/references/protocol/overview), and ordinary AMM pools. What FOMO
owns is the app and the servers behind it — which tokens appear, what price is quoted, which route
is chosen, who follows whom.

That division decides what is knowable. The chain records what moved. It does not record why, and
it never records what a user is about to do.

| What | Readable from a node |
|---|---|
| That a trade happened, and for how much | yes |
| Which pool it touched | yes |
| Which token a crossing buy will deliver | no — the payment carries an amount and an order id, nothing else |
| Which token a same-chain swap traded | yes — one swap, both sides named |
| The price the user was shown | no |
| Trending lists, the social graph, who follows whom | no |

## Solana holds the cash

The balance the app displays as `$` is USDC in the user's own Solana wallet, whichever chain they
funded from. Money arriving from any other chain is converted and consolidated there; nothing rests
elsewhere. A FOMO wallet on Robinhood Chain holds no cash between trades, because USDG is a quote
asset a trade passes through rather than a balance anyone keeps.

Tokens behave the opposite way. A token stays on the chain it was issued on, and buying one puts it
in the user's wallet on that chain, where it stays until sold.

So most trades cross: cash on one side, token on the other. That single fact explains the rest of
this document — why a buy takes two transactions, why a listener on one chain sees half a trade, and
why a buy is paid for on Solana while the token it bought is delivered on Robinhood Chain.

## The stack

```
 ┌─ the app ──────────────────────────────────────────────────────────────┐
 │  FOMO's servers: the token list, the price, the route, the fees        │   offchain
 │  Privy holds the keys, one per chain                                   │
 └───────────────────────┬────────────────────────────────────────────────┘
                         │  one signature per action
        ┌────────────────┴─────────────────┐
        ▼ Robinhood Chain                  ▼ Solana
   account layer                      no account layer
   operations contract + wallet       a plain keypair
        │                                  │
        ▼                                  ▼
   ┌──────────────────── Relay ────────────────────┐
   │  paid on one chain, delivered on the other,   │   crossing between chains
   │  settled afterwards on Relay's own chain      │
   └────────────────────┬──────────────────────────┘
                        │
        ┌───────────────┴──────────────┐
        ▼ Robinhood Chain              ▼ Solana
   Relay's router + executor     a solver, when a buy lands here
                                 an aggregator, when it stays on Solana
        │                              │
        ▼                              ▼
   Uniswap V4 / V3 and two forks   Raydium, Meteora, Orca, PumpSwap
```

## The wallet

On Robinhood Chain a FOMO wallet is an ordinary address with a private key, carrying an **EIP-7702**
delegation: a pointer that lets a plain address borrow a contract's behavior without becoming a
contract. Asking a node for its code returns that pointer, and it is identical for every FOMO
wallet — which makes it the only reliable way to recognize one.

The borrowed contract is `Simple7702Account`, the unmodified eth-infinitism reference. Unmodified
matters: there are no session keys, no permission system, no owner. A signature from the wallet's
own key is the only thing that can authorize anything, so FOMO cannot trade a user's account.

Actions reach the chain as **ERC-4337** operations. Rather than sending a transaction, the user
signs a request and a *bundler* — an ordinary address run by FOMO — submits it and pays the network
fee. The operation declares a zero fee and the bundler absorbs the real cost, so FOMO wallets never
hold ether.

Every one of those requests passes through a single contract, and that contract is what makes this
chain readable. The standard calls it the EntryPoint; this reference calls it the **operations
contract**. A bundler hands it a batch of signed requests; it checks each signature, calls the
wallet that signed, and writes one log line per action carrying that wallet's address. Nothing a
FOMO wallet does reaches the chain any other way, so a single subscription to that one contract is
the entire feed of FOMO activity.

The standard has been revised, and each revision is a separate contract at its own address, all of
them live at once. FOMO uses **version 0.8** and nothing else — the version that accepts the 7702
delegation its wallets carry. The two older versions are deployed on this chain and carry other
applications' traffic, so a listener pointed at the wrong address sees none of FOMO.

Two consequences for anyone reading this chain:

- **Bundlers are not an identifier.** There are dozens, all vanity-prefixed, and the set rotates. An
  allowlist of them goes stale silently. Use the wallet's delegation code.
- **`getNonce()` never moves.** FOMO derives a fresh nonce namespace from a timestamp for every
  operation so that many can be in flight at once, leaving the standard nonce permanently at zero.
  It says nothing about whether an account has traded.

Solana needs none of this. The wallet is a plain keypair, and a co-signer supplies the signature
that pays the fee — so the user's Solana address can hold no lamports and still trade.

## Relay, and why buys look strange

Relay is a settlement network rather than a bridge: nothing is locked or minted, and no asset
crosses anything. The user pays into Relay's depository on the chain their money is on, carrying a
32-byte **order id** and nothing else; a **solver** holding its own capital on the destination chain
sends the bought token out of its own pocket, in its own transaction — the **fill**; Relay reimburses
the solver afterwards on a chain of its own.

That is why a buy cannot be read in advance. The payment moves USDC into a vault and says nothing
about the destination, and the token first appears in the fill, in a transaction the user never
signed. It is also why the trade feels instant: nobody is waiting for a bridge.

The same contracts are deployed on every chain Relay supports — a depository that holds deposits, a
router that users and solvers call, and an executor that moves the tokens — and as a single program
on Solana. [`05-relay.md`](05-relay.md) is the full account: order lifecycle, solvers, the
settlement chain.

## Routing and venues

On Robinhood Chain, FOMO's server builds the route and returns a ready-to-sign transaction. The
trade reaches the pools through **Relay**: the operation approves Relay's router, calls it, and
Relay's executor makes the inner calls that run the swap. The slippage limit is compiled into the
route as a minimum acceptable output, so the contract enforces it rather than the app.

The executor is not always the address that touches the pools. In about a third of bundles the
trade passes through 0x Protocol's **Settler**, which runs the multi-hop route and hands the
proceeds back to the executor. No FOMO wallet ever calls it, so it is invisible to anything that
measures by transaction sender, and it emits almost nothing — the few logs it does emit are
anonymous, carrying no topic at all.

The venues underneath are ordinary AMMs — Uniswap V4 and V3 carry most of the flow, with a Pancake
V3 fork and Uniswap V2 taking the rest. Two further venues are not AMMs at all but owned contracts
that emit their own event shapes; a trade can complete through them without producing a Uniswap
`Swap` log anywhere, which breaks any classifier keyed on swap topics.

On Solana, FOMO's own transaction touches no pool when the trade crosses chains: routing is the
solver's business. When the trade stays on Solana, an aggregator routes it across the usual venues —
at least three of them carry FOMO flow, so nothing should be keyed on one router program.

## The two chains side by side

| What | Robinhood Chain | Solana |
|---|---|---|
| Wallet | address plus borrowed contract code | plain keypair |
| How an action reaches the chain | signed request, submitted by a FOMO bundler | signed transaction, submitted through Jito |
| Who pays the network fee | FOMO's bundler | FOMO's co-signer, except a sell's payout, which a Relay solver signs and pays for |
| Route chosen inside the user's transaction? | yes, run by Relay's router and executor | only when the trade stays on Solana |
| Holds the cash balance | no | yes |

## Watching it

The listeners are in [`../scripts/`](../scripts/): `00_listen_fomo.py` for both chains in one feed,
`01_listen_robinhood_logs.py` for every trade landing on Robinhood Chain alone,
`04_listen_solana_blocks.py` for everything FOMO does on Solana alone, and `07_trace_trade.py` to
join the two legs of one cross-chain trade.
[`04-listening.md`](04-listening.md) explains what each vantage point sees.

## Address book

Every address this document names, with the rest of them, is in
[`07-addresses.md`](07-addresses.md) — account layer, Relay, routing and venues, Solana, Relay
Chain and the offchain hosts, per chain. The machine-readable copy is
[`../scripts/maintenance/registry.json`](../scripts/maintenance/registry.json).

## What Robinhood provides

The chain, and nothing else. No Robinhood contract is called at any point in a buy or a sell: the
account layer is a public standard, the crossing and the routing are Relay's, and the pools are
third-party deployments. FOMO is a tenant on this chain, not a partner.

## Where this comes from

Read from the chains. Robinhood Chain and Solana mainnet, over Chainstack nodes. The addresses,
topics, event signatures and decimals are [`registry.json`](../scripts/maintenance/registry.json),
re-verified 2026-09-09 by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py). The Settler's share
of the route was measured on 2026-09-08 over blocks 57993000–57993400, where it was a party to the
token transfers in 70 of 221 bundles.

From FOMO's app. The offchain hosts, which price is quoted and which route the server chooses, and
that the server returns a ready-to-sign transaction — app traffic, iOS v1.87.1, captured
2026-08-31. Relay Chain's own contracts are named from Relay's documentation and read over
`rpc.chain.relay.link`, which
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py) does not reach.
