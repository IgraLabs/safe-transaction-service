# Kaspa Safe Transaction Service Operator Guide

This guide is for DevOps and operators running Safe Transaction Service with
the Igra/Kaspa federation additions.

It explains what was added on top of vanilla upstream Safe Transaction Service,
why it was added, how it changes infrastructure requirements, and how to run it
safely when exposed to federation members or the public Internet.

## Executive Summary

The Kaspa addition turns Safe Transaction Service into a coordination layer for
Kaspa multisig proposals.

It does not custody funds.

It does not hold signer private keys.

It stores:

- Kaspa federation records.
- Unsigned Kaspa `kaspawallet` PST proposals.
- Igra exit evidence packages.
- Signer PST submissions.
- Merged quorum state.
- Broadcast attempts and results.

Signer wallets remain responsible for trust. Each signer independently verifies
proposal evidence and signs locally.

## What Changed From Vanilla Upstream

Vanilla upstream Safe Transaction Service is Ethereum/Safe-oriented. It stores
and indexes Ethereum Safe data, coordinates Safe transactions, and uses the
existing Ethereum data model and indexers.

This branch adds a separate Kaspa coordination surface.

```text
Vanilla Safe Transaction Service
        |
        +--> Ethereum/Safe API, models, indexers, workers
        |
        +--> Postgres, Redis, Django API

This branch adds
        |
        +--> safe_transaction_service/kaspa/
        |
        +--> Kaspa federation API
        |
        +--> Kaspa PST proposal/signature/broadcast API
        |
        +--> Igra exit-batch evidence storage
        |
        +--> proposal-builder management command
        |
        +--> external kaspa-pst helper binary
```

New API group:

```text
/api/v1/kaspa/federations/
/api/v1/kaspa/federations/{federation_id}/transactions/
/api/v1/kaspa/transactions/
/api/v1/kaspa/transactions/{proposal_hash}/
/api/v1/kaspa/transactions/{proposal_hash}/signatures/
/api/v1/kaspa/transactions/{proposal_hash}/broadcast/
/api/v1/kaspa/exit-batches/
/api/v1/kaspa/exit-batches/{exit_batch_id}/
/api/v1/kaspa/exit-batches/{exit_batch_id}/evidence/
```

New database entities:

- `KaspaFederation`
- `KaspaFederationParticipant`
- `KaspaExitBatch`
- `KaspaExitRequest`
- `KaspaTxProposal`
- `KaspaTxSignature`
- `KaspaBroadcastAttempt`

New external helper:

```text
KASPA_PST_HELPER_PATH=kaspa-pst
KASPA_PST_HELPER_TIMEOUT=10
```

The helper performs Kaspa PST inspection, merge, and broadcast operations. The
Django service delegates Kaspa transaction parsing to the helper instead of
reimplementing Kaspa txscript logic in Python.

## Why We Added This

The Igra bridge exits on Igra L2, but custody funds live in a Kaspa multisig
wallet. We need a system where:

- Proposal Builder can build exit proposals without private keys.
- Safe Transaction Service can coordinate signatures and quorum.
- Signers can verify locally and keep private keys offline from the service.
- Igra exit evidence is stored with the proposal.
- The final Kaspa transaction can be broadcast after quorum.

Safe Transaction Service already gives us a useful pattern:

```text
store unsigned proposal
collect signer submissions
merge signatures
track quorum
broadcast once ready
```

The Kaspa addition reuses that coordination model for old `kaspawallet` PST
bundles and Igra exit evidence.

## Runtime Topology

Recommended production topology:

```text
                         Internet / VPN / private signer network
                                      |
                                      v
                              +---------------+
                              | Load balancer |
                              | WAF/rate limit|
                              +---------------+
                                      |
                                      v
+----------------+          +----------------------+          +----------------+
| Proposal       |  writes  | Safe Transaction     |  reads   | Federation     |
| Builder        |--------->| Service API          |<-------->| signer wallets |
| no keys        |          | Django/Gunicorn      |          | local keys     |
+----------------+          +----------------------+          +----------------+
                                      |
                                      v
                              +---------------+
                              | kaspa-pst     |
                              | helper binary |
                              +---------------+
                                      |
                     +----------------+----------------+
                     |                                 |
                     v                                 v
              +--------------+                  +--------------+
              | Postgres     |                  | Redis        |
              | durable data |                  | cache/queues |
              +--------------+                  +--------------+
```

