"""How a listener prints a trade, and where the full one goes.

The kinds a listener emits are defined here, once, because five listeners that disagree
about what a word means are five listeners you cannot read side by side. Which chain
produces which is a fact about how FOMO uses that chain: Solana holds the cash, Robinhood
Chain holds the tokens, and only Robinhood Chain names a token at the moment it is bought.

  BUY      RH     a token arrived from Relay's executor. The first moment that token is
                  public, and the only event that ever names it
  SELL     RH     a token left a wallet and the proceeds landed in Relay's depository
  PAY      both   cash into Relay: somebody is buying something. The deposit carries an
                  amount and an order id and never the token, so it gives you the size
                  and the wallet, never what was bought
  SWAP     SOL    a trade that stayed on one chain: the wallet gave one asset and
                  received another, with Relay absent
  IN/OUT   SOL    a plain token movement into or out of a wallet
  FILL     SOL    Relay paying out a sell made on another chain, from 05. Its recipient
                  and order id are readable; which application it belongs to is not, so
                  only a feed watching both chains can call one of these FOMO's
  OTHER    SOL    received but not classifiable: nothing the wallet owns changed, so
                  there is no amount to read and no side to take. Emitted with its
                  signature rather than dropped, because a feed that quietly omits a
                  transaction it received looks complete when it is not

`shared/solana.py` decides which of the Solana kinds a transaction is; on Robinhood Chain
each kind has its own log shape and the listener that reads it decides.

A complete row is 240 columns wide. A wallet is 44 characters, a Robinhood transaction 66,
a Solana signature 88, and padding every field to its worst case spends another 60 on air.
So each row wrapped, at a different point every time, and a burst of trades arrived as a
wall. The rule that produced those rows still holds — a row you cannot look up or copy is a
row you cannot act on — but it is met somewhere else now.

The terminal gets about a hundred columns, aimed at the eye: kinds and chains in colour,
amounts without their trailing zeros, and hashes cut to the ends that let you recognise
one. `scripts/runs/<script>-<time>.jsonl` gets everything, in full, one JSON object per
trade, flushed as it is written. That file is what you grep, join and copy from, during the
run or after it:

    tail -f scripts/runs/00_listen_fomo-*.jsonl | jq -c 'select(.kind == "BUY")'

This lives in `scripts/shared/` rather than beside the listeners because it is a module,
not a script: `from shared import feed`. It decodes nothing; it formats and records what a
listener already decoded.

Reference: docs/06-dataset.md, which documents the record this file writes.
"""

import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple


class Trade(NamedTuple):
    """One decoded trade, as every listener hands it over. A new field costs nothing."""

    kind: str
    gave: str = ""
    got: str = ""
    who: str = ""  # the wallet, in full
    app: str = ""  # the application behind the wallet, from its delegation
    note: str = ""  # the venue or router the trade ran through
    where: str = ""  # the block on Robinhood Chain, the slot on Solana
    key: str = ""  # what identifies this trade on this chain
    order_id: str = ""  # the Relay order id, the key that joins a trade's two legs


# What a row says when the chain did not say it. Both chains use the same words, so
# the vocabulary lives with the thing that prints it.
UNNAMED = "an unnamed token"
NOTHING_MOVED = "nothing the wallet owns moved"

RUNS = Path(__file__).parent.parent / "runs"  # scripts/runs, beside the listeners
ISATTY = sys.stdout.isatty()
COLOUR = ISATTY and not os.environ.get("NO_COLOR")
PIPED = 200  # the width to lay out for when nothing is watching: a pipe has none
GAP = 0.25  # a quiet spell this long ends one burst and starts the next
REPEAT = 40  # rows between reprints of the header, so a burst cannot scroll it away

