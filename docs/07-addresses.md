# Address book

Every address the listeners and the docs rely on, per chain and per layer. The machine-readable
copy is [`../scripts/maintenance/registry.json`](../scripts/maintenance/registry.json), and
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py) re-reads it against
the live chains: Robinhood Chain in depth, every other EVM chain for the contracts and cash below,
and Solana when a Solana endpoint is set. Relay's own chain has a separate RPC and is not covered.

To look one up by eye: [robinscan.io](https://robinscan.io) for Robinhood Chain,
[bscscan.com](https://bscscan.com), [basescan.org](https://basescan.org),
[etherscan.io](https://etherscan.io), and [solscan.io](https://solscan.io) for Solana.

Addresses go stale. Run the verifier before trusting anything here, and use
[`maintenance/event_inventory.py`](../scripts/maintenance/event_inventory.py) to find what
replaced a piece that has gone quiet.

## Every EVM chain — account layer and Relay

These sit at the same address on Robinhood Chain, BNB Smart Chain, Base, Ethereum and Monad, so
one table serves all five.

| Address | What |
|---|---|
| `0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108` | the operations contract — ERC-4337 v0.8 EntryPoint, the only version FOMO uses |
| `0xe6cae83bde06e4c305530e199d7217f42808555b` | `Simple7702Account`, the reference implementation wallets delegate to |
| `0xef0100e6cae83bde06e4c305530e199d7217f42808555b` | not an address — the wallet code every FOMO account carries, and on every chain but Robinhood Chain other applications' accounts too |
| `0x4cD00E387622C35bDDB9b4c962C136462338BC31` | Relay depository — deposits land here |
| `0x5567954cD026CEbFE486062cb75848e0f1c12E58` | Relay router — a solver's fill calls it, and its calldata ends with the order id |
| `0x0729E2a4716d9f7dD17858d5d504426D228697f7` | Relay executor — moves the tokens; fills originate here |
| `0xCcC88a9d1B4ED6b0EABA998850414b24f1c315bE` | Relay router before October 2026 — still live for other Relay traffic |
| `0xb92fe925DC43a0ECdE6c8b1a2709c170Ec4fFf4f` | Relay executor before October 2026 — still live for other solver calls |
| `0xf70da97812cb96acdf810712aa562db8dfa3dbef` | Relay solver treasury — fronts fill capital, receives sweeps |

Bundlers are vanity-prefixed `0x4337…` addresses and rotate; treat them as unlistable. The same
fleet submits other applications' operations too, so a bundler says nothing about whose
operation it is.

An operation sponsored by a paymaster is not FOMO's, whatever its sender's code. The one most
seen on the chains other applications share, `0x8888888888…`, sponsors fun.xyz and tria
deposits; [`04-listening.md`](04-listening.md) has the rule.

## Every EVM chain — what differs

| Chain | Chain id | Native asset | Cash a sell is paid in |
|---|---|---|---|
| Robinhood Chain | 4663 | ETH | USDG `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`, 6 decimals |
| BNB Smart Chain | 56 | BNB | USDT `0x55d398326f99059fF775485246999027B3197955` and USDC `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d`, both 18 decimals |
| Base | 8453 | ETH | USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, 6 decimals |
| Ethereum | 1 | ETH | USDC `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` and USDT `0xdAC17F958D2ee523a2206206994597C13D831ec7`, 6 decimals |
| Monad | 143 | MON | USDC `0x754704Bc059F8C67012fEd69BC8A327a5aafb603` and USDT0 `0xe7cd86e13AC4309349F30B3435a9d337750fC82D`, 6 decimals |

The same symbol carries eighteen decimals on BNB Smart Chain and six everywhere else. Read
`decimals()` per address, never per symbol.

## Robinhood Chain — the rest of Relay

| Address | What |
|---|---|
| `0x63C1d3E9C646184529C5694630a01C00dF171b56` | depository allocator |
| `0xF61A305199fa1135d76FFaB3752D42F55cBd775A` | depository owner |
| `0x0000000071727De22E5E9d8BAf0edAc6f37da032` | EntryPoint version 0.7 — deployed, not FOMO |
| `0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789` | EntryPoint version 0.6 — deployed, not FOMO |

Solver addresses are many and rotating, like bundlers.

## Robinhood Chain — routing, venues, assets

| Address | What |
|---|---|
| `0x39b38686a19836ac10162c490e4558e120cbbe5f` | 0x Settler — runs the route in about a third of bundles, emits only anonymous logs |
| `0x8366a39CC670B4001A1121B8F6A443A643e40951` | Uniswap V4 PoolManager — one contract, every V4 pool |
| `0x1f7d7550B1b028f7571E69A784071F0205FD2EfA` | Uniswap V3 factory — also the chain's busiest launch venue |
| `0xEce6eCd61177336ea6Fb9b17937AC439D85EE20B` | Pancake V3 fork factory |
| `0x8F10B468b06c6FD214B65F87778827F7D113f996` | a non-AMM venue, its own event shape |
| `0x6131B5fae19EA4f9D964eAc0408E4408b66337b5` | a non-AMM venue, its own event shapes |
| `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` | USDG — the quote asset, six decimals |
| `0x0bd7d308f8e1639fab988df18a8011f41eacad73` | WETH |

USDG has six decimals while nearly everything traded against it has eighteen, so every pool on the
chain carries a decimals mismatch across the pair. Read `decimals()` per address.

## Solana

| Address | What |
|---|---|
| `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` | FOMO's co-signer — signs and pays for every transaction FOMO sends. The marker on this chain |
| `F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe` | Relay's payout solver — pays a sell's proceeds out as USDC, from whichever EVM chain the sell ran on. Not FOMO's, and shared with every other application Relay settles here |
| `99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2` | Relay's deposit program |
| `7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ` | Relay vault authority |
| `Dodg2HifwU8rmaVVyMyUZDGTRbqAJTyVYxXPwcbNpBKc` | Relay program config account |
| `DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH` | DFlow — an aggregator routing trades that stay on Solana |
| `JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4` | Jupiter — the same |
| `proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u` | a third router, unnamed, its IDL published onchain |
| `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | USDC — the cash balance |

## Relay Chain

Chain id 537713, RPC `https://rpc.chain.relay.link` and `wss://rpc.chain.relay.link/`, explorer
`explorer.chain.relay.link`.

| Address | What |
|---|---|
| `0xd180Dc3b8Cb71b185c563B6e4857592cA93dACAf` | oracle — batches fill attestations |
| `0xDDD361727C22A01EB137880678A20b0BEaE69318` | hub — the ledger, one token id per asset and origin chain |
| `0x613D3c588F6B8f89302b463F8F19f7241B2857E2` | allocator — authorizes withdrawals |

## Offchain

| Host | Role |
|---|---|
| `prod-api.fomo.family` | quotes, token catalog, tradability, social graph, fees |
| `bundler.prod-edge.fomo.family` | FOMO's 4337 bundler |
| `api.relay.link`, `ws.relay.link` | Relay's own API, where the two legs of a trade are tied together |
| `auth.privy.io` | key custody: one EVM key, the same address on every EVM chain, and one Solana key |
| `mainnet.hudson.jito.wtf` | where Solana transactions are submitted |

> [!NOTE]
> Relay's API is versioned and on its way out: version 2 was throttled on 2026-09-01 and retires
> on 2026-11-24, and version 3 requires a key. Relay's own UI for these transactions is at
> [relay.link/transactions](https://relay.link/transactions).

## Where this comes from

Read from the chains. Every address here was re-verified on 2026-10-30 by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py) against Robinhood
Chain, BNB Smart Chain, Base, Ethereum, Monad and Solana mainnet — code present where code is
expected, EOAs still EOAs, every recorded event still firing at its address on Robinhood Chain,
every recorded topic still the keccak of the signature beside it, and every cash token at the
symbol and decimals above. The cash on each chain was read off the deposits made by wallets
carrying FOMO's delegation on 2026-10-30, and the paymaster `0x8888888888…` was tied to fun.xyz and
tria by the `referrer` on the Relay records of the deposits it sponsored.

From FOMO's app. The offchain hosts — iOS v1.87.1, captured 2026-08-31. Relay Chain's contracts
are named from Relay's documentation and read over `rpc.chain.relay.link`, which the verifier does
not reach.