The Proposal Builder can run as:

- a separate service per federation/config, or
- one operator service with strictly separated per-federation configuration and
  state.

For production, prefer service mode. Manual runs are for staging, recovery,
backfill, or investigation.

## Docker Services

This branch adds a Compose profile for the proposal-builder daemon:

```text
docker compose --profile kaspa up kaspa-proposal-builder
```

Required environment:

```text
KASPA_EXIT_BUILDER_CONFIG=/config/builder.json
KASPA_FEDERATION_ID=<safe-service federation uuid>
KASPA_EXIT_BUILDER_POLL_SECONDS=300
KASPA_PST_HELPER_PATH=kaspa-pst
```

The API container needs Python dependencies and the `kaspa-pst` helper for PST
inspect/merge/broadcast. The proposal-builder container additionally needs:

- `kaspa-pst` with the `utxos` command
- Foundry `cast` with `igra build-exit` and `igra verify-exit`
- Node/npm and the `kasExitBridge` tooling, or a mounted tool directory that
  contains it
- KEB manifest signing key material
- access to Igra RPC and Kaspa node RPC
- persistent KEB reports directory

Postgres and Redis are still separate services. Postgres stores durable
federations, evidence, proposals, signatures, and broadcasts. Redis is cache and
queue infrastructure used by the upstream service. Neither Postgres nor Redis
stores signer private keys.

## What Runs Automatically

In normal production:

```text
Proposal Builder service
        |
        +--> watches one configured federation/bridge
        |
        +--> waits until an Igra exit window is finalized
        |
        +--> creates the KEB evidence bundle from Igra RPC
        |
        +--> queries Kaspa node RPC for custody UTXOs
        |
        +--> selects mature script-matching inputs
        |
        +--> verifies exits and evidence
        |
        +--> builds unsigned Kaspa PST
        |
        +--> submits a candidate proposal to Safe Transaction Service
        |
        +--> records progress and waits for the next window
```

Operators should not normally hand-create proposals. If a manual run is needed,
use the same config and validation path as the service mode.

The Django management command can run either one-shot or as a daemon. In daemon
mode it creates the next KEB bundle itself by calling the configured
`kasExitBridge` runner, then queries the configured Kaspa node RPC for live
custody UTXOs and selects the spend inputs. `--bundle-dir` and
`--locking-utxos-json` are retained only as manual overrides for staging,
backfill, recovery, and tests.

## Federation Bootstrap

Safe Transaction Service needs one federation record before proposals can be
created for that signer set.

The service can support many independent federations. Igra Labs does not need to
manually approve every federation for the protocol to work. Registration creates
a service-side namespace for that federation's public signer set.

Registration can be:

- self-service through the federation API
- API-driven by the federation operator
- seeded automatically during deployment from a federation-owned config
- inferred from the first proposal payload when that payload includes the
  public federation material

All modes should create the same durable federation record:

```text
federation config
        |
        +--> network
        +--> kpub set
        +--> threshold
        +--> signing mode
        +--> canonical custody address
        |
        v
idempotent bootstrap creates/reuses federation record
```

For first-proposal registration, the proposal payload carries the unsigned PST
and an explicit public federation block:

```text
proposal payload
        |
        +--> unsigned PST
        +--> network
        +--> kpub set
        +--> threshold
        +--> signing mode
        |
        v
service derives fingerprint and creates/reuses federation record
```

This makes the service usable by any federation without a manual pre-registration
step. The auto-created federation is still only a public namespace. It is not an
operator endorsement, and it does not replace signer-side verification.

Operational rule:

- Any federation can register and operate its own signer set.
- Self-service/API registration is acceptable with authentication, rate limits,
  and quotas.
- Auto-bootstrap from federation-owned deployment config is acceptable.
- First-proposal federation inference is acceptable when the payload contains
  explicit public federation material.
- Signers still verify the federation locally before signing.

## Exit Window Size

Production KEB bundles currently use deterministic Igra L2 windows. For example,
exit-35 used:

```text
keb-from-7344000-to-7430399-20260525T200000Z.bundle
```

