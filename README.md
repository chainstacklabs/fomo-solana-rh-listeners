<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/quickstart/">Blockchain API reference</a> • <br> 
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

# FOMO onchain

> **Experimental.** A reference implementation, not for production use.

FOMO is a cross-chain social trading app that deploys no contracts. Its onchain footprint is
ERC-4337 wallets, Relay's settlement contracts and ordinary AMM pools, on Solana and five EVM
chains: Robinhood Chain, BNB Smart Chain, Base, Ethereum and Monad. This repository maps that
footprint and ships working listeners for the trade flow on every one of them.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/), which fetches the Python it wants on the first run, and one
RPC endpoint per chain.

```bash
uv sync
cp .env.example .env    # fill in a WSS and an HTTPS endpoint per EVM chain, and SOLANA_MAINNET_WSS
uv run scripts/00_listen_fomo.py 60
```

Every chain is on by default, and a chain whose endpoint is missing stops the run naming the
variable to set. Turn a chain off by name to run without it:

```bash
uv run scripts/00_listen_fomo.py 60 eth-off monad-off
```

To follow one person, pass their addresses after the seconds. A `0x` address filters every EVM
chain, because a FOMO wallet has the same address on all of them, and a base58 one filters Solana,
so passing only one leaves the other side streaming every FOMO trade:

```bash
uv run scripts/00_listen_fomo.py 300 <0x and 40 hex digits>
uv run scripts/00_listen_fomo.py 300 <0x and 40 hex digits> <32 to 44 base58 characters>
```

## Architecture

FOMO composes four public building blocks and runs its own servers on top:

