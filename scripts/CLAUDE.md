# scripts/

## Script conventions

Every file in `scripts/` follows the same shape — match it:

- `NN_short_name.py` at the top level, numbered by what you reach for first: the combined
  listener, then the per-transport ones, then the tools that explain one trade or one wallet.
  Renumber when that order changes, and fix the usage line in the docstring along with the
  references in `README.md` and `docs/`. Scripts under `maintenance/` carry no number.
- Module docstring: one line on what it answers, then the usage line.
- `load_dotenv()`, then one client built straight from an env var. No config layer.
- Arguments are `sys.argv` positionals with inline defaults. No argparse.
- Output is `print`. No logging, no return values anyone parses. A listener also records every
  trade in full to `runs/<script>-<time>.jsonl` through `shared/feed.py`; nothing else writes a
  file. `runs/` is gitignored down to its `.gitkeep`.
- Every endpoint a script needs is read through `shared/env.py` and checked before use, so a
  missing one exits with the variable's name rather than a `KeyError`. Nothing is substituted.
- **A listener that loses its subscription stops.** It does not poll to cover the gap and does not
  drain an empty queue until the deadline, either of which ends in a summary that reads like a
  quiet chain rather than a socket that died. The exception propagates; `00` reports which
  transport stopped and carries on with the other chain.
- **Nothing is substituted for a value that could not be read, either.** A token whose `decimals()`
  did not answer is printed unscaled and marked `?`, never divided by a guessed 18: a wrong number
  that looks right is worse than an obviously missing one, and the failure is counted.

Subdirectories, so that the numbered files at the top level of `scripts/` are the whole of what
somebody watching FOMO has to read:

- `shared/` — modules the scripts import rather than run:
  - `feed.py` — the `Trade` every listener emits, and how a row is printed and recorded.
  - `robinhood.py`, `solana.py` — one per chain: its addresses, topics and program ids, plus the
    decoding that needs no network. Nothing here makes a request.
  - `env.py` — `endpoint("NAME")` returns it or exits naming it; `optional("NAME")` for the
    callers that decide for themselves, which is how a listener reading its endpoint at module
    level stays safe to import.
  - `geyser.py` — the channel credentials and the `Subscribe` request the two gRPC listeners
    share, differing only in the account watched. Builds the request; never sends it.
  - `transports.py` — the one table of which transports exist: the module behind each name, the
    chain it reads, the variables it cannot start without, and how it differs from the others on
    its chain. Adding a transport is one row here, and `00` and `compare_listeners` both pick
    from it by name so they cannot disagree about what exists.
  - `cli.py` — `run(name, listen)` is how a listener is started from the command line: it parses
    the duration and the wallets, requires the endpoints, prints the line the run opens with, and
    prints the tally on the way out even when the transport raised. `wallet_shaped()` is here too,
    because `00` validates the same arguments.

  A constant or a pure function used by two scripts belongs here, and that includes the decoding
  rules — everything that reads one Robinhood Chain log has one home in `shared/robinhood.py`.
  Fetching does not: `01` asks for one thing at a time as events arrive and `02` batches a whole
  sweep, so their `rpc` and their lookups stay separate on purpose.
- `maintenance/` — scripts that check this repository, or measure the market around it, rather
  than read FOMO's own flow. Unnumbered, run the same way as the rest, and none of them is
  needed to run a listener:
  - `verify_registry.py` — re-checks every claim in `registry.json`, which sits beside it,
    against both chains.
  - `event_inventory.py` — what each recorded contract actually emits over a block window, for
    when the registry has gone stale and the question is what replaced it.
  - `compare_listeners.py` — the four FOMO-keyed transports at once on one event loop, reporting
    coverage and lag against each other. Imports them through `transports.py` rather than copying
    them. The payout pair stays out: it groups by chain, and the two Solana subjects would each be
    charged for rows the other was never subscribed to.
  - `active_wallets.py` — mines `runs/` for the wallets that keep trading, so a test has a live
    address to point at. Touches no network.
  - `relay_share.py` — how much of Relay's deposit volume is FOMO's, and who else deposits into
    Relay. The odd one out here: it measures the market rather than checking the repository, and
    it is the only script that reads a third chain (Relay's own, for the whole-protocol
    denominator) and the only one with an offchain dependency (CoinGecko, for ETH/SOL/BTC).
    Assets it cannot price are counted and reported unpriced, never folded in at a guess.

  Each one puts `scripts/` on `sys.path` before importing `shared/`. Ruff allows a bare
  `sys.path.insert(...)` ahead of the imports, so none of them needs a `noqa: E402` — compute
  anything before that line and E402 starts firing again. `registry.json` and
  `wallets.local.json` both live here, beside the only scripts that open them; `runs/` stays in
  `scripts/`, because every listener writes it.
- `runs/` — what the listeners record, one JSONL file per run. Gitignored down to its `.gitkeep`.
- `geyser/` — the vendored Yellowstone protobuf stubs `03` and `06` need. Generated, not edited.