That range is `7,430,399 - 7,344,000 + 1 = 86,400` Igra L2 blocks. It is not an
86,400-block Kaspa L1 scan range.

The Proposal Builder handles this as an Igra evidence window:

```text
KEB bundle manifest
        |
        +--> fromBlock / toBlock are Igra L2 block numbers
        |
        +--> builder checks Igra finality for toBlock
        |
        +--> builder verifies bridge evidence and Merkle continuity
        |
        +--> builder uses selected live Kaspa custody UTXOs
        |
        +--> builder creates one unsigned Kaspa PST proposal
```

The builder does not walk 86,400 Kaspa mainnet blocks. Kaspa-side work is limited
to the custody UTXOs selected for the proposal and the final Kaspa PST build,
merge, and broadcast paths.

## Idempotency And Duplicate Submissions

The Kaspa models include uniqueness constraints so normal duplicate operator
actions do not create multiple valid records for the same thing.

Important constraints:

- Federation uniqueness:
  `network + xpub_fingerprint + threshold + ecdsa`.
- Exit batch uniqueness:
  `federation + l2_chain_id + from_block + to_block`.
- Exit request uniqueness:
  `batch + request_id` and `batch + message_id`.
- Proposal uniqueness:
  `proposal_hash`.
- Signature submission uniqueness:
  `proposal + submission_hash`.

Expected duplicate behavior:

- Two builders submit the same finalized window:
  they reuse the same exit batch when the KEB evidence is identical.
- Two builders submit different evidence for the same finalized window:
  Safe stores separate exit batches because `evidence_hash` is part of the
  identity. Signers will only sign proposals whose evidence verifies locally.
- Two identical proposals are submitted:
  one proposal hash wins; duplicate creation fails.
- Two different proposals for the same exit batch are accepted as separate
  candidates with different proposal hashes.
- A signer submits the same signature twice:
  the duplicate should not add quorum.

This mirrors the upstream Safe Transaction Service model: transaction identity is
the transaction/proposal hash, not the Safe nonce or Igra exit window. A noisy or
malicious candidate does not lock the window. Signers decide what matters by
verifying and signing one candidate.

## CPU Impact

Compared with vanilla Safe Transaction Service, the Kaspa endpoints add CPU
work in three places:

1. Proposal validation:
   `kaspa-pst inspect` runs for unsigned PST submissions.
2. Signature submission:
   `kaspa-pst merge` runs to diff and merge signer PST bundles.
3. Broadcast:
   `kaspa-pst broadcast` runs and talks to Kaspa RPC.

The exit evidence read endpoints are mostly JSON serialization and bandwidth.
The Proposal Builder itself can be CPU-heavy when it verifies large Igra exit
windows and builds unsigned transactions, but that should run outside the public
API request path.

Operational guidance:

- Limit helper subprocess concurrency.
- Keep `KASPA_PST_HELPER_TIMEOUT` low enough to fail stuck requests.
- Do not run unlimited Gunicorn workers if each worker can spawn helper
  subprocesses.
- Put write endpoints behind authentication and rate limits.

Initial CPU sizing:

```text
Small staging/devnet:
  API:       2 vCPU
  Postgres: 2 vCPU
  Redis:    1 vCPU

Production MVP, one or a few federations:
  API:       4 vCPU
  Postgres: 4 vCPU
  Redis:    1-2 vCPU

Higher volume or public exposure:
  API:       2+ nodes, 4-8 vCPU each
  Postgres: 8+ vCPU, tuned IOPS
  Redis:    2+ vCPU
```

These are starting points. Measure helper latency, request rate, DB size, and
evidence payload size before final capacity planning.

## Memory Impact

The main memory risk is large JSON evidence. Current implementation stores
evidence in Postgres JSON fields and returns evidence JSON through the API.

Memory pressure can come from:

- API workers serializing large evidence responses.
- Postgres cache and TOAST reads for JSON fields.
- helper subprocess memory during PST inspect/merge.
- large request bodies for invalid or malicious submissions.

Initial memory sizing:

```text
Small staging/devnet:
  API:       4 GB RAM
  Postgres: 4-8 GB RAM
  Redis:    1 GB RAM

Production MVP:
  API:       8 GB RAM
  Postgres: 16 GB RAM
  Redis:    2 GB RAM

Higher volume or large evidence retention:
  API:       8-16 GB RAM per node
  Postgres: 32+ GB RAM
  Redis:    4+ GB RAM
```

Operator controls:

- Enforce request body limits at Nginx/load balancer.
- Keep evidence responses compressed.
- Consider moving raw large artifacts to object storage long term.
- Keep only compact evidence summaries in Postgres if raw artifacts grow too
  large.

## Disk Impact

Kaspa additions increase disk usage mostly in Postgres.

Stored data includes:

- federation records
- proposal PST hex
- merged PST hex
- signed PST hex submissions
- exit evidence JSON
- raw JSON artifacts inside evidence
- broadcast attempts
- Postgres WAL
- backups

The biggest field is usually `KaspaExitBatch.evidence`.

Sizing rule of thumb:

```text
disk per exit batch ~= evidence JSON
                    + exit request rows
                    + proposal PST hex
                    + signed PST hex per signer
                    + indexes
                    + WAL/backups overhead
```

Evidence size depends on exit count and how much raw artifact data is embedded.
Small batches may be kilobytes to a few megabytes. Full raw bundles can be much
larger.

Initial disk sizing:

```text
Small staging/devnet:
  Postgres volume: 50-100 GB

Production MVP:
  Postgres volume: 250-500 GB NVMe/SSD

Large raw evidence retention:
  Postgres volume: 1 TB+
  plus object storage for raw artifacts
```

Plan backup storage separately. WAL and backups commonly require another
2x-3x of active database size depending on retention policy.

Critical monitoring:

- Postgres database size.
- Largest table sizes.
- TOAST table growth.
- WAL volume.
- Backup duration and restore time.
- `/api/v1/kaspa/exit-batches/{id}/evidence/` response sizes.

## Network And Bandwidth

Normal signer traffic is low. The heavy bandwidth endpoint is evidence download.

Bandwidth grows with:

- number of signers
- number of proposals
- evidence JSON size
- repeated downloads or retries
- public unauthenticated reads

Use gzip or equivalent response compression for JSON.

If evidence becomes large, consider:

- authenticated evidence endpoints
- CDN or object storage for immutable evidence blobs
- ETags or cache headers
- signer-side local caching by evidence hash

## What Happens Under DDoS

A DDoS cannot directly steal funds because Safe Transaction Service has no
Kaspa private keys. However, it can harm availability and increase cost.

Risk by endpoint type:

```text
Read endpoints
  - DB load
  - JSON serialization CPU
  - bandwidth pressure
  - large evidence downloads

Federation creation
  - database spam
  - xpub JSON validation
  - table/index growth

Proposal creation
  - federation auto-registration spam
  - helper CPU via kaspa-pst inspect
  - request body pressure
  - DB writes for valid proposals

Signature submission
  - helper CPU via kaspa-pst merge
  - DB transaction contention
  - duplicate/noisy submissions

Broadcast endpoint
  - helper CPU
  - Kaspa RPC pressure
  - repeated failed broadcast attempts
```

Most important mitigation:

```text
Open federation access still needs abuse controls on write endpoints.
```

If this branch is deployed before application-level auth/rate limits are added,
operators must enforce those controls at the load balancer, API gateway, VPN, or
reverse proxy layer.

Recommended controls:

- Put production write paths behind authentication or equivalent anti-abuse
  controls.
- Allow proposal creation for any federation, but rate-limit by account, IP,
  federation fingerprint, and request size.
- Restrict signature submission to federation signer credentials when signer
  authentication is available.
- Restrict broadcast to federation/operator credentials or a small allowlist.
- Apply per-IP, per-account, and per-federation quotas.
- Set request body size limits.
- Set helper subprocess timeout and concurrency limits.
- Apply WAF rules for obvious invalid payload floods.
- Monitor helper error rate and latency.
- Separate public read access from private write access if needed.

Failure mode under DDoS:

- service may become slow or unavailable
- Postgres connections may saturate
- helper subprocesses may consume CPU
- disk may grow from valid-looking spam
- Kaspa/Igra RPCs may be pressured by broadcast or verification paths

Funds remain protected as long as signer wallets verify locally and do not sign
bad proposals.

