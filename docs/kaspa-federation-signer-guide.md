# Kaspa Federation Signer Guide

This guide is for federation members who approve Igra bridge exits from the
Kaspa custody wallet.

It is written for operators and signers, not only engineers. The main idea is:

```text
Do not sign because a service asked you to sign.
Sign only after your own wallet independently proves the proposal is correct.
```

For examples, assume Igra Labs operates Safe Transaction Service at:

```text
https://igralab.com/safe-transaction-service
```

Replace that URL with the production URL supplied by Igra Labs.

## Big Picture

The federation controls a Kaspa custody wallet. Users request exits on Igra L2.
For each finalized group of exits, a proposal is created to pay users on Kaspa.
Federation members verify and sign that proposal.

```text
Users request exits on Igra L2
        |
        v
+-----------------------------------+
| Igra bridge contracts             |
| KasExitBridge, Mailbox, Hook      |
+-----------------------------------+
        |
        v
+-----------------------------------+
| Proposal Builder                  |
| verifies exits and builds         |
| unsigned Kaspa proposal           |
| NO private keys                   |
+-----------------------------------+
        |
        v
+-----------------------------------+
| Safe Transaction Service          |
| stores proposal, evidence,        |
| signatures, quorum state          |
| NO private keys                   |
+-----------------------------------+
        |
        v
+-----------------------------------+
| Federation signers                |
| each wallet verifies locally      |
| each signer owns one key          |
+-----------------------------------+
        |
        v
+-----------------------------------+
| Kaspa network                     |
| final transaction is broadcast    |
+-----------------------------------+
        |
        +--> exit users receive KAS
        |
        +--> custody change returns to federation wallet
```

The important security boundary is at the signer wallet:

```text
Treat these as untrusted input:

  +-------------------------+
  | Proposal Builder output |
  +-------------------------+
  +-------------------------+
  | Safe-service data       |
  +-------------------------+
  +-------------------------+
  | Other signer data       |
  +-------------------------+
              |
              v
  +---------------------------------------------------------+
  | Signer-controlled environment                           |
  |                                                         |
  |   +-------------------------+                           |
  |   | kaspawallet verifier    |<--- local keys file       |
  |   | and signer              |     private material here |
  |   +-------------------------+                           |
  |        |               |                                |
  |        v               v                                |
  |   Signer Igra RPC   Signer Kaspa RPC                    |
  |                                                         |
  +---------------------------------------------------------+
              |
              v
       Sign only if all checks pass
```

Safe Transaction Service coordinates the process. It does not decide whether a
proposal is safe for you to sign. Your wallet makes that decision locally.

## Who Does What

- Proposal Builder:
  Watches finalized Igra exit windows, verifies bridge evidence, and builds
  unsigned Kaspa proposals. It must not hold signer private keys.

- Safe Transaction Service:
  Stores federations, proposals, evidence, signatures, quorum state, and
  broadcast results. It coordinates signing, but it is not a trust oracle.

- Federation signer:
  Verifies proposals locally and signs only exact, reproducible transactions.
  The signer must not trust a proposal without local verification.

- Broadcaster:
  Broadcasts the final signed Kaspa transaction after quorum. It cannot change
  the transaction after signatures are collected.

## Important Words

| Term | Meaning |
| --- | --- |
| Federation | The Kaspa signer group, for example 2-of-3 signers |
| kpub | A public wallet key. It is safe to share; it cannot spend funds |
| Custody wallet | The Kaspa multisig wallet controlled by the federation |
| Proposal | The unsigned Kaspa transaction the federation is asked to sign |
| Evidence | The data needed to prove the proposal matches real Igra exits |
| PST | Partially Signed Transaction, the old `kaspawallet` multisig format |
| Quorum | The number of required signatures, for example 2 signatures in 2-of-3 |
| Kaspa fee | The Kaspa transaction fee paid from custody UTXOs |

## What Federation Members Need

Each signer needs:

- A signer-controlled machine for the wallet.
- The `kaspawallet` CLI from `IgraLabs/kaspad`, branch
  `kaspa-exit-proposal-verifier`.
- A local signer keys file, for example `signer.keys.json`.
- A trusted Kaspa RPC endpoint.
- A trusted Igra RPC endpoint.
- The agreed federation configuration.

The federation configuration includes:

- Kaspa network, for example `mainnet` or `devnet`.
- All federation kpubs.
- Threshold, for example `2-of-3`.
- Signing mode, usually Schnorr.
- Canonical Kaspa custody address.
- Custody derivation path.
- Igra chain id.
- Igra bridge contract addresses.

