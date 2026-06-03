#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-MIT
"""Deploy Igra exit contracts into an isolated devnet from explorer bytecode.

This is a staging/devnet helper. It pulls verified production runtime bytecode
from the Igra block explorer, deploys that runtime into a local chain, patches
the Mailbox immutable address where needed, and initializes the contracts for
test exits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPLORER_BASE_URL = "https://explorer.igralabs.com/api/v2/smart-contracts"

PRODUCTION_MAILBOX_PROXY = "0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7"
PRODUCTION_MAILBOX_IMPLEMENTATION = "0x3a464f746D23Ab22155710f44dB16dcA53e0775E"
PRODUCTION_MERKLE_TREE_HOOK = "0x75719C858e0c73e07128F95B2C466d142490e933"
PRODUCTION_KAS_EXIT_BRIDGE_IMPLEMENTATION = (
    "0x00d39E05A20b2C4f6D0D6CfC3C5718066B861334"
)
DEFAULT_GAS_PRICE_WEI = "2000000000"
DEFAULT_PRIORITY_GAS_PRICE_WEI = "1000000000"

# Minimal contract that satisfies Mailbox's hook/ISM contract-address checks.
# quoteDispatch(bytes,bytes) returns 0; postDispatch(bytes,bytes) is a no-op;
# hookType() returns 0.
NOOP_HOOK_RUNTIME = (
    "0x60003560e01c8063aaccd230146029578063086011b9146034578063e445e7dd"
    "1460365760006000fd5b600060005260206000f35b005b600060005260206000f3"
)


@dataclass(frozen=True)
class DeployedContracts:
    noop_hook: str
    mailbox: str
    merkle_tree_hook: str
    kas_exit_bridge: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--private-key", default=os.environ.get("DEPLOYER_PRIVATE_KEY"))
    parser.add_argument("--deployer", default=os.environ.get("DEPLOYER_ADDRESS"))
    parser.add_argument("--cast-bin", default=os.environ.get("CAST_BIN", "cast"))
    parser.add_argument(
        "--gas-price",
        default=os.environ.get("GAS_PRICE", DEFAULT_GAS_PRICE_WEI),
        help="Max fee/gas price in wei. Defaults to 2 gwei for Igra devnet.",
    )
    parser.add_argument(
        "--priority-gas-price",
        default=os.environ.get(
            "PRIORITY_GAS_PRICE",
            DEFAULT_PRIORITY_GAS_PRICE_WEI,
        ),
        help="Priority fee in wei. Defaults to 1 gwei to satisfy Igra gas validation.",
    )
    parser.add_argument(
        "--gas-limit",
        default=os.environ.get("GAS_LIMIT"),
        help="Explicit gas limit. Useful on isolated Igra devnets where eth_estimateGas is disabled.",
    )
    parser.add_argument("--timeout", default=os.environ.get("CAST_SEND_TIMEOUT"))
    parser.add_argument(
        "--confirmations",
        type=int,
        default=int(os.environ.get("CAST_CONFIRMATIONS", "1")),
    )
    parser.add_argument("--legacy", action="store_true", default=os.environ.get("LEGACY_TX") == "1")
    parser.add_argument("--explorer-base-url", default=EXPLORER_BASE_URL)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chain-id", type=int, default=38833)
    parser.add_argument("--throttle-window-blocks", type=int, default=1000)
    parser.add_argument("--throttle-max-exits", type=int, default=1000)
    parser.add_argument(
        "--throttle-max-unlock-sompi",
        type=int,
        default=1_000_000_000_000_000,
    )
    parser.add_argument("--min-exit-sompi", type=int, default=1)
    parser.add_argument("--max-exit-sompi", type=int, default=1_000_000_000_000_000)
    args = parser.parse_args()

    if not args.private_key:
        parser.error("--private-key or DEPLOYER_PRIVATE_KEY is required")
    if not args.deployer:
        parser.error("--deployer or DEPLOYER_ADDRESS is required")

    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    mailbox_meta = fetch_contract_metadata(
        args.explorer_base_url,
        PRODUCTION_MAILBOX_IMPLEMENTATION,
        artifact_dir / "mailbox.implementation.explorer.json",
    )
    merkle_meta = fetch_contract_metadata(
        args.explorer_base_url,
        PRODUCTION_MERKLE_TREE_HOOK,
        artifact_dir / "merkle-tree-hook.explorer.json",
    )
    bridge_meta = fetch_contract_metadata(
        args.explorer_base_url,
        PRODUCTION_KAS_EXIT_BRIDGE_IMPLEMENTATION,
        artifact_dir / "kas-exit-bridge.implementation.explorer.json",
    )

    noop_hook = deploy_runtime(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        NOOP_HOOK_RUNTIME,
        "NoopHook",
    )

    mailbox = deploy_runtime(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        deployed_bytecode(mailbox_meta),
        "Mailbox",
    )
    merkle_tree_hook = deploy_runtime(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        patch_immutable_mailbox(deployed_bytecode(merkle_meta), mailbox),
        "MerkleTreeHook",
    )
    kas_exit_bridge = deploy_runtime(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        patch_immutable_mailbox(deployed_bytecode(bridge_meta), mailbox),
        "KasExitBridge",
    )

    cast_send(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        [
            mailbox,
            "initialize(address,address,address,address)",
            args.deployer,
            noop_hook,
            merkle_tree_hook,
            noop_hook,
        ],
        "initialize Mailbox",
    )
    cast_send(
        args.cast_bin,
        args.rpc_url,
        args.private_key,
        tx_options(args),
        [
            kas_exit_bridge,
            "initialize(address,uint32,uint32,uint64,uint64,uint64)",
            args.deployer,
            str(args.throttle_window_blocks),
            str(args.throttle_max_exits),
            str(args.throttle_max_unlock_sompi),
            str(args.min_exit_sompi),
            str(args.max_exit_sompi),
        ],
        "initialize KasExitBridge",
    )

    contracts = DeployedContracts(
        noop_hook=noop_hook,
        mailbox=mailbox,
        merkle_tree_hook=merkle_tree_hook,
        kas_exit_bridge=kas_exit_bridge,
    )
    deployment = {
        "schema": "igra.devnet.exit-contracts-from-explorer.v1",
        "rpcUrl": args.rpc_url,
        "chainId": args.chain_id,
        "deployer": checksumish(args.deployer),
        "explorerBaseUrl": args.explorer_base_url,
        "productionSources": {
            "mailboxProxy": PRODUCTION_MAILBOX_PROXY,
            "mailboxImplementation": PRODUCTION_MAILBOX_IMPLEMENTATION,
            "merkleTreeHook": PRODUCTION_MERKLE_TREE_HOOK,
            "kasExitBridgeImplementation": PRODUCTION_KAS_EXIT_BRIDGE_IMPLEMENTATION,
        },
        "contracts": {
            "noopHook": contracts.noop_hook,
            "mailbox": contracts.mailbox,
            "merkleTreeHook": contracts.merkle_tree_hook,
            "kasExitBridge": contracts.kas_exit_bridge,
        },
        "initialization": {
            "mailbox": {
                "owner": checksumish(args.deployer),
                "defaultIsm": contracts.noop_hook,
                "defaultHook": contracts.merkle_tree_hook,
                "requiredHook": contracts.noop_hook,
            },
            "kasExitBridge": {
                "owner": checksumish(args.deployer),
                "throttleWindowBlocks": args.throttle_window_blocks,
                "throttleMaxExitsPerWindow": args.throttle_max_exits,
                "throttleMaxUnlockAmountPerWindow": args.throttle_max_unlock_sompi,
                "minExitAmountSompi": args.min_exit_sompi,
                "maxExitAmountSompi": args.max_exit_sompi,
            },
        },
        "transactionOptions": {
            "gasLimit": args.gas_limit,
            "gasPrice": args.gas_price,
            "priorityGasPrice": args.priority_gas_price,
            "timeout": args.timeout,
            "confirmations": args.confirmations,
            "legacy": args.legacy,
        },
        "notes": [
            "Contracts are deployed from verified runtime bytecode fetched from the Igra explorer.",
            "Mailbox immutables in MerkleTreeHook and KasExitBridge are patched to this devnet Mailbox.",
            "This helper deploys direct runtimes for isolated staging tests; production uses the canonical proxy addresses.",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(deployment, indent=2, sort_keys=True) + "\n")
    print(json.dumps(deployment, indent=2, sort_keys=True))
    return 0


def fetch_contract_metadata(base_url: str, address: str, out: Path) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{address}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read().decode()
    data = json.loads(payload)
    if not data.get("deployed_bytecode"):
        raise RuntimeError(f"explorer response for {address} has no deployed_bytecode")
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def deployed_bytecode(metadata: dict[str, Any]) -> str:
    value = metadata.get("deployed_bytecode")
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RuntimeError("invalid deployed_bytecode in explorer response")
    return value


def patch_immutable_mailbox(runtime_hex: str, dev_mailbox: str) -> str:
    old = normalize_hex(PRODUCTION_MAILBOX_PROXY)
    new = normalize_hex(dev_mailbox)
    runtime = normalize_hex(runtime_hex)
    count = runtime.count(old)
    if count < 1:
        raise RuntimeError("production Mailbox immutable not found in runtime bytecode")
    return "0x" + runtime.replace(old, new)


def deploy_runtime(
    cast_bin: str,
    rpc_url: str,
    private_key: str,
    tx_opts: list[str],
    runtime_hex: str,
    label: str,
) -> str:
    initcode = runtime_initcode(runtime_hex)
    completed = run(
        [
            cast_bin,
            "send",
            "--json",
            "--rpc-url",
            rpc_url,
            "--private-key",
            private_key,
            *tx_opts,
            "--create",
            initcode,
        ],
        f"deploy {label}",
    )
    address = parse_contract_address(completed.stdout)
    print(f"{label}: {address}", file=sys.stderr)
    return address


def cast_send(
    cast_bin: str,
    rpc_url: str,
    private_key: str,
    tx_opts: list[str],
    args: list[str],
    label: str,
) -> None:
    run(
        [
            cast_bin,
            "send",
            "--json",
            "--rpc-url",
            rpc_url,
            "--private-key",
            private_key,
            *tx_opts,
            *args,
        ],
        label,
    )


def tx_options(args: argparse.Namespace) -> list[str]:
    options: list[str] = []
    if args.legacy:
        options.append("--legacy")
    if args.gas_price:
        options.extend(["--gas-price", args.gas_price])
    if args.priority_gas_price:
        options.extend(["--priority-gas-price", args.priority_gas_price])
    if args.gas_limit:
        options.extend(["--gas-limit", args.gas_limit])
    if args.timeout:
        options.extend(["--timeout", args.timeout])
    options.extend(["--confirmations", str(args.confirmations)])
    return options


def runtime_initcode(runtime_hex: str) -> str:
    runtime = bytes.fromhex(normalize_hex(runtime_hex))
    if len(runtime) > 0xFFFF:
        raise RuntimeError("runtime too large for PUSH2 initcode wrapper")
    # PUSH2 len, DUP1, PUSH1 0x0a, RETURNDATASIZE, CODECOPY,
    # RETURNDATASIZE, RETURN, then runtime bytes.
    prefix = bytes.fromhex("61") + len(runtime).to_bytes(2, "big") + bytes.fromhex(
        "80600a3d393df3"
    )
    return "0x" + (prefix + runtime).hex()


def run(cmd: list[str], label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def parse_contract_address(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("contractAddress", "contract_address"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("0x"):
                return checksumish(value)
    match = re.search(r"0x[a-fA-F0-9]{40}", stdout)
    if match:
        return checksumish(match.group(0))
    raise RuntimeError(f"could not parse deployed contract address from: {stdout}")


def normalize_hex(value: str) -> str:
    return value.removeprefix("0x").lower()


def checksumish(address: str) -> str:
    normalized = normalize_hex(address)
    if len(normalized) != 40:
        raise RuntimeError(f"invalid address: {address}")
    return "0x" + normalized


if __name__ == "__main__":
    raise SystemExit(main())
