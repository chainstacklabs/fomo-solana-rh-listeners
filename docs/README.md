# Technical reference

| Document | What it covers |
|---|---|
| [01 — How it works](01-how-it-works.md) | The architecture: who owns which contract, where the money lives, and what the chain does and does not reveal |
| [02 — Trade lifecycle](02-trade-lifecycle.md) | What a buy and a sell actually do, from the tap to the last transaction, and who takes a cut of each |
| [03 — Tokens and liquidity](03-tokens-and-liquidity.md) | Where the catalog comes from, what gates trading, and what the liquidity behind it is made of |
| [04 — Listening](04-listening.md) | Which contracts and accounts to subscribe to on each chain, and how to decode what arrives |
| [05 — Relay](05-relay.md) | The cross-chain protocol FOMO runs on: order lifecycle, solvers, settlement chain, public APIs |
| [06 — The recorded feed](06-dataset.md) | The JSONL every listener writes: the fields, the units, and how the two legs of a trade join |
| [07 — Address book](07-addresses.md) | Every address, per chain and per layer |

Working listeners are in [`../scripts/`](../scripts/); addresses and event topics in
machine-readable form are in
[`../scripts/maintenance/registry.json`](../scripts/maintenance/registry.json), which
[`../scripts/maintenance/verify_registry.py`](../scripts/maintenance/verify_registry.py) checks
against both chains.
