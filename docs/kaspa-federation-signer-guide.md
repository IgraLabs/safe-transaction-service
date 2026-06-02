# Kaspa Federation Signer Guide

This guide is for federation members who sign Igra bridge exits from the Kaspa
custody wallet.

For examples, assume Igra Labs operates Safe Transaction Service at:

```text
https://igralab.com/safe-transaction-service
```

Replace that URL with the production URL supplied by Igra Labs.

## Roles

There are three roles in the exit signing flow:

- Igra Labs Proposal Builder observes Igra L2, verifies finalized exit windows,
  builds unsigned Kaspa proposals, and submits them to Safe Transaction Service.
- Safe Transaction Service stores federations, proposals, evidence, signatures,
  quorum state, and broadcast results. It coordinates signing, but it is not a
  trust oracle.
- Federation signers run their own wallet tooling, verify every proposal against
  their own RPC endpoints, and sign only after local verification passes.

The Proposal Builder and Safe Transaction Service do not have signer private
keys.

## One-Time Setup

Each federation member needs:

- A signer-controlled machine for the Kaspa wallet.
- The Go `kaspawallet` signer CLI from `IgraLabs/kaspad` branch
  `kaspa-exit-proposal-verifier`.
- A local signer keys file, for example `signer.keys.json`.
- Access to a signer-owned or signer-trusted Kaspa RPC endpoint.
- Access to a signer-owned or signer-trusted Igra RPC endpoint.
- The agreed federation config: Kaspa network, kpubs, threshold, signing mode,
  canonical custody address, and derivation path.

The signer should share only public kpub material with Igra Labs or the other
federation members. Private keys and mnemonics stay local.

## Federation Registration

Safe Transaction Service needs one federation record per Kaspa signer set.

The federation record contains:

- Kaspa network, for example `mainnet` or `devnet`.
- Sorted federation kpubs.
- Signature threshold, for example `2-of-3`.
- Signing mode, currently Schnorr unless explicitly configured as ECDSA.
- Optional participant labels and cosigner indexes.

The API shape is:

```text
POST /api/v1/kaspa/federations/
GET  /api/v1/kaspa/federations/{federation_id}/
GET  /api/v1/kaspa/federations/{federation_id}/transactions/
```

You do not register a new federation for every exit proposal. Reuse the same
federation for every exit window handled by the same signer set.

Create a new federation only when the Kaspa custody signer set changes:

- kpubs changed
- threshold changed
- Kaspa network changed
- signing mode changed
- key rotation created a new custody wallet

Different Igra exit windows do not require new federation records. Different
Igra contract configuration is part of proposal evidence and Proposal Builder
configuration; it does not by itself change the Kaspa signer federation.

## Proposal Lifecycle

For each finalized exit window:

1. Proposal Builder scans the configured Igra block window.
2. It verifies the configured KasExitBridge, Mailbox, and MerkleTreeHook.
3. It verifies exit transactions, receipts, logs, message ids, recipients,
   amounts, checkpoint continuity, and Merkle tree replay.
4. It selects live Kaspa custody UTXOs.
5. It builds an unsigned Kaspa PST.
6. It embeds the Igra exit messages in `KaspaTx.Payload`.
7. It stores the exit evidence and unsigned proposal in Safe Transaction
   Service.
8. Signers fetch the proposal and evidence.
9. Signers verify locally.
10. Signers submit signed PST bundles.
11. When quorum is reached, the final Kaspa transaction can be broadcast.

The proposal endpoint is:

```text
GET /api/v1/kaspa/transactions/{proposal_hash}/
```

The proposal references an exit batch:

```text
GET /api/v1/kaspa/exit-batches/{exit_batch_id}/
GET /api/v1/kaspa/exit-batches/{exit_batch_id}/evidence/
```

The exit batch detail contains summary metadata. The `/evidence/` endpoint
returns the raw evidence JSON that signers hash and verify.

## What Is In The Evidence

A proposal is not just an unsigned Kaspa transaction. It is paired with evidence
that allows signers to reproduce the decision locally.

Evidence includes:

- Kaspa network and Igra chain id.
- Exit window: `fromBlock`, `toBlock`, finalized block, confirmation settings.
- Required Igra contracts: KasExitBridge, Mailbox, MerkleTreeHook.
- Kaspa bridge custody address, script public key, derivation path, threshold,
  kpubs, and signing mode.
- All exit requests: request id, message id, block number, transaction hash, log
  index, tree index, recipient, amount, burner, and checks.
- KEB bundle artifacts: manifest, exit data, tree data, checkpoint end,
  verification checks, contract preverification, and raw JSON artifacts.
- Kaspa transaction build input.
- Unsigned transaction manifest.
- Unsigned transaction verification report.

The evidence hash is canonical JSON SHA-256. Signers must recompute it and
compare it with the proposal's `exit_evidence_hash`.

## Local Verification Methodology

Federation signers must treat Safe Transaction Service and Proposal Builder data
as untrusted input. The signer signs only if it can independently reproduce the
same unsigned Kaspa PST.

The local verifier must check:

- The downloaded evidence hash equals the proposal `exit_evidence_hash`.
- The proposal belongs to the expected federation.
- Local wallet kpubs, threshold, signing mode, derivation path, custody address,
  and script public key match the evidence.
- Igra RPC chain id matches the configured chain.
- The proposal window is finalized by the signer-owned Igra RPC.
- KasExitBridge, Mailbox, and MerkleTreeHook are the expected contracts.
- Contract code and relevant storage checks match the evidence.
- Exit transactions succeeded.
- Exit logs and receipt data match the evidence.
- Message ids, request ids, recipients, and amounts match the unsigned Kaspa
  outputs.
- Merkle tree replay from the previous checkpoint to the end checkpoint matches
  the evidence.
- Selected Kaspa UTXOs are live, unspent, mature, and locked to the custody
  script.
- The unsigned PST inputs match the selected UTXOs.
- The unsigned PST outputs match all exits plus custody change.
- The fee equals `sum(inputs) - sum(outputs)`.
- `KaspaTx.Payload` equals `0x93 || message_id_1 || ... || message_id_n ||
  nonce_u32_be`.
- The Kaspa txid matches the manifest and configured txid prefix.
- A locally rebuilt unsigned PST matches the proposal bytes exactly.

Any mismatch is a hard failure. The signer should not sign and should escalate
to the federation operators.

## Signer Commands

Fetch and verify without decrypting private keys:

```bash
kaspawallet verify-exit-proposal \
  --keys-file /secure/path/signer.keys.json \
  --safe-url https://igralab.com/safe-transaction-service \
  --proposal-hash <proposal_hash> \
  --igra-rpc-url https://your-igra-rpc.example \
  --kaspa-rpc-url grpc://your-kaspa-rpc.example:16610
```

Verify and sign:

```bash
kaspawallet sign-exit-proposal \
  --keys-file /secure/path/signer.keys.json \
  --safe-url https://igralab.com/safe-transaction-service \
  --proposal-hash <proposal_hash> \
  --igra-rpc-url https://your-igra-rpc.example \
  --kaspa-rpc-url grpc://your-kaspa-rpc.example:16610
```

The signing command runs the verifier first. It should decrypt private keys only
after verification succeeds.

After signing, submit the signed PST bundle to Safe Transaction Service:

```text
POST /api/v1/kaspa/transactions/{proposal_hash}/signatures/
```

The service merges valid signatures and updates quorum state.

## Broadcasting

When enough signatures are collected, the proposal becomes ready for broadcast.

```text
POST /api/v1/kaspa/transactions/{proposal_hash}/broadcast/
```

Broadcasting can be performed by Igra Labs or an assigned federation operator.
The service records broadcast tx ids and errors.

## Who Pays The Kaspa Fee

The Kaspa transaction fee is paid by the custody wallet itself.

Mechanically:

```text
fee = selected custody inputs - exit outputs - custody change output
```

The proposal includes:

- selected custody UTXOs
- every user exit output
- one custody change output, when change exists
- `fee_sompi`

Signers verify that the fee is exactly the difference between inputs and
outputs. The fee is not charged to the signer personally at signing time. It is
deducted from the federation custody UTXOs when the final Kaspa transaction is
broadcast.

The signer should reject proposals where:

- the fee is unexpectedly high
- change does not return to the canonical custody address
- extra outputs exist
- output order or amounts differ from the evidence
- selected UTXOs are not live or mature

For mined devnet/testnet funds, coinbase UTXOs are spendable only after the
network maturity period. Production tooling must not select immature coinbase
UTXOs.

## Trust Model

The federation trust model is fail-closed:

- Igra Labs may operate Proposal Builder and Safe Transaction Service.
- Safe Transaction Service may be unavailable, stale, buggy, or compromised.
- Proposal Builder may be buggy or compromised.
- Other signers may submit bad or irrelevant signatures.
- A signer still does not lose funds if its local wallet verifies correctly and
  signs only reproducible proposals.

The signer trusts:

- its own wallet binary and build process
- its own keys file and password handling
- its own configured federation data
- its own Igra RPC endpoint
- its own Kaspa RPC endpoint
- the deterministic verification algorithm

The signer does not trust:

- proposal text
- service database state
- another signer
- Proposal Builder output
- RPC responses from Igra Labs unless the signer explicitly chooses to trust
  those endpoints

## Operational Checklist

Before joining a federation:

- Confirm the expected kpub set and threshold out of band.
- Confirm the canonical custody address out of band.
- Confirm the Igra chain id and bridge contract addresses out of band.
- Build or install the signer wallet from the expected branch/tag.
- Configure independent Igra and Kaspa RPC endpoints.
- Run a devnet signing rehearsal before mainnet signing.

Before signing each proposal:

- Run `verify-exit-proposal`.
- Review recipient count, total amount, fee, change address, and Kaspa txid.
- Sign only with `sign-exit-proposal`.
- Submit the signed bundle to Safe Transaction Service.
- Watch quorum and broadcast status.

After broadcast:

- Verify the Kaspa transaction by txid.
- Confirm user outputs landed.
- Confirm custody change returned to the canonical custody address.
- Archive proposal hash, evidence hash, signed bundle hash, and broadcast txid.