Private keys and mnemonics must stay local. Only public kpubs should be shared.

## Federation Registration

Safe Transaction Service needs one federation record for each Kaspa signer set.

```text
One federation record is created from:

  federation kpubs
  threshold, for example 2-of-3
  Kaspa network
  signing mode
          |
          v
  +---------------------------------------------+
  | Federation record                           |
  | /api/v1/kaspa/federations/{federation_id}/  |
  +---------------------------------------------+
          |
          v
  Many exit proposals reuse this same federation id
```

You do not register a new federation for every exit proposal. Reuse the same
federation for every exit window handled by the same signer set.

Any federation can operate through the service. Registration is the step where
the federation creates a public namespace for its signer set.

Registration can be self-service, API-driven, automated by the federation's own
deployment tooling, or inferred from the first proposal payload when that
payload includes the public federation material.

That first proposal payload must include the federation's public trust anchor:

- Kaspa network
- public kpub set
- threshold
- signing mode
- optional participant labels

The service derives the federation fingerprint from those public fields and
creates or reuses the matching federation record.

That distinction matters:

```text
Explicit registration:

  federation-approved config
      kpubs + threshold + network + custody address
              |
              v
  idempotent service bootstrap creates/reuses federation record

First-proposal registration:

  proposal payload
      unsigned PST + public federation block
              |
              v
  service derives federation fingerprint and creates/reuses record
```

The federation record is the service-side identifier for the public signer set.
It is not an endorsement by Igra Labs and it is not what makes a proposal
trustworthy by itself. Signer wallets must still pin the expected kpubs,
threshold, network, custody address, and Igra bridge config locally before
signing.

Create a new federation only when the Kaspa custody signer set changes:

- kpubs changed
- threshold changed
- Kaspa network changed
- signing mode changed
- key rotation created a new custody wallet

Different Igra exit windows do not require new federation records. Different
Igra bridge contract configuration is part of the proposal evidence and builder
configuration; it does not by itself create a new Kaspa signer federation.

The main API paths are:

```text
POST /api/v1/kaspa/federations/
GET  /api/v1/kaspa/federations/{federation_id}/
POST /api/v1/kaspa/transactions/
GET  /api/v1/kaspa/federations/{federation_id}/transactions/
GET  /api/v1/kaspa/transactions/{proposal_hash}/
GET  /api/v1/kaspa/exit-batches/{exit_batch_id}/evidence/
POST /api/v1/kaspa/transactions/{proposal_hash}/signatures/
POST /api/v1/kaspa/transactions/{proposal_hash}/broadcast/
```

Most signers should use the wallet commands rather than calling these APIs
manually.

## Proposal Builder Config Example

Igra Labs runs one Proposal Builder config per federation/bridge setup. This
config is an allowlist. It says exactly which Igra chain, contracts, Kaspa
network, and custody address the builder is allowed to use.

The config does not contain private keys.

```text
Proposal Builder config
        |
        +--> which Igra chain?
        |
        +--> which bridge contracts?
        |
        +--> which Kaspa network?
        |
        +--> which custody address?
        |
        +--> how many confirmations/finality checks?
```

Production-shaped example:

```json
{
  "network": "mainnet",
  "l2ChainId": 38833,
  "igraRpcUrl": "https://rpc.igralabs.com:8545",
  "kaspaTxIdPrefix": "97b1",
  "l2ConfirmationBlocks": 12,
  "proposedBy": "igra-exit-proposal-builder",
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

Field meaning:

| Field | What it means |
| --- | --- |
| `network` | Kaspa network. A mainnet federation must see `mainnet`. |
| `l2ChainId` | Igra L2 chain id. Signers verify this through their own Igra RPC. |
| `igraRpcUrl` | RPC used by the builder. Signers may use a different RPC locally. |
| `kaspaTxIdPrefix` | Required txid prefix for Igra protocol transactions. |
| `l2ConfirmationBlocks` | Confirmation/finality margin before the builder proposes. |
| `contracts` | Exact Igra bridge contracts the builder is allowed to observe. |
| `bridge.address` | Canonical Kaspa custody address for this federation. |
| `bridge.scriptPublicKey` | Expected Kaspa locking script for custody UTXOs. |
| `bridge.derivationPath` | Wallet path used to derive the custody address. |

The signer set itself comes from the Safe Transaction Service federation record:
kpubs, threshold, Kaspa network, and signing mode. The proposal-builder config
and the federation record must agree on the custody wallet. If they do not, the
builder should fail before proposal creation and signer wallets should fail
before signing.

Some deployments may also include `foundryExtendedPublicKeys` in the config.
Those are public kpubs used by the Foundry `build-exit` command. They are not
private keys.

## Automatic Or Manual?

In production, Proposal Builder should run as an operator service. Federation
members normally do not run it by hand.

```text
Normal production mode

