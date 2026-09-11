"""Is registry.json still true? Run this before you trust anything built on it.

Every address, event topic and token decimal in the repo comes from that one file, and
chains move: contracts get replaced, venues go quiet, a token turns out to have different
decimals than assumed. Nothing in the scripts or the docs notices on its own — a listener
keyed on a dead address simply goes silent and looks like a quiet market.

This re-reads the registry's contracts, venues, event topics and token decimals against
the live chain and prints PASS or FAIL per claim: the account, intent and aggregation
contracts still have code, the EOAs among them are still EOAs, every venue factory and the
V4 pool manager and both non-AMM venues are alive, USDG really is 6 decimals, each
registered event still fires, and the wallet fingerprint still separates a FOMO wallet from
a lookalike and still matches the one shared/robinhood.py hands the listeners. The market
survey and offchain sections are descriptive and are not checked. Set SOLANA_MAINNET_HTTPS
and the Solana entries are checked too: the co-signer is still busy and every router FOMO
uses is still an executable program.

A failure means the registry is stale, not that the chain is broken. Fix the file, using
event_inventory.py to find what replaced the missing piece.

Needs KNOWN_FOMO_WALLET in .env as well as the endpoints, because the fingerprint claim is
only testable against a wallet known to carry it.

Reference: docs/07-addresses.md.

    uv run scripts/maintenance/verify_registry.py
"""

import json
import sys
from pathlib import Path

import requests
from web3 import Web3

# This runs from scripts/maintenance/ but imports shared/ from scripts/, so the path goes
# on before shared/ is imported. registry.json is beside this file.
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import env
from shared.robinhood import FOMO_CODE

w3 = Web3(Web3.HTTPProvider(env.endpoint("ROBINHOOD_MAINNET_HTTPS")))
# Read up front, so a missing variable stops the run before it prints half a report.
KNOWN_FOMO = env.endpoint("KNOWN_FOMO_WALLET")
SOLANA = env.optional("SOLANA_MAINNET_HTTPS")
reg = json.loads((Path(__file__).parent / "registry.json").read_text())

ok = bad = 0


def check(label, condition, detail=""):
    global ok, bad
    if condition:
        ok += 1
        print(f"  PASS  {label}")
    else:
        bad += 1
        print(f"  FAIL  {label}  {detail}")


print(f"chain id {w3.eth.chain_id}, head {w3.eth.block_number}\n")

print("-- contracts have code --")
for group in ("accountLayer", "intentLayer", "aggregationLayer"):
    for name, e in reg[group].items():
        if not isinstance(e, dict) or "address" not in e:
            continue
        size = len(w3.eth.get_code(Web3.to_checksum_address(e["address"])))
        expect_eoa = "EOA" in e.get("what", "")
        check(
            f"{group}.{name} {'is EOA' if expect_eoa else 'has code'}",
            (size == 0) if expect_eoa else (size > 0),
            f"codesize={size}",
        )

print("\n-- venues --")
for f in reg["venues"]["factories"]:
    size = len(w3.eth.get_code(Web3.to_checksum_address(f["address"])))
    check(f"factory {f['address'][:12]}… has code", size > 0, f"codesize={size}")
# The V4 pool manager and the two non-AMM venues carry real FOMO flow and are named in
# the address book, so they belong in the check rather than only in the file.
manager = reg["venues"]["uniswapV4PoolManager"]["address"]
size = len(w3.eth.get_code(Web3.to_checksum_address(manager)))
check("uniswapV4PoolManager has code", size > 0, f"codesize={size}")
for address, entry in reg["venues"]["nonUniswapVenues"].items():
    if address.startswith("_"):
        continue
    size = len(w3.eth.get_code(Web3.to_checksum_address(address)))
    check(f"non-AMM venue {address[:12]}… has code", size > 0, f"codesize={size}")
    check(
        f"non-AMM venue {address[:12]}… codesize still {entry['codesize']}",
        size == entry["codesize"],
        f"chain says {size}",
    )

