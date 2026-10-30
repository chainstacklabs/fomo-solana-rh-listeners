# Tokens and liquidity

FOMO runs no launchpad and issues no tokens. Its catalog is aggregated from third-party data
providers, ranked on its own servers, and gated per token — none of which leaves a trace on any
chain. What it trades against is ordinary liquidity, sitting on the same venues as everything else.
Spot only: the catalog also lists Hyperliquid perpetuals, on a separate execution path this
repository does not cover.

## Where the catalog comes from

Token records reach the app already assembled. Each one carries asset URLs on two third-party hosts,
`metadata.mobula.io` and `token-media.defined.fi`, which put Mobula and Defined.fi behind the
records.

Every token is keyed by **address and chain**. The address alone is not enough — the same address
exists on several chains, and the same ticker exists many times over on one.

A record carries identity (address, chain, name, symbol, decimals), supply, market data (price,
liquidity, volume and price change over several windows, holder and transaction counts), the venues
the price is read from, social links, an origin block naming the launchpad and its bonding-curve
progress, and safety flags including mint and freeze authorities.

The catalog spans six chains: Solana, Robinhood Chain, Base, Ethereum, BNB Smart Chain and Monad,
and every one of them carries FOMO trades. The app tells the server which of them its build
understands; the server decides everything else.

## Discovery is a server decision

The app sends no filter parameters. There is no sort field, no minimum-liquidity threshold, no
exclusion list — the trending request carries an empty body. Every ranking and filtering decision
happens on FOMO's servers: what trends, what is featured, what is verified, what is hidden, and in
what order. The market data displayed alongside is the visible input; the function that ranks it is
not observable.

There is no single list. A curated set, a trending set, a featured set and several social feeds lean
in different directions, and a token's presence in one implies nothing about the others.

## Tradability is a gate, not a property

Before building a swap the app asks its server whether the token may be traded, and gets back two
independent switches — buying and selling — plus a list of warnings to display. A token can be
sellable but not buyable, so a delisting winds down while holders can still exit.

Nothing about the token contract reveals this. It is a backend decision, checked per token,
immediately before the trade. Appearing in discovery says nothing about whether a swap will be
built.

## The life of a launchpad token

Launchpad tokens begin on a bonding curve, where the launchpad contract is the only counterparty and
the price rises mechanically with each purchase. Once enough has been bought the token **migrates**:
liquidity moves out of the curve into an ordinary AMM pool, and it trades like anything else.

> [!WARNING]
> Two fields track this and they disagree. `graduationPercent` is curve progress; `migrated` is
> whether liquidity actually moved. Tokens appear with `migrated` true and a curve progress of
> zero, verifiably trading in ordinary pools. Trust `migrated`; treat curve progress as a progress
> bar that is often stale and sometimes never filled in.

Graduation state does not affect execution. Trades route through established pools, and nothing in
the execution path touches a bonding curve — curve state is a discovery label.

## What the liquidity is made of

### Robinhood Chain

**USDG is the quote asset**, and it has **six decimals** while nearly everything traded against it
has eighteen. Every pool on Robinhood Chain therefore carries a decimals mismatch across its pair,
and lookalike tokens with eighteen decimals exist. Read `decimals()` per address and resolve tokens
by address only.

### BNB Smart Chain, Base, Ethereum and Monad

The same shape as Robinhood Chain, with each chain's own stablecoins as the quote: USDT and USDC on
BNB Smart Chain and on Ethereum, USDC on Base, USDC and USDT0 on Monad. On BNB Smart Chain both
carry **eighteen decimals**, where the same symbols carry six everywhere else, so a scale guessed
from the symbol is wrong by twelve orders of magnitude there. The venues are the chains' own:
PancakeSwap's V3 and Infinity pools beside Uniswap V2 and V4 on BNB Smart Chain, Uniswap V3 and V4
with Aerodrome's pools on Base, Uniswap on Ethereum. The addresses are in
[`07-addresses.md`](07-addresses.md).

### Solana

**USDC is the quote asset**, at six decimals, and the liquidity is the ordinary Solana ecosystem,
where most of the catalog still lives. Post-migration liquidity sits across Raydium, Meteora, Orca
and PumpSwap, with a long tail behind them. Solana records carry two fields the EVM ones do not:
the token's mint and freeze authorities, when they exist. A null mint authority means supply is
fixed. A mint with no ticker is still a token — resolve by mint address, never by the first few
characters of one.

## Where this comes from

Read from the chains. Robinhood Chain's venues and quote assets, from a pool census on 2026-08-31
recorded in [`registry.json`](../scripts/maintenance/registry.json) and re-verified 2026-09-09 by
[`maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py). The quote assets on
the four other EVM chains, from the tokens wallets carrying FOMO's delegation deposited into Relay
on 2026-10-30, with symbol and decimals re-read by the verifier the same day; their venues, from the
swap event shapes inside FOMO's fills and deposits on those chains the same day. Aerodrome's pools
are named by the factory they report: 14 of 15 Solidly-shaped pools swapping on Base on 2026-10-30
answered `factory()` with Aerodrome's.

From FOMO's app. The catalog, the discovery feeds, the tradability gate, the launchpad fields and
the perpetuals listing — iOS v1.87.1, captured 2026-08-31, and free to change on any build.
