# Kaspa Exit Proposal Builder

This document captures the first implementation target for Igra L2 driven Kaspa
multisig proposals. The component observes finalized Igra L2 exit windows,
verifies the bridge events, builds an unsigned Kaspa PST, and stores enough
evidence for wallets to re-verify before signing.

The builder is not a wallet. It may know public kpubs, the threshold, the
canonical bridge address, and public chain data. It must not have Kaspa private
keys.

## Existing Sources

The current production methodology lives in the Igra exit tooling and runbooks:

- `/Users/user/Source/igra/kasExitBridge/auditExits.ts`
- `/Users/user/Source/igra/kasExitBridge/verifyExits.ts`
- `/Users/user/Source/igra/kasExitBridge/verifyExitArtifacts.ts`
- `/Users/user/Source/igra/kasExitBridge/runDelta.ts`
- `/Users/user/Source/igra/foundry/docs/dev/igra-mainnet-exit-bundle-operations.md`
- `/Users/user/Source/igra/foundry/docs/dev/igra-exit-unsigned-tx-runbook.md`
- `/Users/user/Source/igra/kas-bridge-history-data-till-block-4492799/docs/kas-exit-bridge-query-audit-methodology.md`

The real exit-35 directory name shows the active window convention:

```text
keb-from-7344000-to-7430399-20260525T200000Z.bundle
```

That is an 86,400-block window. `runDelta.ts` confirms the durable model: start
from the previous bundle checkpoint, scan `fromBlock..toBlock`, write
`derived/checkpoint.end.json`, then advance to `toBlock + 1`.

## Foundry Kaspa Transaction Creation

Foundry has two Kaspa transaction creation paths, and only one is appropriate
for the exit proposal builder.

The normal Igra L2 write path lives in
`/Users/user/Source/igra/foundry/crates/common/src/provider/igra_transport.rs`.
It builds a raw L2 payload transaction with protocol header `0x94`
(`version=0x9`, `txTypeId=0x4`), mines the 4-byte payload nonce until the Kaspa
txid matches the configured prefix, signs with a local Kaspa private key, and
broadcasts immediately. This is correct for ordinary `cast send` style Igra L2
transactions, but it is not suitable for the observer because it requires
private-key access.

The bridge exit path lives in
`/Users/user/Source/igra/foundry/crates/common/src/igra_exit.rs` and is exposed
through `cast igra build-exit` in
`/Users/user/Source/igra/foundry/crates/cast/src/cmd/igra.rs`. It builds an
unsigned old-kaspawallet `PartiallySignedTransaction` from public material:
locking UTXOs, verified exit requests, kpubs, threshold, change path, fee, and
network. Its protocol header is `0x93` (`version=0x9`, `txTypeId=0x3`), and the
payload is:

```text
0x93 || message_id_1 || ... || message_id_n || nonce_u32_be
```

This is the path the service should call first, either by shelling out to the
existing `cast igra build-exit` binary or by wrapping the same Rust function in a
small helper. The signer-side local verification path is already present as
`cast igra verify-exit`, with companion address checks in `derive-msig-address`,
`verify-msig-address`, `check-msig-path`, and `verify-bundle-integral`.

## Operational Architecture

The production flow has three independent roles:

- Safe Transaction Service stores federations, unsigned PST proposals, signer
  submissions, merged PST state, evidence, and broadcast results.
- Proposal Builder observes only the configured Igra chain and exact bridge
  contracts, verifies a closed exit window, builds one unsigned proposal, and
  posts it to Safe Transaction Service. It never has Kaspa private keys.
- Signer wallets poll Safe Transaction Service, fetch the unsigned PST and exit
  evidence, re-verify against their own Igra and Kaspa RPC endpoints, sign
  locally, and submit signed PST bundles back to Safe Transaction Service.

Runtime ownership should stay separated:

```text
proposal-builder  -> per federation/config; no private keys; creates proposals
safe-service      -> coordination/storage API; no trust decision for signers
signer wallet     -> signer-controlled verifier and signer; owns private keys
```

The Proposal Builder should run per federation, or as one daemon with strictly
separated per-federation configuration and state. Each federation config is an
allowlist: federation id, Igra chain id, bridge contract addresses, Mailbox,
MerkleTreeHook, Kaspa custody address, kpub set, threshold, ECDSA flag,
derivation path, txid prefix, and the last processed exit checkpoint. The
builder must not dynamically discover which federation an exit belongs to.

