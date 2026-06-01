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

The expected wallet flow is:

```text
1. Wallet creates a kaspawallet_pst_v1 unsigned bundle locally.
2. Wallet posts it to /api/v1/kaspa/federations/{id}/transactions/.
3. Signer wallets fetch the proposal and sign locally.
4. Signer wallets post signed bundles to /api/v1/kaspa/transactions/{hash}/signatures/.
5. The service accepts only signature-slot additions and stores the merged bundle.
6. Once threshold is met, the proposal becomes ready and can be broadcast.
```
