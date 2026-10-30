"""Which transports exist, what each one needs in `.env`, and what to call it.

00_listen_fomo.py and maintenance/compare_listeners.py both pick transports by name, and both have
to know which chain one reads and what an unconfigured one is missing. They carried that
table twice and the copies had already drifted: one listed four transports and the other
five, and each said which chain a transport reads in its own way. It is one table now, so
adding a transport is one row and the two entry points cannot disagree about what exists.

Every EVM chain in shared/evm.py's `CHAINS` gets the same two rows, `<chain>-logs` and
`<chain>-blocks`, read by the same two scripts with the chain bound in. Adding a chain is a
row there; this table follows.

Modules are named as strings and imported with `importlib` by `listener()` when a caller
asks for one, so importing this file imports no listener and a listener is free to import
this file.
"""

import importlib
from functools import partial
from typing import NamedTuple

from shared import env
from shared.evm import CHAINS


class Transport(NamedTuple):
    """One way of reading one chain. `needs` is every variable it cannot start without."""

    module: str
    chain: str  # the code a row is printed under: see shared/feed.py
    needs: tuple[str, ...]
    what: str  # what it subscribes to, for the line a listener prints when it starts
    summary: str  # the trade-off between this transport and the others on its chain
    network: str = ""  # the key into shared/evm.py's CHAINS an EVM listener is bound to


def evm_transports(network, chain):
    """The two ways of reading one EVM chain, named after it."""
    needs = (f"{chain.variable}_MAINNET_WSS", f"{chain.variable}_MAINNET_HTTPS")
    return {
        f"{network}-logs": Transport(
            module="01_listen_evm_logs",
            chain=chain.code,
            needs=needs,
            what="6 log filters, FOMO only",
            summary="eth_subscribe logs, nothing polled or fetched. Standard WSS.",
            network=network,
        ),
        f"{network}-blocks": Transport(
            module="02_listen_evm_blocks",
            chain=chain.code,
            needs=needs,
            what="new heads, FOMO only",
            summary="newHeads plus per-block eth_getLogs. More requests, groups blocks.",
            network=network,
        ),
    }


TRANSPORTS = {
    **{name: t for network, chain in CHAINS.items() for name, t in evm_transports(network, chain).items()},
    "sol-grpc": Transport(
        module="03_listen_solana_grpc",
        chain="SOL",
        needs=("GEYSER_ENDPOINT", "GEYSER_API_TOKEN"),
        what="FOMO's co-signer, whole transactions",
        summary="Yellowstone gRPC. Whole transactions. Needs a paid Geyser endpoint.",
    ),
    "sol-blocks": Transport(
        module="04_listen_solana_blocks",
        chain="SOL",
        needs=("SOLANA_MAINNET_WSS",),
        what="FOMO's co-signer, whole blocks",
        summary="blockSubscribe. Whole transactions, no add-on needed.",
    ),
    "payouts-blocks": Transport(
        module="05_listen_relay_payouts_blocks",
        chain="SOL",
        needs=("SOLANA_MAINNET_WSS",),
        what="Relay's payout solver, whole blocks",
        summary="Relay's payout solver over blockSubscribe. Settles sells; other applications are counted.",
    ),
    "payouts-grpc": Transport(
        module="06_listen_relay_payouts_grpc",
        chain="SOL",
        needs=("GEYSER_ENDPOINT", "GEYSER_API_TOKEN"),
        what="Relay's payout solver, whole transactions",
        summary="Relay's payout solver over Yellowstone gRPC. Needs a paid Geyser endpoint.",
    ),
}

# The name a chain goes by in a sentence, where the code a row is printed under would
# read as shorthand.
CHAIN_NAME = {chain.code: chain.name for chain in CHAINS.values()} | {"SOL": "Solana"}

# Two transports per subject: FOMO on each EVM chain, FOMO on Solana, and Relay's payout
# solver on Solana. Each is read through two pipes, so picking between the pipes on one
# subject is a question about an endpoint. The payout pair is prefixed for its subject
# rather than for its chain, because what separates it from `sol-grpc` and `sol-blocks` is
# not the chain — it is that it keys on the solver instead of on FOMO.
#
# compare_listeners groups by chain, which puts two subjects in one Solana group and would
# charge each pipe for what the other saw. The payout pair stays out of it until that
# grouping is by subject.
COMPARABLE = tuple(name for name in TRANSPORTS if not name.startswith("payouts"))


def listener(name, **extra):
    """The `listen()` behind `name`, with its chain and anything in `extra` bound in.

    Every caller then starts every transport the same way, with the four arguments
    scripts/CLAUDE.md describes. `extra` is for what only some transports take — the
    EVM ones take `claimed` — and is dropped for the rest.
    """
    transport = TRANSPORTS[name]
    listen = importlib.import_module(transport.module).listen
    return partial(listen, chain=transport.network, **extra) if transport.network else listen


def missing(name):
    """The variables `name` needs that `.env` does not have, in the order declared."""
    return [variable for variable in TRANSPORTS[name].needs if not env.optional(variable)]


def unconfigured(name):
    """Why `name` cannot start, phrased for a person, or "" when it can."""
    absent = missing(name)
    return f"needs {' and '.join(absent)} in .env" if absent else ""