Safe Transaction Service is not a signing authority. It stores the proposal and
evidence, tracks quorum, accepts signer submissions, merges PST state, and
records broadcasts. It must be safe for signers to treat all data from
safe-service as untrusted input. A signer should sign only after its own wallet
verifier has reproduced the unsigned PST from public chain data and exact
federation config.

The execution step is permissionless at the service level after quorum:
whichever signer/operator is assigned can call the broadcast endpoint once the
proposal is `READY`. The service re-validates the merged signed PST immediately
before broadcast.

The Proposal Builder must be configured with an allowlist, not discovered
dynamically:

```text
Igra chain ID:        38833
Igra RPC:             configured RPC URL
KasExitBridge:        0x4bb88C213d3eD9dc4bae694f1bc1bF745903b2d0
Mailbox:              0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7
MerkleTreeHook:       0x75719C858e0c73e07128F95B2C466d142490e933
Kaspa network:        mainnet
Kaspa txid prefix:    97b1
Kaspa bridge address: kaspa:ppvnxxzm0rr37zpnwux2f2ntvfpr4uqdpm7zsvsztg3en92r7gs0wkmr72q9n
```

Any chain ID, contract address, mailbox, Merkle hook, bridge address, threshold,
ECDSA flag, or kpub mismatch is a hard failure before proposal creation.

## Signer Trust Boundary

Signer wallets hold the responsibility for user funds. They must not trust the
Proposal Builder, Safe Transaction Service, or another signer. Before adding a
signature, signer-side tooling must:

1. Fetch the proposal and `/api/v1/kaspa/exit-batches/{id}/evidence/`.
2. Recompute the evidence hash using canonical JSON and compare it to the
   proposal's `exit_evidence_hash`.
3. Verify the federation config against the local signer wallet: network, kpubs,
   threshold, ECDSA flag, custody address, script public key, and derivation
   path.
4. Verify the Igra evidence against the signer's own Igra RPC: chain id,
   contract code, finalized window, transaction receipts, logs, successful exit
   events, and Merkle checkpoint continuity.
5. Verify the Kaspa funding UTXOs against the signer's own Kaspa RPC: outpoint,
   amount, script public key, live unspent state, and coinbase maturity.
6. Rebuild the unsigned exit PST locally from verified exits, verified UTXOs,
   kpubs, threshold, change policy, fee, and txid-prefix payload nonce.
7. Compare the rebuilt PST bytes, unsigned txid, payload, outputs, fee, mass, and
   proposal hash to safe-service.
8. Sign only if every comparison is exact.

The expected wallet UX is a fail-closed command such as:

```bash
kaspawallet verify-exit-proposal \
  --safe-url https://safe.example \
  --proposal-hash <hash> \
  --keys-file signer.keys.json \
  --igra-rpc-url https://signer-owned-igra-rpc \
  --kaspa-rpc-url grpc://signer-owned-kaspa-rpc

kaspawallet sign-exit-proposal \
  --safe-url https://safe.example \
  --proposal-hash <hash> \
  --keys-file signer.keys.json \
  --igra-rpc-url https://signer-owned-igra-rpc \
  --kaspa-rpc-url grpc://signer-owned-kaspa-rpc
```

The second command must run the same verifier internally before decrypting keys
or producing a signed PST bundle. Raw `kaspawallet sign --transaction ...`
remains useful for generic multisig, but bridge signers should use the
exit-aware signing command for custody exits.

## Window Timing

The existing KEB automation uses deterministic block windows. In
`/Users/user/Source/igra/kasExitBridge/runDelta.ts`, the default delta is
`86_400` blocks. It can be overridden by `--delta-blocks`, `KEB_DELTA_BLOCKS`, or
`kasExitBridge.deltaBlocksDefault`.

For each run:

```text
fromBlock = previous.toBlock + 1
toBlock   = fromBlock + deltaBlocks - 1
start checkpoint block = fromBlock - 1
end checkpoint block   = toBlock
next window starts at  = toBlock + 1
```

The current script does not implement an additional finality delay; operators
choose a completed range. The service daemon should make that rule explicit:
only build when the Igra RPC reports that the selected `toBlock` is finalized, or
when `latest >= toBlock + l2ConfirmationBlocks` for RPCs without a reliable
`finalized` tag. For mainnet, the Foundry Igra profile uses `el_confirmations=12`
for ordinary submissions; that is a reasonable initial default for the Proposal
Builder confirmation margin unless Igra exposes a stronger finalized block tag.

## Real Exit-35 Fixture

The real local fixture is available at:

```text
/Users/user/Desktop/Igra/Bridge/exit-35
```

Exit-35 is an unsigned production bridge proposal that has not been signed or
broadcast.

Observed values:

```text
Window:              7,344,000..7,430,399
Start checkpoint:    block 7,343,999
Request IDs:         257..265
Exit count:          9
Total unlock:        138,956.00000000 KAS
Total unlock sompi:  13,895,600,000,000
Funding UTXO:        97b17b0ed3a21a16ba326dd83671b0f3db6537dd929317c5f05a251321a2b9b0:0
Funding amount:      140,000.00000000 KAS
Change:              1,043.99000000 KAS
Fee:                 0.01000000 KAS
Unsigned txid:       97b1697123ed1cff36a36629375202184d6b6a21da578fac514f5479e14fb84b
Payload nonce:       60793
Inputs:              1
Outputs:             10
Signed inputs:       0
Fully signed:        false
```

Exit-35 bundle checks:

```text
Exit global errors: []
Tree global errors: []
Exit txs checked: 9
Merkle inserts in window: 28
Root replay: matched on-chain end root
Tree start: count 699, root 0xf86d79779abe95de6a77b4ae11df08df1881b6c5aac6a430950a250d2868491e
Tree end:   count 727, root 0x634b51d6975c755b5125101aba45d51755b68f05f9e02321f77cf6eb554deb58
```

Continuity is established by the catch-up bundle:

```text
Range:                5,184,000..7,343,999
Exit txs checked:     142
Merkle inserts:       371
Root replay:          matched on-chain end root
Start for exit-35:    catch-up end checkpoint
```

The fixture has two groups of artifacts:

- The canonical L2 audit bundle:
  - `keb-from-7344000-to-7430399-20260525T200000Z.bundle/manifest.json`
  - `derived/exit.data.json`
  - `derived/checks.json`
  - `derived/tree.data.json`
  - `derived/tree.snapshot.json`
  - `derived/checkpoint.end.json`
  - `derived/contract.preverify.json`
  - `raw/checkpoints.json`
  - `raw/keb_exit_logs.json`
  - `raw/keb_burn_logs.json`
  - `raw/hook_inserted_logs.json`
  - `raw/successful_exit_logs.json`
  - `raw/tx/*.tx.json`
  - `raw/tx/*.receipt.json`
- The Kaspa proposal and signer-verification artifacts:
  - `igra-official-bridge-public-keys.json`
  - `funding-utxos.json`
  - `exit-35-bridge-utxos-live.json`
  - `exit-35-funding-utxo-live-check.json`
  - `exit-35-msig-address-check.json`
  - `exit-35-msig-path-check.json`
  - `exit-35-official-bridge.input.json`
  - `exit-35-official-bridge.unsigned.json`
  - `exit-35-official-bridge.unsigned.hex`
  - `exit-35-unsigned-verify.json`
  - `kaspa-landing-check.json`
  - `exit-35-artifact-sha256.txt`

The proposal builder should preserve both groups. The bundle proves the L2 exit
set and tree continuity; the Kaspa artifacts prove the public multisig address,
selected live funding UTXO, unsigned PST, payload, mass, fee, and
not-yet-broadcast landing state.

## Production Bridge Constants

Current public bridge constants from the runbook:

```text
Igra L2 RPC: https://rpc.igralabs.com:8545
Igra chain ID: 38833
KasExitBridge: 0x4bb88C213d3eD9dc4bae694f1bc1bF745903b2d0
Mailbox: 0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7
MerkleTreeHook: 0x75719C858e0c73e07128F95B2C466d142490e933

Kaspa bridge address:
kaspa:ppvnxxzm0rr37zpnwux2f2ntvfpr4uqdpm7zsvsztg3en92r7gs0wkmr72q9n

Kaspa bridge script public key:
aa205933185b78c71f0833770ca4aa6b62423af00d0efc2832025a23999543f220f787

Canonical derivation path: m/0/0/1
Threshold: 2
ECDSA: false
```

Production kpubs:

```text
kpub2HoLSHkWgT8VxmjL7Qv2hbh5Jq9h11XmmPmy3ua2QH89iVNzv6W55ZLy4dVAV3ArUMEAFZWmdADauHTbLCGQ54HyBqgeKTjB3Mdv8kxjetC
kpub2HsAfqNwGLzHhbbmGfAHtkgMM26VfqKsqKuCDAMAw4SMAoUx8YpoKcYq9tBCBqJXirASDtqo3iwcQtSF9d2MKCuLbuPPzTgyH8C5dMMb5Ms
kpub2JeC9uSRRMjr2ExKPtB7UJJo134UFwg6MaToXQefhJv2tgvx4aWah7UfbGM72iF2gpxgHSUGBVu7J5a5wnnrQuAqHNusi9i35XwHfKZgmnr
```