## Recommended Production Controls

Minimum production controls:

- TLS at the load balancer.
- Authentication on every mutating Kaspa endpoint.
- Body size limits at the load balancer.
- Per-IP and per-credential rate limits.
- Separate credentials for:
  - Proposal Builder
  - signers
  - broadcasters
  - administrators
- Postgres backups with restore tests.
- DB migration runbook.
- Structured logs with request ids.
- Metrics for API latency, error rate, helper duration, DB size, and DB
  connections.
- Alerts for disk usage, DB connection saturation, helper timeout spikes, and
  broadcast failures.

Better production controls:

- Run API nodes horizontally behind a load balancer.
- Run Postgres on managed HA storage or a managed database service.
- Store large raw artifacts in object storage instead of Postgres.
- Make evidence blobs immutable and addressable by hash.
- Use private networking between API, DB, Redis, and helper runtime.
- Use a signer/operator VPN or allowlist for write endpoints.
- Run Proposal Builder outside the public API deployment.

## Suggested Hardware Profiles

These are starting points, not final benchmark results.

### Devnet Or Staging

```text
API node:
  2 vCPU
  4 GB RAM

Postgres:
  2 vCPU
  4-8 GB RAM
  50-100 GB SSD

Redis:
  1 vCPU
  1 GB RAM
```

### Production MVP

```text
API:
  2 nodes preferred
  4 vCPU each
  8 GB RAM each

Postgres:
  4-8 vCPU
  16-32 GB RAM
  250-500 GB NVMe/SSD

Redis:
  1-2 vCPU
  2-4 GB RAM
```

### Larger Deployment

```text
API:
  2-4 nodes
  4-8 vCPU each
  8-16 GB RAM each

Postgres:
  8-16 vCPU
  32-64 GB RAM
  1 TB+ NVMe/SSD

Redis:
  2+ vCPU
  4+ GB RAM

Object storage:
  recommended for raw evidence artifacts
```

Scale based on:

- number of federations
- proposal frequency
- exit count per proposal
- evidence size
- signer count
- public read traffic
- write endpoint exposure

## Operational Runbook

Before enabling Kaspa endpoints:

1. Apply migrations.
2. Install and configure `kaspa-pst`.
3. Confirm `KASPA_PST_HELPER_PATH`.
4. Confirm `KASPA_PST_HELPER_TIMEOUT`.
5. Create operator credentials or network allowlists.
6. Create the federation record.
7. Run one devnet proposal/sign/broadcast rehearsal.
8. Confirm backups include Kaspa tables.
9. Confirm monitoring and alerts.

For each production proposal window:

1. Proposal Builder creates or reuses one exit batch.
2. Proposal Builder submits one or more candidate proposals for that batch.
3. Operators/signers choose the intended candidate by `proposal_hash`.
4. Signers verify locally and submit signatures only for the selected candidate.
5. Operators watch quorum.
6. Broadcast after the selected proposal status becomes `ready`.
7. Verify broadcast tx ids.
8. Archive evidence hash, proposal hash, and broadcast tx ids.

If something goes wrong:

- Do not delete records manually as the first response.
- Mark/cancel through application paths when available.
- Preserve evidence and logs.
- If another candidate is needed, submit it as a new proposal hash linked to the
  same exit batch when the evidence is unchanged.
- Signers must verify every candidate from scratch.
- Do not reuse signatures from a stale, failed, or different proposal.

## Key Metrics

Track at minimum:

- request rate by endpoint
- 4xx and 5xx rate by endpoint
- helper command duration and timeout count
- Postgres connection count
- Postgres database size
- Kaspa table sizes
- evidence response size
- proposal count by status
- signature submissions by proposal
- broadcast attempts and failures
- Proposal Builder success/failure per window
- disk free space
- backup success and restore test age

## Security Notes

Safe Transaction Service is not the last line of defense for funds. Signer
wallets are.

A compromised service can:

- hide proposals
- spam proposals
- provide bad evidence
- delay or block signatures
- attempt to broadcast repeatedly
- waste infrastructure resources

A compromised service should not be able to move custody funds without signer
wallets signing a bad transaction. That is why signer-side verification is
mandatory.

Do not weaken signer verification to improve operator convenience.
