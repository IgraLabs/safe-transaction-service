# Kaspa Multisig Staging Runbook

This documents the isolated staging deployment used to verify the Kaspa native wallet integration.

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