Proposal Builder service
        |
        +--> watches one configured federation/bridge
        |
        +--> waits until the next Igra exit window is finalized
        |
        +--> creates the KEB evidence bundle from Igra RPC
        |
        +--> queries Kaspa node RPC for live custody UTXOs
        |
        +--> selects mature script-matching inputs
        |
        +--> verifies the window
        |
        +--> creates a candidate proposal in Safe Transaction Service
        |
        +--> remembers progress and waits for the next window
```

Manual runs are still useful, but they are operator actions:

- devnet testing
- staging rehearsals
- backfilling an old finalized window
- retrying after infrastructure failure
- investigating a failed automatic run

Manual mode must use the same config, same federation id, and same validation
rules as the service mode. It must not bypass evidence checks.

The management command can still accept an existing `--bundle-dir` and
`--locking-utxos-json`, but those are manual override paths for staging,
backfill, and recovery. In production, the Proposal Builder service creates the
KEB bundle itself and selects live custody UTXOs from the configured Kaspa node
RPC.

Signers should think about the system like this:

```text
Proposal Builder creates proposals.
Signer wallets verify and sign proposals.

These are different jobs.
```

The Proposal Builder still cannot move funds by itself. It has no private keys.
It can only create an unsigned proposal and evidence package.

## Per-Proposal Flow

This is what happens for one exit proposal.

```text
1. Igra L2
   Finalized exit window exists
          |
          v
2. Proposal Builder
   - verifies exits
   - builds evidence
   - builds unsigned Kaspa PST
          |
          v
3. Safe Transaction Service
   Stores proposal and evidence
          |
          v
4. Signer wallet
   Fetches proposal and evidence
          |
          +--> checks signer Igra RPC:
          |    chain, contracts, logs, finality
          |
          +--> checks signer Kaspa RPC:
          |    custody UTXOs
          |
          +--> rebuilds unsigned PST locally
          |
          +--> compares rebuilt bytes with proposal
          |
          v
5. If exact match, signer submits signature
          |
          v
6. Safe Transaction Service
   Merges signatures and tracks quorum
          |
          v
7. Kaspa network
   Final transaction is broadcast after quorum
```

A signer normally performs only these actions:

1. Receive/discover proposals for the federation, or receive one direct
   `proposal_hash`.
2. Run `verify-exit-proposal` for each candidate the signer wants to inspect.
3. Ignore proposals that fail local verification.
4. Review the high-level result: recipient count, total amount, fee, change
   address, txid.
5. Run `sign-exit-proposal` only for the selected verified proposal.
6. Submit the signed bundle to Safe Transaction Service.
7. Watch quorum and broadcast status.

Both signer UX styles are supported:

```text
Browse mode:
  wallet fetches proposals for the federation
  wallet verifies each candidate locally
  wallet shows only candidates that pass verification
  signer selects one candidate to sign

Direct mode:
  operator/signer provides proposal_hash
  wallet fetches that proposal
  wallet verifies it locally
  signer signs only if verification passes
