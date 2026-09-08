# robinhood-fomo

Reverse-engineer how the **FOMO** trading app works onchain, on **Robinhood Chain** and on
**Solana**. Two outputs, interleaved rather than sequential: a technical reference in `docs/`, and
standalone listeners and tools in `scripts/`. Every architectural claim is proven by a script that
hits a node.

- `README.md` — where a reader starts: the listener table, what each transport subscribes to, and
  the endpoints they need.
- `docs/README.md` — the reference itself, numbered 01 to 07.
- `scripts/CLAUDE.md` — how a script is laid out and what the listeners share. Read it before
  touching anything under `scripts/`.

## Ground rules

- **Verify onchain, always.** No claim enters `docs/` unless a script or an RPC call reproduced
  it. Every doc ends with a short **Where this comes from** block separating what was read from a
  chain from what came out of FOMO's app, with dates. Block explorers and project docs are leads,
  not evidence.
- **A listener that stops delivering is not a quiet chain.** A decode failure degrades to a
  printed `?` and is counted; a transport failure — a closed socket, a refused subscription, a
  stream that has gone silent — raises, naming what failed. The two are not the same, and a run
  that cannot tell them apart reports an hour of silence as a clean run.
- **A technical reference, not an investigation log.** No first person, no hedging, no
  `UNVERIFIED:` markers, no narration of what was tried. An unproven claim is dropped, or restated
  as a scope boundary — a fact about the document ("same-chain execution is out of scope"), never a
  confession ("we never captured this").
- **Start from the registry.** Before writing anything that reads chain state, read
  `scripts/maintenance/registry.json` for addresses and topics, `docs/04-listening.md` for the
  rules a reader has to follow to stay correct, and `docs/06-dataset.md` for what a recorded run
  holds. `scripts/maintenance/verify_registry.py` confirms the registry still holds.
- **Simple over clever.** The scripts are the documentation. No config frameworks, no plugin
  systems, no library beyond `scripts/shared/`. **Decoding stays per-transport; scaffolding does
  not.** A constant, a pure function or a request body used twice belongs in `shared/`, and so does
  reading an endpoint — that is what `shared/env.py` is for. What stays duplicated is the decoding
  itself, where two transports read the same fact out of genuinely different wire formats and
  hiding the difference costs more readability than the duplication does. `shared/` still makes no
  requests: it can build the subscription, never send it.
- **Listeners subscribe, never poll.** Push transports only: log and head subscriptions on
  Robinhood Chain, gRPC or `blockSubscribe` on Solana. Where a chain offers more than one
  transport, there is one script per transport rather than a flag. Endpoints come from `.env`, and
  a script started without its endpoint exits naming the variable to set.
- **Python + uv.** `uv run scripts/<name>.py`, and `uv sync` after any dependency change.
  Functional style, no classes unless they earn it. Solana scripts hit the JSON-RPC directly with
  `requests` — no `solders`/`solana` dependency unless something actually needs transaction
  construction.
- **Ruff before a script is done.** `uvx ruff check scripts/` and `uvx ruff format scripts/`, both
  clean; `.github/workflows/lint.yml` runs the same two on every push and pull request. No unused
  imports, no dead code, no leftover helper nobody calls, and no `noqa` that has stopped
  suppressing anything. Blind `except` is allowed on purpose, because an odd token or a flaky RPC
  call must degrade to a printed "?" rather than kill a listener mid-run.
- **Never commit a real private key, endpoint credential, or `.env`.** `.env.example` carries
  names only, and `*.local.*` files are gitignored scratch.
- **Conventional Commits**, on branch commits and PR titles alike: `<type>(<scope>): <summary>`,
  imperative, lowercase after the colon, no trailing period, 72 characters or less. The repo
  squash-merges, so the PR title becomes the commit on `main`.
