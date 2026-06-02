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

Current working devnet consensus override:

```text
--devnet-finality-depth=5000
--devnet-pruning-depth=30125
```

The pruning depth must be high enough to retain Kaspa's DAA difficulty window
and must satisfy rusty-kaspa's devnet override constraint:
`pruning % finality` must be in `(124, finality - 124)`. `30125 % 5000 = 125`,
so the profile is accepted and remained healthy through the full e2e run.

Do not use the earlier experimental profiles:

- `300/3150` failed around DAA 5554 with `DAA window data has only 138 entries`.
- `700/10000` failed around DAA 12816 with `DAA window data has only 320 entries`.

With `finality=5000`, Igra EL blocks did not start immediately at DAA 5000.
In the successful run, EL started advancing after roughly two finality windows
(`virtualDaaScore` just above 10000). Also note that rusty-kaspa devnet enforces
1000 DAA coinbase maturity, so faucet/miner rewards cannot fund custody until
the chain is past that point.

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
sequence asynchronously; wait until `eth_blockNumber` advances from `0x0`
before verifying contract code at the deterministic addresses. With the current
`5000/30125` profile, this was roughly two finality windows.

Successful `daa12` Igra exit e2e:

```text
safe-transaction-service branch: kaspa-native-wallet-integration
staging root: /home/roman/kaspa-exit-builder-codex-20260601-123722
compose project: kaspa-exit-builder-codex-20260601-123722
safe API internal URL: http://safe-api:8888
EL host RPC: http://127.0.0.1:39455
Kaspa RPC inside compose: kaspad:16610
```

Contracts:

```text
NoopHook:       0x5fbdb2315678afecb367f032d93f642f64180aa3
Mailbox:        0xe7f1725e7734ce288f8367e1bb143e90bb3f0512
MerkleTreeHook: 0x9fe46736679d2d9a65f0992f2272de9f3c7fa6e0
KasExitBridge:  0xcf7ed3acca5a467e9e704c703e8d87f634fb0fc9
```

Custody and exit:

```text
federation: b62e5c9e-61cb-4c1b-90fc-7c1b334fbee0
custody:    kaspadev:pz0xf48dhcf7sj2sulm4ww6vawyyv7zs0mjdf3c3u0jwqlktx2m022jufl05l
recipient:  kaspadev:qqe937usn505jmqlttyq8q9nwv0n2junr37gaeammr6fjuw39ucqwvwucpe0s

funding outpoint: dbae383515c614d18dfed14df0d993c79342d2b9ff56ea1d2a6d16fa5ac2e2b3:0
requestExit tx:   0x3c1b10536a9c33b7573614386d09aa3723aea99d97abd00d12930049b3850484
requestExit block: 0x4a0 (1184)
message id:       0x1c38ed250567a50f51be281425336f583b745cf7adad43aeacb3ed9ca9119a07
exit bundle:      /work/igra/exits/devnet-exit-daa12.bundle
builder config:   /work/igra/exits/builder-config-daa12.json
```

Safe-service proposal:

```text
proposal hash: 71c209303a09dd0e62fdfc02e4e9c9621573532c4549051df613a9fb1539b8fd
evidence hash: 7265592f07ba302bd33fe44f811dddb9f92f43e4d33963414799581a2cb6688f
exit batch:    13fa8a2a-c28a-4587-af79-67a46ca5cec0
Kaspa tx id:   97b1e08af5d2ab619bfd93362d2395aef7e4759ad1b66b429e2b82005b980269
signers:       signer-2 + signer-1
status:        broadcasted
```

Final Kaspa balances:

```text
recipient balance: 100000000 sompi
custody change:    2399000000 sompi
fee:               1000000 sompi
landing DAA:       31038
```

## Laptop Devnet Bootstrap Shape

The reusable laptop deployment should live in `igra-orchestra` (or
`igra-orchestra-public`) rather than in this service repository. The service
repo should be one component in the stack.

Minimum components:

- `execution-layer`: Igra EL from `reth-private`.
- `kaspad`: `rusty-kaspa-private` devnet with Igra enabled and the stable
  finality/pruning profile above.
- `kaspa-miner`: IgraLabs miner, defaulting to 2 local threads for bootstrap.
- `wallet-tools`: old Go `kaspawallet`, `kaspa-pst`, fixture helpers, and any
  local faucet/funding helper.
- `safe-db`, `safe-redis`, `safe-api`: Safe Transaction Service.

Recommended script phases:

```text
devnet-up
wait-kaspa-daa 1000
fund-custody
wait-igra-live
deploy-exit-contracts
request-exit
build-exit-bundle
build-kaspa-proposal
sign-and-broadcast
verify-balances
```

`wait-igra-live` should poll both Kaspa DAA and `eth_blockNumber`. With
`finality=5000`, expect `eth_blockNumber` to stay at `0x0` until around DAA
10000. This is normal because Kaspa drives Igra block production.

Repository ownership for laptop reuse:

- `safe-transaction-service`: service APIs, exit proposal-builder, evidence
  storage, tests, and service-side docs.
- `foundry`: Igra transport maturity fix so payload transactions do not choose
  immature coinbase UTXOs for Kaspa fees.
- `rusty-kaspa-private`: devnet finality/pruning override support and the stable
  profile.
- `igra-orchestra` / `igra-orchestra-public`: compose files, env generation,
  bootstrap scripts, and one-command e2e flow.
- `kaspa-miner`: no code change required for this run; pin a known-good ref in
  orchestra.
- `reth-private`: no code change required for this run unless orchestra needs
  the exact EL boot/genesis handoff scripted.

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