## What the listeners share

`00_listen_fomo.py` imports all six listeners rather than copying them — `01` through `06` — and
`maintenance/compare_listeners.py` imports the four that carry FOMO's own trades. Those are the
only imports of one script by another.

That makes `01` through `06` the single home of each transport's decoding, so a rule changes in one
file and is tested in one file. Each of them exposes:

```python
async def listen(deadline, watched, emit, counts)
```

- `deadline` is an absolute `time.time()` value.
- `watched` is a set of wallet addresses, empty for all of them.
- `emit(trade)` is called once per trade with a `Trade`, a NamedTuple, so a new field costs nothing
  at the call sites. `feed.start()` returns the printer `cli.run()` passes; `00` wraps it to name
  the chain and to hang a join on the row below, and `compare_listeners` passes one of its own that
  records arrival times instead of printing.
- `Trade.key` identifies one trade across transports: the signature on Solana, and **transaction
  hash plus log index** on Robinhood Chain, because one `handleOps` bundle carries several users and
  the hash alone merges their trades.
- `Trade.order_id` is the Relay order id when the trade has one. It is the same value on both
  chains, so holding Solana payments by it and matching the last word of each Robinhood fill joins
  the two legs with no third party. `00_listen_fomo.py` does that live.
- `Trade.app` is the application behind the wallet, read from its EIP-7702 implementation. FOMO is
  one of several on Robinhood Chain, so that value is the filter as well as the label: a listener
  emits the wallets carrying FOMO's delegation and nobody else's.
- `kind` is `BUY`, `SELL`, `PAY`, `SWAP`, `IN`, `OUT`, or `OTHER`. `OTHER` means this transport
  received the trade but cannot classify it, which is not the same as disagreeing about it;
  `maintenance/compare_listeners.py` counts it as seen and leaves it out of the disagreement
  tally. **A listener must never silently drop a transaction it received.** Doing so makes the
  feed look complete when it is not, and makes the transport look lossy when the endpoint is fine.

`shared/feed.py` is imported by every listener. It owns the header, the column key, the row, the
colour, the burst separators and the JSONL sidecar, and it decodes nothing — a listener hands it a
`Trade` and it formats and records that. Widths are fixed once per run from the terminal size, so a
row always wraps at the same place if it wraps at all. `feed.amount()` and `feed.short()` are
public because `00` builds a join line out of the same pieces. Change a column there and all six
listeners change together.

Because they are imported, `01` through `06` keep their entry point behind
`if __name__ == "__main__":`, and so does `compare_listeners`. Scripts `07` through `09` and the
rest of `maintenance/` are never imported and run at module level.

## How a listener is laid out

`01` and `02` read the same flow and are laid out the same way, so either can be read top to
bottom and any one step lifted out of it:

- module level, no network and no state — the pure steps. `01` has `wanted`, `is_fomo` and
  `sold_in_span`; `02` has `result`, `operation_boundaries`, `swaps_by_transaction` and its own
  `sold_in_span`, which reads raw `Transfer` logs where `01` reads what its subscription recorded.
- module level, one call each — the lookups. `01` has `rpc`, `token_meta`, `app_of` and `order_of`,
  and each takes the cache it should remember its answer in rather than closing over one, so none
  of them holds state and any of them can be called on its own.
- inside `listen()` — the transport, and nothing else: what to subscribe to, and when a log has
  enough of its transaction to be decoded. `01` holds fills and deposits until a later block
  arrives; `02` sweeps a block range per head. Both wire the steps above into four named
  functions that turn one log into one `Trade` — `02` names its sweep's phases too, so `handle()`
  reads as the four round trips and two decoding passes it is.

A step that needs no network and no run state goes to `shared/robinhood.py` instead, where the
other Robinhood Chain scripts can reach it.

`05` and `06` read a subject FOMO does not sign: Relay's payout solver, whose transfers no filter
on FOMO can see. Their rows cannot be attributed to FOMO on their own — `00` names a payout only
when its order id matches a sell it already saw. They reach that solver over the same two pipes
`03` and `04` reach the co-signer over; what differs is the wire, and `06`'s docstring names the
three facts a Geyser update states differently.

## Before calling a script done

- `uvx ruff check scripts/` and `uvx ruff format scripts/`, both clean. `.github/workflows/lint.yml`
  runs the same two on every push and pull request, against the pinned ruff in that file — bump
  both together, since a formatter release can reflow code nobody touched.
- Run it against a live endpoint and read the output, rather than trusting that it parses. For a
  listener, check that the `runs/*.jsonl` row count matches the summary tally, and that piping it
  through `cat` leaves no escape codes behind.
- `uv run scripts/maintenance/verify_registry.py` after touching `maintenance/registry.json`.
- `uv run scripts/maintenance/compare_listeners.py 60` after touching any listener: it decodes
  through the same `listen()` the standalone scripts use, so a disagreement it reports is a real
  one.