# Colour is a column that costs nothing. It carries the kind and the chain, which is
# what the eye looks for first, and leaves the width for what has to be read.
KINDS = {"BUY": "32", "SELL": "31", "SWAP": "36", "PAY": "33", "IN": "32", "OUT": "31", "OTHER": "2"}
KINDS["FILL"] = "32"
CHAINS = {"RH": "35", "SOL": "34"}
DIM = "2"
NOMINAL = {"gave": 16, "got": 19, "who": 11, "note": 10, "where": 10, "key": 15}
# What each column of the header means, printed under it once when a listener starts.
MEANING = {
    "time": "seconds since this listener started",
    "ch": "RH for Robinhood Chain, SOL for Solana",
    "kind": "BUY SELL SWAP PAY FILL IN OUT OTHER",
    "gave": "what left the wallet",
    "got": "what arrived, big figures scaled: 1.752M",
    "wallet": "the user; … means cut, the sidecar has it whole",
    "venue": "the pool or router the trade ran through",
    "where": "block on Robinhood Chain, slot on Solana",
    "trade": "transaction#log, or the Solana signature",
}
JOINED = "this trade's other leg, and how long ago it went past"
SEPARATORS = 25  # the spaces and the arrow in one row, everything a column is not


def paint(text, code):
    """Colour already-padded text, or hand it back untouched into a pipe or NO_COLOR."""
    return f"\033[{code}m{text}\033[0m" if COLOUR and code else text


def cut(text, width):
    """Shorten to the column with an ellipsis. For the last cell, which needs no pad."""
    return text if len(text) <= width else text[: width - 1] + "…"


def fit(text, width, align="<"):
    """Pad to the column, cutting first when the value will not fit."""
    return f"{cut(text, width):{align}{width}}"


def amount(value):
    """`1751556.260175 INVEST` -> `1.752M INVEST`, `150.000000 USDC` -> `150 USDC`.

    A memecoin balance carried to six decimals pushes the symbol out of the column,
    and the symbol is the half you read. Big figures are scaled, small ones lose only
    their trailing zeros, and the exact number is in the sidecar either way. Anything
    that is not a figure — `an unnamed token` — comes back untouched.

    A Solana mint with no ticker arrives here in full, 44 characters of base58, and is
    cut to a prefix so the figure beside it still fits the column. That cut is for the
    eye only: the sidecar holds the mint whole, which is where you look it up.
    """
    number, _, symbol = value.partition(" ")
    try:
        size = float(number)
    except ValueError:
        return value
    if abs(size) >= 1e12:
        figure = f"{size / 1e12:.4g}T"
    elif abs(size) >= 1e9:
        figure = f"{size / 1e9:.4g}B"
    elif abs(size) >= 1e6:
        figure = f"{size / 1e6:.4g}M"
    elif abs(size) >= 1e3:
        figure = f"{size:,.0f}"
    else:
        figure = f"{size:.6f}".rstrip("0").rstrip(".")
    # Long enough for any ticker, short enough that a 44-character base58 mint cannot
    # push the figure out of the column. `fit()` trims the cell after this, but it trims
    # from the right, and the figure is the half that has to survive.
    return f"{figure} {cut(symbol, 12)}".rstrip()


def short(value):
    """`0x1234…7890`: enough of a hash to recognise it, not enough to wrap the row.

    A `#log` suffix survives, because that is what separates two trades in one bundle.
    The whole value is in the sidecar.
    """
    body, marker, suffix = value.partition("#")
    if len(body) > 11:
        body = f"{body[:6]}…{body[-4:]}"
    return body + marker + suffix


def place(where):
    """`blk 1234567` -> `b1234567`, `slot 298431027` -> `s298431027`."""
    word, _, number = where.partition(" ")
    return f"{word[:1]}{number}" if number else where


def widths(chains):
    """Nominal columns, given up in the order that costs least when the terminal is narrow."""
    columns = dict(NOMINAL)
    room = shutil.get_terminal_size().columns if ISATTY else PIPED
    overhead = SEPARATORS + (4 if chains else 0)
    # A venue and a block number shorten harmlessly, so they go first. An amount does
    # not: cut it far enough and the symbol goes with it, and the symbol is the half
    # worth reading. Below the last floor the row wraps, always at the same place.
    for field, floor in (("note", 6), ("where", 9), ("got", 15), ("gave", 12)):
        while sum(columns.values()) + overhead > room and columns[field] > floor:
            columns[field] -= 1
    return columns