```

## Duplicates And Edge Cases

The system should be safe if two operators click the same button, if a signer
submits twice, or if a proposal becomes stale. Signers should understand what
happens in these cases.

### Two Operators Run Proposal Builder For The Same Window

Expected result:

- Safe Transaction Service may accept more than one exit batch for the same
  federation, Igra chain id, and block window when the evidence hash differs.
- Safe Transaction Service may accept many candidate proposals linked to the
  same exit batch.
- Identical proposal bytes resolve to the same `proposal_hash`.
- Different unsigned PST candidates get different `proposal_hash` values.
- No candidate locks the window for the others.

Signer action:

- Verify the candidate selected by the federation or operator.
- Ignore any candidate that fails local verification.
- Keep local anti-double-sign state for the exit evidence/message set.

### Two Different Proposals Claim The Same Exits

This can happen in an open service. It is not a service-level emergency by
itself because proposals are only candidates.

Possible causes:

- different UTXO selection
- different fee policy
- builder retry with different transaction candidate
- wrong builder config
- attempted malicious proposal

Signer action:

- Compare federation id, custody address, Igra window, evidence hash, fee, and
  outputs.
- Sign only one candidate for the same exit evidence/message set.
- If several candidates pass verification, follow the federation's operational
  selection policy.
- If none pass verification, ignore them and ask operators for a new candidate.

### Same Signer Submits The Same Signature Twice

Expected result:

- Safe Transaction Service should not count the same signature twice.
- A duplicate submission should be rejected or treated as no new signature.

Signer action:

- No emergency action is needed.
- Check the proposal page/API to confirm your signature was recorded once.

### Quorum Is Reached While A Signer Is Still Reviewing

Expected result:

- Once enough signatures are collected, the proposal becomes ready for broadcast.
- A later signer may not need to sign.

Signer action:

- If the transaction is already broadcast, verify the broadcast txid and outputs.
- If it is ready but not broadcast, the signer may still review, but the
  federation should coordinate who broadcasts.

### Selected Kaspa UTXO Is Spent Before Broadcast

Expected result:

- The transaction may fail to broadcast because an input is no longer available.
- Another candidate proposal may be required with different live custody UTXOs.

Signer action:

- Do not reuse old signatures for another proposal.
- Verify and sign the new candidate proposal from scratch.

### Fee Looks Wrong

Expected result:

- The signer verifier checks the exact fee math.
- A mathematically valid fee can still be operationally unacceptable if it is
  much higher than expected.

Signer action:

- Reject or pause if the fee is surprising.
- Ask operators to explain the fee policy before signing.

### Broadcast Fails

Expected result:

- Safe Transaction Service records the broadcast error.
- Operators investigate whether the issue is RPC availability, stale UTXOs,
  invalid signatures, or network state.

Signer action:

- Do not sign another candidate blindly.
- Treat every candidate as a new proposal and run the full verifier again.

## What The Proposal Contains

A proposal is not just "please sign this transaction." It includes or points to
evidence so each signer can verify the transaction independently.

```text
+-------------------------------------------------------------+
| Kaspa proposal                                              |
|                                                             |
|  - unsigned Kaspa PST: inputs, outputs, fee, payload        |
|  - evidence hash                                            |
|  - exit batch id                                            |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
| Evidence JSON                                                |
|                                                             |
|  Igra exits: logs, receipts, message ids, amounts           |
|  Contract checks: KasExitBridge, Mailbox, MerkleTreeHook    |
|  Merkle replay and checkpoint continuity                    |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
| Proposal candidate metadata                                  |
|                                                             |
|  Selected Kaspa custody UTXOs                                |
|  Unsigned transaction manifest and verifier report           |
|  Candidate-specific artifact hashes                          |
+-------------------------------------------------------------+
```

Evidence includes:

- Kaspa network and Igra chain id.
- Exit window: `fromBlock`, `toBlock`, finalized block, confirmation settings.
- Required Igra contracts: KasExitBridge, Mailbox, MerkleTreeHook.
- Kaspa custody address, script public key, derivation path, threshold, kpubs,
  and signing mode.
- Each exit: request id, message id, transaction hash, recipient, and amount.
- Bundle verification results: exit checks, tree checks, checkpoint data, and
  contract preverification.
- Selected Kaspa custody UTXOs.
- Unsigned transaction manifest and verification report.

The evidence hash is canonical JSON SHA-256. If the downloaded evidence does
not hash to the proposal's `exit_evidence_hash`, signing must stop.

## What Your Wallet Verifies

Your wallet verifier asks one question:

```text
Can I independently rebuild the exact same unsigned Kaspa transaction from
public chain data and the agreed federation configuration?
```

It checks:

| Area | What is verified |
| --- | --- |
| Evidence | Evidence hash matches the proposal |
| Federation | kpubs, threshold, signing mode, custody address, derivation path |
| Igra chain | chain id, finalized window, expected bridge contracts |
| Igra exits | successful receipts, logs, message ids, recipients, amounts |
| Merkle proof | tree replay and checkpoint continuity |
| Kaspa UTXOs | selected custody UTXOs are live, unspent, mature, and correct |
| Kaspa transaction | inputs, outputs, change, fee, payload, txid |
| Final bytes | locally rebuilt unsigned PST equals proposed PST exactly |

If any check fails, do not sign.

## Signer Commands

First verify without decrypting private keys:

```bash
kaspawallet verify-exit-proposal \
  --keys-file /secure/path/signer.keys.json \
  --safe-url https://igralab.com/safe-transaction-service \
  --proposal-hash <proposal_hash> \
  --igra-rpc-url https://your-igra-rpc.example \
  --kaspa-rpc-url grpc://your-kaspa-rpc.example:16610
```

Then verify and sign:

```bash
kaspawallet sign-exit-proposal \
  --keys-file /secure/path/signer.keys.json \
  --safe-url https://igralab.com/safe-transaction-service \
  --proposal-hash <proposal_hash> \
  --igra-rpc-url https://your-igra-rpc.example \
  --kaspa-rpc-url grpc://your-kaspa-rpc.example:16610
