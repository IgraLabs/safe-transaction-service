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
    "utxos": []
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
  "artifactHashes": {
    "exitData": "...",
    "checks": "...",
    "treeData": "...",
    "treeSnapshot": "...",
    "fundingUtxos": "...",
    "unsignedJson": "...",
    "unsignedHex": "..."
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
2. Wait until the L2 head is beyond `toBlock` by the configured finality margin.
3. Collect logs, receipts, traces, contract checkpoints, and raw tx material.
4. Run the L2, tree, root replay, and contract preverification gates.
5. Select live Kaspa bridge UTXOs.
6. Build unsigned exit input JSON and unsigned PST hex using the existing audited
   builder path. This process uses kpubs only.
7. Inspect the PST through `kaspa-pst`.
8. Store `KaspaExitBatch` and `KaspaExitRequest` rows with evidence.
9. Create a `KaspaTxProposal` linked to the batch.
10. Wallets fetch the proposal and evidence, re-run verification locally, sign,
    and submit signed PST bundles back to Safe service.

## Open Implementation Decisions

- Whether the first live observer shells out to the existing TypeScript/Rust
  tooling, or ports the verifier into Python.
- Where large raw artifacts live long term: database JSON for small reports,
  object storage for raw receipts/traces.
- Exact finality margin beyond the 86,400-block window close.
- UTXO selection policy when several canonical bridge UTXOs are available.
- Whether evidence should be unsigned only, or also signed by an independent
  service identity that is not a Kaspa wallet key.