def row(chains, when, chain, kind, gave, arrow, got, who, note, where, key):
    """Join padded cells with fixed separators, so a coloured row lines up anyway."""
    return f"{when}  " + (f"{chain} " if chains else "") + f"{kind} {gave} {arrow} {got}  {who}  {note} {where} {key}"


def legend(chains):
    """A key to the header, printed once, because nine columns of shorthand need one.

    Laid out in two columns so it costs five lines rather than nine, and drops `ch`
    when there is only one chain to name. Only a two-chain feed joins legs, so only
    a two-chain feed is told what the continuation line under a row means.
    """
    keys = [k for k in MEANING if k != "ch" or chains]
    label = max(len(k) for k in keys)
    entries = [f"{k:<{label}}  {MEANING[k]}" for k in keys]
    half = (len(entries) + 1) // 2
    left, right = entries[:half], entries[half:]
    column = max(len(e) for e in left) + 4
    right += [""] * (len(left) - len(right))
    lines = [paint(f"  {a:<{column}}{b}".rstrip(), DIM) for a, b in zip(left, right, strict=True)]
    if chains:
        lines.append(paint(f"  {'└':<{label}}  {JOINED}", DIM))
    return lines


def header(columns, chains):
    return paint(
        row(
            chains,
            f"{'time':>7}",
            fit("ch", 3),
            fit("kind", 5),
            fit("gave", columns["gave"], ">"),
            "  ",
            fit("got", columns["got"]),
            fit("wallet", columns["who"]),
            fit("venue", columns["note"]),
            fit("where", columns["where"]),
            "trade",
        ),
        DIM,
    )


def start(script, *chains):
    """Print the header, open the sidecar, and hand back `(show, done)`.

    Name every chain the caller emits. The chain column appears only when there is
    more than one, so a single-chain listener never passes a chain per row.
    """
    both = len(chains) > 1
    columns = widths(both)
    RUNS.mkdir(exist_ok=True)
    # UTC, because `run` is this stem and every row's `at` is UTC: stamping the name in
    # local time puts a whole timezone offset between the two timestamps in one record.
    path = RUNS / f"{script}-{datetime.now(UTC):%Y%m%d-%H%M%S}.jsonl"
    sidecar = path.open("w")
    began = time.time()
    state = {"rows": 0, "last": began}
    # One id per run, so rows concatenated from several files can still be told apart:
    # `t` restarts at zero in every file and the wall clock is only in the filename.
    run = path.stem

    def show(trade, chain=chains[0], tail=""):
        """Print one trade and record it in full. This is a listener's `emit`."""
        now = time.time()
        if state["rows"] and now - state["last"] > GAP:
            print()
        if state["rows"] and state["rows"] % REPEAT == 0:
            print(header(columns, both))
        state["rows"], state["last"] = state["rows"] + 1, now
        # `app` is recorded rather than printed: the listeners filter on it, so it says
        # `fomo` on every row and a column of it would carry nothing.
        print(
            row(
                both,
                paint(f"+{now - began:.1f}s".rjust(7), DIM),
                paint(fit(chain, 3), CHAINS.get(chain, "")),
                paint(fit(trade.kind, 5), KINDS.get(trade.kind, "")),
                fit(amount(trade.gave), columns["gave"], ">"),
                "->" if trade.gave and trade.got else "  ",
                fit(amount(trade.got), columns["got"]),
                fit(short(trade.who), columns["who"]),
                fit(trade.note, columns["note"]),
                fit(place(trade.where), columns["where"]),
                cut(short(trade.key), columns["key"]),
            )
        )
        if tail:
            print(paint(f"{'':>7}  └ {tail}", DIM))
        record = {
            "t": round(now - began, 3),
            "at": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds"),
            "run": run,
            "chain": chain,
            **trade._asdict(),
        }
        sidecar.write(json.dumps(record) + "\n")
        sidecar.flush()

    def done():
        """Close the sidecar and say where the rows went in full."""
        sidecar.close()
        print(f"\n{state['rows']} rows in full: {path}")

    print(f"recording to {path}")
    for note in legend(both):
        print(note)
    print()
    print(header(columns, both))
    return show, done