| Layer | What it is | Who runs it |
|---|---|---|
| Wallets | [ERC-4337](https://eips.ethereum.org/EIPS/eip-4337) accounts carrying an [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702) delegation, one address on every EVM chain; plain keypairs on Solana | public standards |
| Crossing | Relay, a settlement protocol that takes payment on one chain and delivers on another | Relay |
| Routing | Relay's router and executor, at the same addresses on every EVM chain; an aggregator program for a Solana same-chain swap | Relay and solvers, Solana aggregators |
| Venues | Uniswap V4, V3 and V2 and PancakeSwap's pools on the EVM chains, Aerodrome on Base; Raydium, Meteora, Orca, and PumpSwap on Solana | third parties |
| Product | the token list, the quoted price, the chosen route, the fees, the social graph | FOMO |

That split sets what a node can tell you: the chain records amounts, tokens, wallets, and venues,
while FOMO's servers hold the prices, the rankings, and the intent behind a trade. `docs/` works
that through in detail, starting at [`docs/01-how-it-works.md`](docs/01-how-it-works.md).

The cash sits on Solana as USDC in the user's own wallet, and a token stays on the chain that
issued it, so most trades cross: Solana is the hub, and every EVM chain is a spoke. A crossing
trade lands as two transactions carrying one 32-byte
[Relay](https://docs.relay.link/references/protocol/overview) order id, and that id is what joins
them with no help from FOMO's servers.

Both directions are filled by a solver fronting the far side out of its own capital, and the two
legs are not equally visible. A buy's payment on Solana names an amount and an order id but never
the token, so the token first appears when the solver delivers it. A sell reverses that: the user's
own operation is fully readable, while the payout arrives on Solana as a plain USDC transfer signed
by the solver, calling no Relay program and emitting no event. Nothing about that payout says FOMO,
so it takes its own subscription and counts as FOMO's only when its order id matches a sell seen on
an EVM chain.

FOMO's wallets delegate to a public reference account that other applications use too on every EVM
chain but Robinhood Chain. What tells FOMO's operations apart is that they name no paymaster: FOMO's
bundler pays the gas, while the others are sponsored. The listeners apply both tests,
[`docs/04-listening.md`](docs/04-listening.md) has the rule.

## Listeners

Start with `00_listen_fomo.py`: it runs one transport per subject, joins the halves of a crossing
trade as they arrive, and prints them as one feed. Every transport also runs on its own. The
addresses and topics they decode against are recorded in
[`scripts/maintenance/registry.json`](scripts/maintenance/registry.json), which
[`maintenance/verify_registry.py`](scripts/maintenance/verify_registry.py) re-checks against every
chain.

| Script | Chain | What lands in the feed | Transport |
|---|---|---|---|
| [`00_listen_fomo.py`](scripts/00_listen_fomo.py) | all | Every kind below, in one stream, with the two halves of a crossing trade joined as they arrive | any of the below |
| [`01_listen_evm_logs.py`](scripts/01_listen_evm_logs.py) | one EVM chain, named first | `BUY` a token delivered by a solver — the one moment a bought token is named; `SELL` a token swapped and handed to Relay; `PAY` cash into Relay | `eth_subscribe` logs |
| [`02_listen_evm_blocks.py`](scripts/02_listen_evm_blocks.py) | one EVM chain, named first | The same `BUY`, `SELL` and `PAY`, arriving a whole block at a time | `newHeads` + `eth_getLogs` |
| [`03_listen_solana_grpc.py`](scripts/03_listen_solana_grpc.py) | Solana | `SWAP` a trade that never leaves Solana, both sides named; `PAY` the cash leg of a buy landing elsewhere; `IN`/`OUT` a plain token movement | Yellowstone gRPC |
| [`04_listen_solana_blocks.py`](scripts/04_listen_solana_blocks.py) | Solana | The same `SWAP`, `PAY`, `IN` and `OUT` | `blockSubscribe` |
| [`05_listen_relay_payouts_blocks.py`](scripts/05_listen_relay_payouts_blocks.py) | Solana | `FILL` Relay paying out on Solana — a sell made on an EVM chain, or cash moving to Solana | `blockSubscribe` |
| [`06_listen_relay_payouts_grpc.py`](scripts/06_listen_relay_payouts_grpc.py) | Solana | The same `FILL`, one commitment earlier | Yellowstone gRPC |

The EVM chains are `rh` for Robinhood Chain, `bsc` for BNB Smart Chain, `base`, `eth` for Ethereum,
and `monad`, each one row of [`scripts/shared/evm.py`](scripts/shared/evm.py):

```bash
uv run scripts/01_listen_evm_logs.py bsc 60
```

A `PAY` names an amount and an order id but never the token, so it tells you the size of a buy and
who made it, never what they bought — that arrives as the `BUY` on the EVM chain. A `PAY` on an EVM
chain is usually cash on its way to Solana, and `00` shows it beside the payout that lands it. The
Solana listeners also emit `OTHER` for a transaction they received but could not classify, printed
with its signature rather than dropped, so a feed never looks more complete than it is.
[`scripts/shared/feed.py`](scripts/shared/feed.py) defines every kind once, which is what lets two
listeners be read side by side.

[`07_trace_trade.py`](scripts/07_trace_trade.py), [`08_wallet.py`](scripts/08_wallet.py) and
[`09_split_bundle.py`](scripts/09_split_bundle.py) explain one trade, one wallet and one bundle
instead of streaming, each on the EVM chain named first. [`scripts/maintenance/`](scripts/maintenance)
checks this repository against the chains:
[`maintenance/probe_chain.py`](scripts/maintenance/probe_chain.py) tests whether FOMO runs on an
EVM chain the way the listeners expect before that chain gets a row,
[`maintenance/volume.py`](scripts/maintenance/volume.py) totals FOMO's own volume per chain out of
a recording, and [`maintenance/relay_share.py`](scripts/maintenance/relay_share.py) measures how
much of Relay's deposit volume is FOMO's against every other application settling through it. None
of them is needed to run a listener.

### What `00` subscribes to

These names are arguments to `00_listen_fomo.py` only. Scripts `01`–`06` take none of them,
because each one *is* a single transport. `00` has one slot per **subject**: FOMO on each EVM
chain, FOMO on Solana, and Relay's payout solver. Each subject is read through one of two pipes, or
switched off.

Both pipes onto an EVM chain carry the same trades, so picking between them is a question about
your endpoint, not about what you will see. Every EVM chain takes the same three, named after it —
here for BNB Smart Chain:

| Each EVM chain | What you get | Commitment | What it costs |
|---|---|---|---|
| `bsc-logs` *(default)* | Every event pushed as its block is published, nothing polled or fetched | the chain's head | A standard WebSocket endpoint |
| `bsc-blocks` | The same trades grouped per block, and the same code points at history | the chain's head | A few round trips per sweep |
| `bsc-off` | Nothing, and so no buy delivered there is ever named | | |

Same again on Solana:

| Solana | What you get | Commitment | What it costs |
|---|---|---|---|
| `sol-grpc` | Whole transactions pushed, nothing to fetch | `processed` | A Geyser endpoint, a paid add-on at most providers |
| `sol-blocks` *(default)* | Whole transactions too, on a standard endpoint | `confirmed` | A WebSocket carrying full blocks, so a much larger frame |
| `sol-off` | Nothing, so payments and Solana-native swaps go unseen | | |

The last slot is the payouts, and it is a subject rather than a third choice of Solana pipe. The
pipes above each read FOMO's own transactions, keyed on the delegation or the co-signer. These two
key on Relay's payout solver instead, so they deliver *different rows*: the far leg of a sell, which
no FOMO-keyed subscription can reach. Most of what they carry belongs to other applications and is
counted rather than printed.

| Relay's payouts | What you get | Commitment | What it costs |
|---|---|---|---|
| `payouts-blocks` *(default)* | Every payout the solver signs, a block at a time | `confirmed` | A second Solana WebSocket subscription |
| `payouts-grpc` | The same payouts, pushed one transaction at a time | `processed` | A Geyser endpoint, and a second stream on it |
| `payouts-off` | Nothing, and a sell then ends at the deposit into Relay | | |

Each slot has a default, so a command names only what it changes:

```bash
uv run scripts/00_listen_fomo.py 60                                # every default
uv run scripts/00_listen_fomo.py 60 sol-grpc payouts-grpc          # Geyser where it helps
uv run scripts/00_listen_fomo.py 60 bsc-blocks base-blocks         # a block's events must arrive together
uv run scripts/00_listen_fomo.py 60 eth-off monad-off              # leave two chains out
uv run scripts/00_listen_fomo.py 60 rh-off bsc-off base-off eth-off monad-off  # Solana alone, nothing is ever named
```

Which of two pipes serves you better is a property of your endpoint rather than of this code, so
`maintenance/compare_listeners.py` runs the FOMO-keyed ones at once and reports coverage and lag
on yours, chain by chain. Name the transports to compare, or none for all of them:

```bash
uv run scripts/maintenance/compare_listeners.py 120 bsc-logs bsc-blocks
```

It leaves the payout pair out: it groups transports by chain, and the two subjects on Solana would
each be charged for rows the other was never subscribed to. Commitment levels, and how a listener
tells FOMO apart from every other tenant of the same contracts, are worked through in
[`docs/04-listening.md`](docs/04-listening.md).

## Endpoints

[Chainstack](https://chainstack.com) serves every chain here with WebSocket endpoints:
[Robinhood Chain](https://docs.chainstack.com/reference/robinhood-getting-started), BNB Smart Chain,
Base, Ethereum, Monad and Solana mainnet. Solana also offers the
[Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin).

Robinhood Chain is an
[Arbitrum Orbit](https://docs.arbitrum.io/launch-orbit-chain/orbit-gentle-introduction) chain whose
sequencer broadcasts transactions before it publishes them in a block, which is earlier than any
log subscription. For that edge, see
[chainstacklabs/robinhood-chain-sequencer-feed](https://github.com/chainstacklabs/robinhood-chain-sequencer-feed).
