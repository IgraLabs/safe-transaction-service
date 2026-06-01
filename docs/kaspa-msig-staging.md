# Kaspa Multisig Staging Runbook

This documents the isolated staging deployment used to verify the Kaspa native wallet integration.

## Igra Exit Proposal Staging

The current Igra exit proposal-builder test stack is separate from the original
wallet-only stack below.

- Server: `roman@stage-roman.igralabs.com`
- Directory: `/home/roman/kaspa-exit-builder-codex-20260601-123722`
- Compose project: `kaspa-exit-builder-codex-20260601-123722`
- Docker network: `kaspa-exit-builder-codex-20260601-123722_msig`
- Private subnet: `172.31.92.0/24`
- EL RPC host ports: `127.0.0.1:39455` HTTP, `127.0.0.1:39456` WS

This stack runs an isolated Kaspa devnet plus Igra EL. Igra L2 writes must use
Foundry's Igra transport, not the normal EL txpool path: Foundry signs the raw
L2 transaction, embeds it in a Kaspa transaction payload with the Igra protocol
header, mines the configured `97b1` txid prefix, signs the Kaspa transaction,
and submits it to kaspad. The EL side only observes the transaction after
kaspad/ATAN/Viaduct drive the Igra block.

Current devnet consensus override:

```text
--devnet-finality-depth=300
--devnet-pruning-depth=3150
```

The pruning depth must be high enough to retain Kaspa's DAA difficulty window,
but low enough for Viaduct to see a pruning point quickly in an isolated test
chain. The first EL blocks start only after the initial pruning/finality window.

Use the explicit Igra Foundry environment for deployment and `requestExit`
transactions:

```bash
export FOUNDRY_IGRA_ENABLED=true
export FOUNDRY_IGRA_EL_RPC_URL=http://127.0.0.1:39455
export FOUNDRY_IGRA_KASPA_RPC_URL=grpc://172.31.92.10:16610
export FOUNDRY_IGRA_EXPECTED_EL_CHAIN_ID=38833
export FOUNDRY_IGRA_KASPA_NETWORK=devnet
export FOUNDRY_IGRA_TX_ID_PREFIX=97b1
export FOUNDRY_IGRA_EL_RECEIPT_TIMEOUT_SECS=900
export FOUNDRY_IGRA_MINING_TIMEOUT_SECS=300
```

The helper `scripts/submit_igra_exit_contracts_async.py` deploys the devnet
Mailbox, MerkleTreeHook, KasExitBridge, and initialization sequence from
production explorer bytecode through that payload path. It submits the nonce
sequence asynchronously; wait for one Igra finality/pruning window, then verify
contract code at the deterministic addresses before creating exits.

## Deployment

- Server: `roman@stage-roman.igralabs.com`
- Directory: `/home/roman/kaspa-msig-codex-20260601-120403`
- Compose project: `kaspa-msig-codex-20260601-120403`
- Docker network: `kaspa-msig-codex-20260601-120403_msig`
- Private subnet: `172.31.91.0/24`
- Host ports: none published

Repository revisions used:

- `safe-transaction-service`: `0b4c3342` on `kaspa-native-wallet-integration`
- `kaspad`: `4edecdd0` on `kaspa-pst-helper`
- `kaspa-miner`: `1963b5b`

The deployment uses rusty-kaspa `kaspad` in devnet mode with `--utxoindex` and `--enable-unsynced-mining`. `rothschild` was built as a deployment-local devnet binary because the upstream tool defaults to the testnet address prefix. The rusty-kaspa source on staging was restored after that build.

## Services

- `kaspad`: rusty-kaspa devnet node
- `kaspa-miner`: IgraLabs kaspa-miner, mining to the generated faucet key
- `wallet-tools`: old Go `kaspawallet`, `kaspa-pst`, the fixture helper, and devnet `rothschild`
- `safe-db`: project-scoped Postgres volume
- `safe-redis`
- `safe-api`: Safe Transaction Service with the Kaspa app and `kaspa-pst`

## Run

From the deployment directory:

```bash
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 scripts/run-stage-e2e.sh
```

The script:

1. Builds the isolated images.
2. Creates or reuses three old-kaspawallet multisig key files under `state/wallets/`.
3. Uses the signer whose `cosignerIndex` is `0` as the canonical custody-address wallet.
4. Starts the miner and records `faucetMiningStartedBlock`.
5. Waits for `ROTHSCHILD_MIN_BLOCKS` blocks after that point. Default is `1150`.
6. Funds the custody address with devnet `rothschild`.
7. Creates or reuses the Safe Transaction Service federation.
8. Creates a spend proposal from the canonical signer.
9. Signs with signer `0` and the signer whose `cosignerIndex` is `1`.
10. Broadcasts through the Safe Transaction Service Kaspa endpoint.
11. Verifies the recipient balance.

The latest successful result is stored at:

```bash
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 docker compose exec -T wallet-tools cat /work/results/e2e-result.json
```

Successful staging result:

```json
{
  "ok": true,
  "federationId": "a2b37094-2201-4ecd-bfc0-58b55660daa9",
  "proposalHash": "a3672d2f61f30c1765acbcb45b0666e6ee0914d72905d31a4e117efe51ac21f1",
  "custodyAddress": "kaspadev:pzhse688qxkc72d43g8y9us4sactgggezx09eaecpxh8mdcjas98v6nxgv6nc",
  "recipientAddress": "kaspadev:qrn6wrt3zv2s320jav0apmm54ecvh94tqr4qrzvpzm7pxymdcf4gq6dyhzxnt",
  "canonicalSigner": "signer-0",
  "secondSigner": "signer-2",
  "broadcastTxIds": [
    "c1c337918c25b142a39d8bcd9b02533bca9a196a591df2b7b7ca793dfed7a006"
  ],
  "custodyBalanceSompi": 308000887600,
  "recipientBalanceSompi": 200000000
}
```

## Operational Notes

- Keep `COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403` on every compose command to avoid touching other staging services.
- The stack does not publish host ports. Use `docker compose exec` inside the deployment directory for inspection.
- The miner is CPU-bound. Stop it after test runs unless someone is actively inspecting the live devnet.
- After the successful run above, the project stack was stopped to avoid consuming staging CPU. The deployment directory and bind-mounted `state/` remain on disk.
- `docker compose down -v` removes only the project-scoped containers, network, and Postgres volume. It leaves the bind-mounted `state/` directory in place.
- To remove all deployment state after teardown, delete `/home/roman/kaspa-msig-codex-20260601-120403`.

Useful commands:

```bash
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 docker compose ps
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 docker compose logs --tail=120 safe-api wallet-tools kaspa-miner kaspad
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 docker compose exec -T wallet-tools kaspa-msig-fixture dag-info --rpc kaspad:16610
COMPOSE_PROJECT_NAME=kaspa-msig-codex-20260601-120403 docker compose stop
```

## Implementation Findings

- The service uses Native wallet integration: wallets submit old-kaspawallet PST bundles, the service stores proposals, merges signer bundles, and broadcasts after threshold.
- The helper accepts common network aliases such as `devnet`; old kaspad internally names devnet as `kaspa-devnet`.
- The helper must preserve each input's `SigOpCount` while merging. Old `kaspawallet sign` sets this field, and losing it causes node rejection with `sig op count exceeds passed limit of 0`.
- Broadcast relies on the node as the final consensus validator, matching old `kaspawallet broadcast` behavior.