## Event Model

The observer discovers successful exits event-first:

- `KasExitBridge.ExitRequested(uint32 requestId, bytes32 messageId, uint64 feeAmountSompi)`
- `Mailbox.Dispatch(address indexed sender, uint32 destination, bytes32 recipient, bytes message)`
- `Mailbox.DispatchId(bytes32 messageId)`
- `MerkleTreeHook.InsertedIntoTree(bytes32 messageId, uint32 index)`
- `KasExitBridge.BurnIKas(uint256 amount)`

`requestExit(string kasPayoutAddress, uint64 unlockAmountSompi)` calldata is
decoded for direct calls. For internal calls, traces are required to bind the
successful internal `requestExit` frame to the emitted events.

The Hyperlane dispatch message body is the payout source of truth:

```text
format:              0x11
requestId:           uint32
unlockAmountSompi:   uint64
originBurner:        20 bytes
kasPayoutAddress:    length-prefixed UTF-8 string
```

Each proposal output is derived from:

```text
recipient = dispatch.body.kasPayoutAddress
amount    = dispatch.body.unlockAmountSompi
memo      = messageId included in Kaspa payload material
```

## Verification Gates

The proposal builder must fail closed if any hard check fails.

Per-exit checks:

- Receipt status is successful.
- Expected event cardinality is exact.
- `ExitRequested.messageId == keccak256(Dispatch.message)`.
- `messageId` matches across `ExitRequested`, `DispatchId`, `Dispatch`, and
  `InsertedIntoTree`.
- Decoded dispatch body request id equals `ExitRequested.requestId`.
- Decoded dispatch body amount equals `requestExit.unlockAmountSompi`.
- Decoded dispatch body payout address equals `requestExit.kasPayoutAddress`.
- `tx.value` equals `BurnIKas.amount`.
- Internal `requestExit` traces are unambiguous when the top-level tx is not the
  direct bridge call.

Global window checks:

- Successful exit tx count equals `ExitRequested` count.
- Start checkpoint is read at `fromBlock - 1`.
- End checkpoint is read at `toBlock`.
- Merkle tree count delta equals hook `InsertedIntoTree` count.
- Hook message IDs are unique.
- Hook indices are unique and gapless across the scanned range.
- Every successful exit has the matching hook insertion.
- Hyperlane Merkle root replay from the start checkpoint matches the end
  checkpoint.
- `KasExitBridge` request-id and total-burned checkpoints do not regress.

Kaspa checks:

- Federation kpub fingerprint, threshold, and ECDSA flag match the registered
  Safe service federation.
- Canonical bridge address, script public key, and derivation path match config.
- Funding UTXOs are live and unspent immediately before proposal creation.
- Input sum equals exit outputs plus change plus fee.
- Outputs match the verified L2 exits exactly.
- Change returns to the canonical bridge derivation path.
- Protocol payload uses the existing exit format:
  `0x93 || message_id_1 || ... || nonce_u32_be`.
- The unsigned PST passes `kaspa-pst inspect` and the existing `cast igra
  verify-exit` artifact checks.

## Evidence Package

Every `KaspaExitBatch` stores a signer evidence package. Wallets fetch it from:

```text
GET /api/v1/kaspa/exit-batches/{id}/evidence/
```

Recommended schema:

