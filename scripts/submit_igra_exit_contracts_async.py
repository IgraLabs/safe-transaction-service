#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-MIT
"""Submit Igra exit contract deployment transactions asynchronously.

This staging helper uses the Igra-enabled Foundry transport but does not wait
for execution-layer receipts after each transaction. It submits the deployment
and initialization nonce sequence up front, then operators can wait one Igra
finality window and verify the deterministic contract addresses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from deploy_igra_exit_contracts_from_explorer import (
    DEFAULT_GAS_PRICE_WEI,
    DEFAULT_PRIORITY_GAS_PRICE_WEI,
    EXPLORER_BASE_URL,
    NOOP_HOOK_RUNTIME,
    PRODUCTION_KAS_EXIT_BRIDGE_IMPLEMENTATION,
    PRODUCTION_MAILBOX_IMPLEMENTATION,
    PRODUCTION_MAILBOX_PROXY,
    PRODUCTION_MERKLE_TREE_HOOK,
    checksumish,
    deployed_bytecode,
    fetch_contract_metadata,
    patch_immutable_mailbox,
    run,
    runtime_initcode,
)


DEFAULT_GAS_LIMIT = "50000000"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--private-key", default=os.environ.get("DEPLOYER_PRIVATE_KEY"))
    parser.add_argument("--deployer", default=os.environ.get("DEPLOYER_ADDRESS"))
    parser.add_argument("--cast-bin", default=os.environ.get("CAST_BIN", "cast"))
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chain-id", type=int, default=38833)
    parser.add_argument("--start-nonce", type=int, default=0)
    parser.add_argument(
        "--resume-at-nonce",
        type=int,
        help=(
            "Skip deployment steps below this nonce while preserving addresses "
            "computed from --start-nonce"
        ),
    )
    parser.add_argument("--gas-limit", default=os.environ.get("GAS_LIMIT", DEFAULT_GAS_LIMIT))
    parser.add_argument("--gas-price", default=os.environ.get("GAS_PRICE", DEFAULT_GAS_PRICE_WEI))
    parser.add_argument(
        "--priority-gas-price",
        default=os.environ.get("PRIORITY_GAS_PRICE", DEFAULT_PRIORITY_GAS_PRICE_WEI),
    )
    parser.add_argument("--explorer-base-url", default=EXPLORER_BASE_URL)
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

    nonce = args.start_nonce
    noop_hook = compute_create_address(args, nonce)
    mailbox = compute_create_address(args, nonce + 1)
    merkle_tree_hook = compute_create_address(args, nonce + 2)
    kas_exit_bridge = compute_create_address(args, nonce + 3)

    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if should_submit(args, nonce, "NoopHook", skipped):
        pending.append(
            submit_create(args, nonce, runtime_initcode(NOOP_HOOK_RUNTIME), "NoopHook")
        )
    if should_submit(args, nonce + 1, "Mailbox", skipped):
        pending.append(
            submit_create(
                args,
                nonce + 1,
                runtime_initcode(deployed_bytecode(mailbox_meta)),
                "Mailbox",
            )
        )
    if should_submit(args, nonce + 2, "MerkleTreeHook", skipped):
        pending.append(
            submit_create(
                args,
                nonce + 2,
                runtime_initcode(
                    patch_immutable_mailbox(deployed_bytecode(merkle_meta), mailbox)
                ),
                "MerkleTreeHook",
            )
        )
    if should_submit(args, nonce + 3, "KasExitBridge", skipped):
        pending.append(
            submit_create(
                args,
                nonce + 3,
                runtime_initcode(
                    patch_immutable_mailbox(deployed_bytecode(bridge_meta), mailbox)
                ),
                "KasExitBridge",
            )
        )
    if should_submit(args, nonce + 4, "initialize Mailbox", skipped, to=mailbox):
        pending.append(
            submit_call(
                args,
                nonce + 4,
                mailbox,
                [
                    "initialize(address,address,address,address)",
                    args.deployer,
                    noop_hook,
                    merkle_tree_hook,
                    noop_hook,
                ],
                "initialize Mailbox",
            )
        )
    if should_submit(
        args,
        nonce + 5,
        "initialize KasExitBridge",
        skipped,
        to=kas_exit_bridge,
    ):
        pending.append(
            submit_call(
                args,
                nonce + 5,
                kas_exit_bridge,
                [
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
        )

    deployment = {
        "schema": "igra.devnet.exit-contracts-from-explorer.v1",
        "submissionMode": "igra-async-nonce-sequence",
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
            "noopHook": noop_hook,
            "mailbox": mailbox,
            "merkleTreeHook": merkle_tree_hook,
            "kasExitBridge": kas_exit_bridge,
        },
        "initialization": {
            "mailbox": {
                "owner": checksumish(args.deployer),
                "defaultIsm": noop_hook,
                "defaultHook": merkle_tree_hook,
                "requiredHook": noop_hook,
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
        "pendingTransactions": pending,
        "skippedTransactions": skipped,
        "transactionOptions": {
            "gasLimit": args.gas_limit,
            "gasPrice": args.gas_price,
            "priorityGasPrice": args.priority_gas_price,
            "startNonce": args.start_nonce,
            "resumeAtNonce": args.resume_at_nonce,
        },
        "notes": [
            "Transactions were submitted asynchronously through the Igra transport.",
            "Wait for the Igra finality window, then verify code exists at each deterministic address.",
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(deployment, indent=2, sort_keys=True) + "\n")
    print(json.dumps(deployment, indent=2, sort_keys=True))
    return 0


def compute_create_address(args: argparse.Namespace, nonce: int) -> str:
    completed = run(
        [
            args.cast_bin,
            "compute-address",
            "--nonce",
            str(nonce),
            args.deployer,
        ],
        f"compute address nonce {nonce}",
    )
    return parse_address(completed.stdout)


def submit_create(
    args: argparse.Namespace,
    nonce: int,
    initcode: str,
    label: str,
) -> dict[str, Any]:
    tx_hash = submit(args, nonce, ["--create", initcode], label)
    return {"label": label, "nonce": nonce, "txHash": tx_hash}


def submit_call(
    args: argparse.Namespace,
    nonce: int,
    to: str,
    call_args: list[str],
    label: str,
) -> dict[str, Any]:
    tx_hash = submit(args, nonce, [to, *call_args], label)
    return {"label": label, "nonce": nonce, "to": to, "txHash": tx_hash}


def should_submit(
    args: argparse.Namespace,
    nonce: int,
    label: str,
    skipped: list[dict[str, Any]],
    *,
    to: str | None = None,
) -> bool:
    if args.resume_at_nonce is None or nonce >= args.resume_at_nonce:
        return True
    item: dict[str, Any] = {"label": label, "nonce": nonce, "skipped": True}
    if to:
        item["to"] = to
    skipped.append(item)
    print(f"{label} nonce={nonce} skipped below resume-at-nonce")
    return False


def submit(
    args: argparse.Namespace,
    nonce: int,
    tx_args: list[str],
    label: str,
) -> str:
    completed = run(
        [
            args.cast_bin,
            "send",
            "--async",
            "--rpc-url",
            args.rpc_url,
            "--private-key",
            args.private_key,
            "--nonce",
            str(nonce),
            "--gas-limit",
            args.gas_limit,
            "--gas-price",
            args.gas_price,
            "--priority-gas-price",
            args.priority_gas_price,
            *tx_args,
        ],
        label,
    )
    tx_hash = parse_hash(completed.stdout)
    print(f"{label} nonce={nonce} tx={tx_hash}")
    return tx_hash


def parse_address(value: str) -> str:
    matches = re.findall(r"0x[a-fA-F0-9]{40}", value)
    if not matches:
        raise RuntimeError(f"could not parse address from: {value}")
    return checksumish(matches[-1])


def parse_hash(value: str) -> str:
    matches = re.findall(r"0x[a-fA-F0-9]{64}", value)
    if not matches:
        raise RuntimeError(f"could not parse tx hash from: {value}")
    return matches[-1]


if __name__ == "__main__":
    raise SystemExit(main())
