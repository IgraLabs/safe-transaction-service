# Kaspa Multisig Coordination

This app coordinates Kaspa multisig transaction proposals for wallet clients.
Wallets keep private keys locally and exchange `kaspawallet` partially signed
transaction bundles through the API.

The service does not parse Kaspa txscript in Python. Proposal inspection,
signature diffing, merging, extraction, and broadcast are delegated to the
configured helper binary:

```text
KASPA_PST_HELPER_PATH=kaspa-pst
KASPA_PST_HELPER_TIMEOUT=10
```

Helper commands receive JSON on stdin and return JSON on stdout:

```text
kaspa-pst inspect
kaspa-pst merge
kaspa-pst broadcast
```

The service sends the federation root xpub set, threshold, and ECDSA flag to the
helper on every operation. The helper must derive every input path and reject a
bundle if its per-input pubkey slots do not match the resolved federation.

The expected wallet flow is:

```text
1. Wallet creates a kaspawallet_pst_v1 unsigned bundle locally.
2. Wallet posts it to /api/v1/kaspa/transactions/ with public federation data.
3. Signer wallets fetch the proposal and sign locally.
4. Signer wallets post signed bundles to /api/v1/kaspa/transactions/{hash}/signatures/.
5. The service accepts only signature-slot additions and stores the merged bundle.
6. Once threshold is met, the proposal becomes ready and can be broadcast.
```

Clients that already know the federation id can also post to
`/api/v1/kaspa/federations/{id}/transactions/`.

## Igra L2 exit proposals

Exit-driven proposals use the same signing flow, but the unsigned PST is
attached to a verified `KaspaExitBatch`.

```text
1. The exit observer verifies a finalized Igra L2 block window and records a KaspaExitBatch.
2. It records every successful ExitRequested message as a KaspaExitRequest.
3. It builds an unsigned Kaspa PST without private keys and posts it with exitBatch.
4. Wallets fetch the proposal and /api/v1/kaspa/exit-batches/{id}/evidence/.
5. Wallets re-run the L2, Merkle-tree, funding-UTXO, and unsigned-tx checks locally.
6. Wallets sign only if the evidence and unsigned PST match.
```

An exit batch is reusable evidence, not a lock. Multiple candidate proposals may
point to the same exit batch. This mirrors upstream Safe behavior: the proposal
hash identifies the candidate transaction, and signer quorum decides which
candidate becomes executable.

Signer wallets can either fetch proposals for their federation and locally show
only candidates that pass verification, or fetch one direct `proposal_hash` from
an operator and verify only that candidate.

The service stores public kpubs, the canonical bridge address, verified event
material, artifact hashes, and the unsigned transaction. It must not store Kaspa
wallet private keys.

Federation signer/operator instructions are documented in
`docs/kaspa-federation-signer-guide.md`.

Safe Transaction Service DevOps/operator instructions are documented in
`docs/kaspa-safe-service-operator-guide.md`.

The proposal-builder entrypoint is:

```text
python manage.py build_kaspa_exit_proposal \
  --config builder.json \
  --federation <uuid> \
  --daemon \
  --poll-seconds 300
```

In service mode the builder creates the next KEB bundle through the configured
`kasExitBridge` runner, queries the configured Kaspa node RPC for live custody
UTXOs, selects mature script-matching inputs, records the selected UTXO evidence,
and submits the unsigned PST through the same proposal validation path as
wallets.

`--bundle-dir` and `--locking-utxos-json` are manual overrides for tests,
backfills, or recovery. They are not the normal testnet/production operating
mode.