```json
{
  "schemaVersion": 1,
  "kind": "kaspa-exit-proposal-evidence",
  "network": {
    "kaspa": "mainnet",
    "igraChainId": 38833,
    "igraRpcUrl": "https://rpc.igralabs.com:8545"
  },
  "window": {
    "fromBlock": 7344000,
    "toBlock": 7430399,
    "startCheckpointBlock": 7343999,
    "finalizedAtBlock": 7430399,
    "deltaBlocks": 86400
  },
  "contracts": {
    "kasExitBridge": "0x4bb88C213d3eD9dc4bae694f1bc1bF745903b2d0",
    "mailbox": "0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7",
    "merkleTreeHook": "0x75719C858e0c73e07128F95B2C466d142490e933"
  },
  "bridgeMultisig": {
    "threshold": 2,
    "ecdsa": false,
    "xpubs": ["..."],
    "xpubFingerprint": "...",
    "canonicalAddress": "kaspa:...",
    "scriptPublicKey": "aa20...",
    "derivationPath": "m/0/0/1"
  },
  "exits": [
    {
      "requestId": 35,
      "messageId": "0x...",
      "blockNumber": 7344010,
      "transactionHash": "0x...",
      "treeIndex": 123,
      "recipient": "kaspa:...",
      "amountSompi": "100000000",
      "dispatchMessage": "0x...",
      "dispatchDecoded": {}
    }
  ],
  "l2Checks": {},
  "merkleTree": {
    "startCheckpoint": {},
    "endCheckpoint": {},
    "insertedIntoTree": [],
    "rootReplay": {"enabled": true, "match": true}
  },
  "kaspaFunding": {
    "source": "kaspa-node-or-explorer",
    "checkedAt": "2026-06-01T00:00:00Z",
    "utxos": [],
    "liveCheck": {}
  },
  "unsignedTransaction": {
    "format": "kaspawallet_pst_v1",
    "bundleHex": "...",
    "proposalHash": "...",
    "txIds": [],
    "inputs": [],
    "outputs": [],
    "feeSompi": "0",
    "mass": "0",
    "payload": "0x93..."
  },
  "kaspaVerification": {
    "multisigAddressCheck": {},
    "multisigPathCheck": {},
    "unsignedVerify": {},
    "landingCheck": {}
  },
  "artifactHashes": {
    "exitData": "...",
    "checks": "...",
    "treeData": "...",
    "treeSnapshot": "...",
    "fundingUtxos": "...",
    "fundingUtxoLiveCheck": "...",
    "multisigAddressCheck": "...",
    "multisigPathCheck": "...",
    "unsignedJson": "...",
    "unsignedHex": "...",
    "unsignedVerify": "...",
    "kaspaLandingCheck": "..."
  },
  "verifierCommands": [
    "cast igra verify-exit --input ... --unsigned-hex ...",
    "kaspa-pst inspect"
  ]
}
```

The evidence hash is the SHA-256 of canonical JSON:

```text
json.dumps(evidence, sort_keys=True, separators=(',', ':'))
```

## Safe Service Data Model

The Safe service now has durable records for the proposal-builder path:

- `KaspaExitBatch`: one finalized L2 window, verified checks, artifact hashes,
  public bridge multisig config, and full signer evidence.
- `KaspaExitRequest`: one successful L2 exit inside the batch.
- `KaspaTxProposal.exit_batch`: optional one-to-one link from the unsigned Kaspa
  PST proposal to its verified L2 exit evidence.

This preserves the current wallet API while allowing exit proposals to be
identified, audited, and re-verified by wallets.

## Builder Flow

1. Resolve the next window from the latest successful `KaspaExitBatch` or an
   explicit previous checkpoint.
2. Wait until the Igra RPC reports `toBlock` finalized, or until
   `latest >= toBlock + l2ConfirmationBlocks`.
3. Collect logs, receipts, traces, contract checkpoints, and raw tx material.
4. Run the L2, tree, root replay, and contract preverification gates.
5. Select live Kaspa bridge UTXOs.
6. Build unsigned exit input JSON and unsigned PST hex through the Foundry
   `cast igra build-exit` / `build_unsigned_exit` path. This process uses kpubs
   only.
7. Inspect the PST through `kaspa-pst`.
8. Store `KaspaExitBatch` and `KaspaExitRequest` rows with evidence.
9. Create a `KaspaTxProposal` linked to the batch.
10. Wallets fetch the proposal and evidence, re-run verification locally, sign,
    and submit signed PST bundles back to Safe service.

## Safe Service Builder Command

The first implementation lives in this repository as a Django management command:

```text
python manage.py build_kaspa_exit_proposal \
  --config /path/to/builder-config.json \
  --federation <kaspa-federation-uuid> \
  --bundle-dir /path/to/keb-from-X-to-Y.bundle \
  --locking-utxos-json /path/to/funding-utxos.json
```

The command validates the KEB bundle, checks Igra finality unless
`--skip-finality-check` is passed, builds the unsigned PST through
`cast igra build-exit`, verifies it through `cast igra verify-exit`, creates a
`KaspaExitBatch`, records every successful exit as `KaspaExitRequest`, then
submits the unsigned PST through the existing proposal serializer.

The successful staging `daa12` run used the live Igra receipt bundle and a live
custody UTXO:

```text
/app/.venv/bin/python manage.py build_kaspa_exit_proposal \
  --config /work/igra/exits/builder-config-daa12.json \
  --federation b62e5c9e-61cb-4c1b-90fc-7c1b334fbee0 \
  --bundle-dir /work/igra/exits/devnet-exit-daa12.bundle \
  --locking-utxos-json /work/igra/exits/locking-utxos-daa12.json \
  --cast-bin /work/foundry-target-codex-maturity/debug/cast \
  --foundry-timeout 600 \
  --mining-timeout-secs 300 \
  --allow-non-igra-lock-script-for-testing
```

Output:

```json
{
  "evidenceHash": "7265592f07ba302bd33fe44f811dddb9f92f43e4d33963414799581a2cb6688f",
  "exitBatch": "13fa8a2a-c28a-4587-af79-67a46ca5cec0",
  "exitBatchStatus": "proposed",
  "kaspaTxId": "97b1e08af5d2ab619bfd93362d2395aef7e4759ad1b66b429e2b82005b980269",
  "proposalHash": "71c209303a09dd0e62fdfc02e4e9c9621573532c4549051df613a9fb1539b8fd"
}
```

`--allow-non-igra-lock-script-for-testing` is staging-only. The `daa12` custody
UTXO was locked to the generated wallet federation P2SH script rather than the
production Igra bridge lock script. The service still verified that the UTXO
script matched the configured bridge script in `builder-config-daa12.json`.
Production should omit this flag and use the canonical configured Igra lock
script.

For devnet bundles generated from a mainnet-format `requestExit` address, the
bundle preserves both values:

- `kasPayoutAddressRaw`: original contract body address, such as `kaspa:...`.
- `kasPayoutAddress`: devnet-projected address used by `cast igra build-exit`
  when `network=devnet`.

For prebuilt artifacts, such as the exit-35 fixture, the Foundry build step can
be skipped:

```text
python manage.py build_kaspa_exit_proposal \
  --config /path/to/builder-config.json \
  --federation <kaspa-federation-uuid> \
  --bundle-dir /Users/user/Desktop/Igra/Bridge/exit-35/keb-from-7344000-to-7430399-20260525T200000Z.bundle \
  --unsigned-json /Users/user/Desktop/Igra/Bridge/exit-35/exit-35-official-bridge.unsigned.json \
  --unsigned-hex /Users/user/Desktop/Igra/Bridge/exit-35/exit-35-official-bridge.unsigned.hex \
  --unsigned-verify-json /Users/user/Desktop/Igra/Bridge/exit-35/exit-35-unsigned-verify.json
```

Config schema:

```json
{
  "network": "mainnet",
  "l2ChainId": 38833,
  "igraRpcUrl": "https://rpc.igralabs.com:8545",
  "kaspaTxIdPrefix": "97b1",
  "l2ConfirmationBlocks": 12,
  "proposedBy": "igra-exit-proposal-builder",
  "foundryExtendedPublicKeys": [
    "kpub...",
    "kpub..."
  ],
  "contracts": {
    "kasExitBridge": "0x4bb88C213d3eD9dc4bae694f1bc1bF745903b2d0",
    "mailbox": "0x3a867fCfFeC2B790970eeBDC9023E75B0a172aa7",
    "merkleTreeHook": "0x75719C858e0c73e07128F95B2C466d142490e933"
  },
  "bridge": {
    "address": "kaspa:ppvnxxzm0rr37zpnwux2f2ntvfpr4uqdpm7zsvsztg3en92r7gs0wkmr72q9n",
    "scriptPublicKey": "aa205933185b78c71f0833770ca4aa6b62423af00d0efc2832025a23999543f220f787",
    "derivationPath": "m/0/0/1"
  }
}
```

`foundryExtendedPublicKeys` is optional. Use it when the signer-facing
federation stores wallet-specific xpub/kdub metadata but Foundry's
`build-exit` expects the root kpubs used to derive the canonical multisig
address. The unsigned PST and evidence still record the public key material so
wallets can re-derive and verify locally before signing.

## Open Implementation Decisions

- Whether the first live observer shells out to the existing TypeScript/Rust
  tooling, or ports the verifier into Python.
- Where large raw artifacts live long term: database JSON for small reports,
  object storage for raw receipts/traces.
- Whether mainnet should use the current Foundry default of 12 L2 confirmations
  or a stronger finalized-block RPC tag for proposal readiness.
- UTXO selection policy when several canonical bridge UTXOs are available.
- Whether evidence should be unsigned only, or also signed by an independent
  service identity that is not a Kaspa wallet key.