print("\n-- token decimals --")
abi = [
    {"name": "decimals", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view"}
]
for sym in ("USDG", "WETH"):
    t = reg["tokens"][sym]
    live = w3.eth.contract(address=Web3.to_checksum_address(t["address"]), abi=abi).functions.decimals().call()
    check(f"{sym} decimals == {t['decimals']}", live == t["decimals"], f"chain says {live}")
for t in reg["tokens"]["lookalikes_DO_NOT_TRADE"]:
    live = w3.eth.contract(address=Web3.to_checksum_address(t["address"]), abi=abi).functions.decimals().call()
    check(f"lookalike {t['symbol']} decimals == {t['decimals']}", live == t["decimals"], f"chain says {live}")

print("\n-- every topic is the keccak of the signature recorded beside it --")
# A recorded topic and a recorded signature can disagree, and nothing else notices: the
# listeners match on the topic and never hash the ABI, so a wrong signature stays correct
# in every run and burns whoever derives their own filter from this file.
signatures = {t: m["abi"] for t, m in reg["events"].items() if "abi" in m}
signatures |= {t: v.split(" - ")[0] for t, v in reg["venues"]["swapEventShapes"].items()}
for topic, abi in signatures.items():
    computed = w3.keccak(text=abi).hex()
    computed = computed if computed.startswith("0x") else "0x" + computed
    check(f"{abi[:52]}", computed == topic, f"hashes to {computed[:18]}…")

print("\n-- every registered event actually fires --")
addr_of = {
    "entryPoint": reg["accountLayer"]["entryPoint"]["address"],
    "relayDepository": reg["intentLayer"]["relayDepository"]["address"],
    "relayRouter": reg["intentLayer"]["relayRouter"]["address"],
    "relayExecutor": reg["intentLayer"]["relayExecutor"]["address"],
}
head = w3.eth.block_number
for topic, meta in reg["events"].items():
    at = meta["at"].split(",")[0].strip()
    if at not in addr_of:
        # Two different reasons to skip, and calling both "any-address" hides one: an
        # event recorded against another chain is not checkable here at all, while a
        # Transfer has no single address to look it up at.
        why = f"on {meta['chain']}, not this chain" if meta.get("chain") else "any-address event"
        print(f"  SKIP  {meta.get('abi', '?')[:44]} ({why})")
        continue
    logs = w3.eth.get_logs(
        {"fromBlock": head - 3000, "toBlock": head, "address": Web3.to_checksum_address(addr_of[at]), "topics": [topic]}
    )
    check(f"{at}: {meta.get('abi', '?')[:46]}", len(logs) > 0, "no occurrences in 3000 blocks")

print("\n-- FOMO wallet fingerprint discriminates --")
recorded = reg["accountLayer"]["fomoWalletFingerprint"]["test"].split("== ")[1]
fp = bytes.fromhex(recorded.removeprefix("0x"))
# The listeners test wallets against shared/robinhood.py, not against this file. If the
# two drift apart every listener silently stops recognising a FOMO wallet, so the drift
# is worth a check of its own.
check(f"shared/robinhood.py agrees with the recorded fingerprint {recorded[:14]}…", recorded == FOMO_CODE)
check("a known FOMO wallet matches the fingerprint", w3.eth.get_code(Web3.to_checksum_address(KNOWN_FOMO)) == fp)
duster = "0x3F48ad1d49643ED1FCec16cc44Bd6bE8Dbfe48eE"
check("duster wallet does NOT match", w3.eth.get_code(Web3.to_checksum_address(duster)) != fp)

print("\n-- solana --")
if not SOLANA:
    print("  SKIP  set SOLANA_MAINNET_HTTPS to check the Solana entries")
else:

    def solana(method, params):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return requests.post(SOLANA, json=body, timeout=30).json().get("result")

    sol = reg["solana"]
    recent = solana("getSignaturesForAddress", [sol["coSigner"]["address"], {"limit": 100}]) or []
    check(
        "the co-signer is still signing FOMO transactions",
        len(recent) == 100,
        f"only {len(recent)} recent signatures",
    )
    for program, what in sol["routers"].items():
        info = solana("getAccountInfo", [program, {"encoding": "base64"}])
        live = (info or {}).get("value") or {}
        check(f"router {what} is an executable program", bool(live.get("executable")), f"{program[:12]}…")
    deposit = solana("getAccountInfo", [sol["relayDepositProgram"]["address"], {"encoding": "base64"}])
    check(
        "Relay's deposit program is executable",
        bool(((deposit or {}).get("value") or {}).get("executable")),
    )
    # The solver that pays out a Robinhood Chain sell. Its address is the part of this
    # entry that can go stale, because Relay's solvers rotate: what it must still be
    # doing is signing transfers that carry an order id in a memo.
    settlement = sol["settlement"]
    payouts = solana("getSignaturesForAddress", [settlement["solver"], {"limit": 20}]) or []
    memos = 0
    for signature in [s["signature"] for s in payouts[:8]]:
        tx = solana("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        programs = {i.get("programId") for i in tx["transaction"]["message"]["instructions"]}
        memos += bool(programs & set(settlement["memoPrograms"]))
    check(
        "the settlement solver is still paying out with memos",
        memos >= 4,
        f"{memos} of the last 8 carried a memo — solvers rotate, re-verify the address",
    )

print(f"\n{ok} passed, {bad} failed")
raise SystemExit(1 if bad else 0)