```

The signing command runs the same verification first. It should decrypt private
keys only after verification succeeds.

After signing, submit the signed PST bundle to Safe Transaction Service:

```text
POST /api/v1/kaspa/transactions/{proposal_hash}/signatures/
```

Safe Transaction Service merges valid signatures and updates quorum state.

## Who Pays The Kaspa Fee

Kaspa does not use Ethereum-style gas. It uses transaction fees paid in sompi
from the Kaspa transaction itself.

For an exit proposal, the Kaspa fee is paid by the federation custody wallet.
It is deducted from the selected custody UTXOs when the final transaction is
broadcast.

```text
Selected custody UTXOs
        |
        +------------------> user exit outputs
        |
        +------------------> custody change output
        |                    back to federation wallet
        |
        +------------------> Kaspa transaction fee
                             paid to miners
```

Mechanically:

```text
fee = selected custody inputs - user exit outputs - custody change output
```

The fee is not paid personally by the signer at signing time. The signer is
approving a custody transaction whose fee comes from custody funds.

Reject a proposal if:

- the fee is unexpectedly high
- change does not return to the canonical custody address
- extra outputs exist
- output amounts differ from the evidence
- selected UTXOs are not live or mature

For mined devnet or testnet funds, coinbase UTXOs are spendable only after the
network maturity period. Production tooling must not select immature coinbase
UTXOs.

## Broadcasting

After enough signatures are collected, the proposal becomes ready. Igra Labs or
an assigned operator can broadcast it:

```text
POST /api/v1/kaspa/transactions/{proposal_hash}/broadcast/
```

Broadcasting does not change the transaction. The signatures already commit to
the exact transaction bytes.

After broadcast, signers should verify:

- the Kaspa txid exists on the expected network
- users received the expected outputs
- custody change returned to the canonical custody address
- the recorded broadcast txid matches the signed proposal

## Trust Model

The model is fail-closed.

Igra Labs may run the Proposal Builder and Safe Transaction Service, but that
does not mean signers must trust them. A compromised service can ask for a bad
signature, but a correctly verifying signer wallet should refuse to sign it.

```text
Bad or compromised service
        |
        v
Bad proposal
        |
        v
+------------------+
| Signer verifier  |
+------------------+
        |
        v
Verification fails
        |
        v
No signature


Correct proposal
        |
        v
+------------------+
| Signer verifier  |
+------------------+
        |
        v
Verification passes
        |
        v
Signer adds signature
```

The signer trusts:

- its own wallet binary and build process
- its own keys file and password handling
- its own confirmed federation configuration
- its own Igra RPC endpoint
- its own Kaspa RPC endpoint
- the deterministic verification algorithm

The signer does not automatically trust:

- Proposal Builder output
- Safe Transaction Service database state
- another signer
- proposal text or UI labels
- Igra Labs RPC responses unless the signer explicitly chooses to use those
  endpoints

## Practical Checklist

Before joining a federation:

- Confirm the kpub set and threshold out of band.
- Confirm the canonical custody address out of band.
- Confirm the Igra chain id and bridge contract addresses out of band.
- Build or install the signer wallet from the expected branch or tag.
- Configure independent Igra and Kaspa RPC endpoints.
- Run a devnet signing rehearsal before mainnet signing.

Before signing each proposal:

- Run `verify-exit-proposal`.
- Confirm the proposal hash and evidence hash.
- Review recipient count, total amount, fee, change address, and txid.
- Sign only with `sign-exit-proposal`.
- Submit the signed bundle to Safe Transaction Service.
- Watch quorum and broadcast status.

After broadcast:

- Verify the Kaspa transaction by txid.
- Confirm user outputs landed.
- Confirm custody change returned to the canonical custody address.
- Archive proposal hash, evidence hash, signed bundle hash, and broadcast txid.

## Technical Appendix

The verifier rebuilds the unsigned Kaspa PST from public data:

```text
verified Igra exits
+ verified custody UTXOs
+ federation kpubs and threshold
+ custody change policy
+ Kaspa fee
+ Igra exit payload nonce
= unsigned Kaspa PST
```

The Igra exit payload embedded in the Kaspa transaction is:

```text
0x93 || message_id_1 || ... || message_id_n || nonce_u32_be
```

The wallet signs only if:

```text
locally rebuilt PST bytes == proposal PST bytes
```

That byte-for-byte comparison is the main safety property. It prevents Safe
Transaction Service, Proposal Builder, or another signer from silently changing
recipients, amounts, fees, inputs, change, payload, or txid.
