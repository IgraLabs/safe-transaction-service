# SPDX-License-Identifier: FSL-1.1-MIT
import json
import shlex
import time
from dataclasses import replace
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from safe_transaction_service.kaspa.models import KaspaFederation
from safe_transaction_service.kaspa.services.exit_proposal import (
    FoundryExitBuilder,
    KaspaExitProposalBuilder,
    KaspaExitProposalBuilderError,
    KaspaRpcUtxoSelector,
    KaspaUtxoSelectorConfig,
    KebBundleRunner,
    KebBundleRunnerConfig,
    L2FinalityClient,
    load_builder_config,
)


class Command(BaseCommand):
    help = "Build and submit a Kaspa multisig proposal from verified Igra exit artifacts"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="Proposal-builder JSON config")
        parser.add_argument("--federation", required=True, help="Kaspa federation UUID")
        parser.add_argument("--bundle-dir", help="Verified KEB .bundle directory")
        parser.add_argument(
            "--daemon",
            action="store_true",
            help="Run continuously: build one finalized window, sleep, repeat",
        )
        parser.add_argument(
            "--poll-seconds",
            type=int,
            default=300,
            help="Daemon sleep interval after a skipped/failed/successful cycle",
        )
        parser.add_argument(
            "--locking-utxos-json",
            help=(
                "Manual override JSON array of bridge UTXOs. Service mode normally "
                "selects UTXOs from configured Kaspa RPC instead."
            ),
        )
        parser.add_argument(
            "--build-input-json",
            help="Prebuilt cast igra build-exit input JSON",
        )
        parser.add_argument("--unsigned-json", help="Prebuilt unsigned manifest JSON")
        parser.add_argument("--unsigned-hex", help="Prebuilt unsigned PST hex file")
        parser.add_argument(
            "--unsigned-verify-json",
            help="Prebuilt cast igra verify-exit JSON report",
        )
        parser.add_argument(
            "--fee-sompi",
            type=int,
            default=1_000_000,
            help="Fee used when generating build input from locking UTXOs",
        )
        parser.add_argument(
            "--cast-bin",
            default="cast",
            help="Foundry cast binary with the Igra subcommands",
        )
        parser.add_argument(
            "--foundry-timeout",
            type=int,
            default=180,
            help="Timeout for Foundry build/verify subprocesses",
        )
        parser.add_argument(
            "--mining-timeout-secs",
            type=int,
            default=120,
            help="Payload nonce mining timeout passed to cast igra build-exit",
        )
        parser.add_argument(
            "--allow-non-igra-lock-script-for-testing",
            action="store_true",
            help="Forward Foundry's testing-only non-Igra lock-script flag",
        )
        parser.add_argument(
            "--skip-finality-check",
            action="store_true",
            help="Do not query Igra RPC readiness for bundle toBlock",
        )
        parser.add_argument(
            "--skip-submit",
            action="store_true",
            help="Create a verified KaspaExitBatch but do not create a proposal",
        )
        self._add_keb_arguments(parser)
        self._add_utxo_arguments(parser)

    def handle(self, *args, **options):
        config = load_builder_config(options["config"])
        try:
            federation = KaspaFederation.objects.get(pk=options["federation"])
        except KaspaFederation.DoesNotExist as exc:
            raise CommandError("Federation not found") from exc

        builder = KaspaExitProposalBuilder(config=config, federation=federation)
        if options["daemon"]:
            while True:
                try:
                    output = self._run_once(builder, config, options)
                    if output is not None:
                        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))
                except CommandError as exc:
                    self.stderr.write(self.style.ERROR(str(exc)))
                time.sleep(options["poll_seconds"])

        output = self._run_once(builder, config, options)
        if output is not None:
            self.stdout.write(json.dumps(output, indent=2, sort_keys=True))

    def _run_once(self, builder, config, options):
        bundle_dir = self._resolve_bundle_dir(config, options)
        if bundle_dir is None:
            return {
                "status": "skipped",
                "reason": "next KEB window is not finalized yet",
            }

        build_input = self._load_optional_json(options.get("build_input_json"))
        unsigned_manifest = self._load_optional_json(options.get("unsigned_json"))
        unsigned_bundle_hex = self._load_optional_text(options.get("unsigned_hex"))
        unsigned_verify_report = self._load_optional_json(
            options.get("unsigned_verify_json")
        )

        if not options["skip_finality_check"]:
            to_block = self._bundle_to_block(bundle_dir)
            ready = L2FinalityClient(config.igra_rpc_url).is_ready(
                to_block, config.l2_confirmation_blocks
            )
            if not ready:
                raise CommandError(
                    f"Bundle toBlock {to_block} is not finalized by {config.igra_rpc_url}"
                )

        try:
            if unsigned_manifest is None or unsigned_bundle_hex is None:
                if build_input is None:
                    locking_utxos = self._resolve_locking_utxos(
                        builder=builder,
                        config=config,
                        bundle_dir=bundle_dir,
                        options=options,
                    )
                    if not isinstance(locking_utxos, list):
                        raise CommandError("--locking-utxos-json must contain a JSON array")
                    build_input = builder.build_foundry_input(
                        bundle_dir=bundle_dir,
                        locking_utxos=locking_utxos,
                        fee_sompi=options["fee_sompi"],
                    )

                foundry = FoundryExitBuilder(
                    cast_bin=options["cast_bin"],
                    timeout=options["foundry_timeout"],
                    allow_non_igra_lock_script_for_testing=options[
                        "allow_non_igra_lock_script_for_testing"
                    ],
                )
                unsigned_manifest, unsigned_bundle_hex, unsigned_verify_report = (
                    foundry.build_exit(
                        network=config.network,
                        tx_id_prefix=config.kaspa_tx_id_prefix,
                        build_input=build_input,
                        mining_timeout_secs=options["mining_timeout_secs"],
                    )
                )
            elif unsigned_verify_report is None:
                raise CommandError(
                    "--unsigned-verify-json is required when prebuilt unsigned artifacts are used"
                )

            result = builder.build_from_artifacts(
                bundle_dir=bundle_dir,
                unsigned_manifest=unsigned_manifest,
                unsigned_bundle_hex=unsigned_bundle_hex,
                unsigned_verify_report=unsigned_verify_report,
                build_input=build_input,
                submit_proposal=not options["skip_submit"],
            )
        except KaspaExitProposalBuilderError as exc:
            raise CommandError(str(exc)) from exc

        result.exit_batch.refresh_from_db()
        output = {
            "status": "created",
            "bundleDir": str(bundle_dir),
            "exitBatch": str(result.exit_batch.pk),
            "exitBatchStatus": result.exit_batch.status,
            "evidenceHash": result.evidence_hash,
            "proposalHash": result.proposal.proposal_hash if result.proposal else None,
            "kaspaTxId": result.unsigned_manifest.get("protocol", {}).get("kaspa_tx_id"),
        }
        return output

    def _resolve_bundle_dir(self, config, options) -> Path | None:
        if options.get("bundle_dir"):
            return Path(options["bundle_dir"])

        keb_config = self._keb_config(config, options)
        if keb_config is None:
            raise CommandError("--bundle-dir or builder config keb section is required")

        runner = KebBundleRunner(keb_config)
        next_window = runner.next_window()
        if next_window and not options["skip_finality_check"]:
            _, to_block = next_window
            ready = L2FinalityClient(config.igra_rpc_url).is_ready(
                to_block, config.l2_confirmation_blocks
            )
            if not ready:
                return None
        try:
            return runner.run_delta()
        except KaspaExitProposalBuilderError as exc:
            raise CommandError(str(exc)) from exc

    def _resolve_locking_utxos(self, *, builder, config, bundle_dir, options):
        locking_utxos_path = options.get("locking_utxos_json")
        if locking_utxos_path:
            return self._load_required_json(locking_utxos_path)

        utxo_config = self._utxo_config(config, options)
        if utxo_config is None:
            raise CommandError(
                "kaspaUtxos config or --locking-utxos-json is required "
                "when unsigned artifacts are not provided"
            )
        required_sompi = builder.required_input_sompi(
            bundle_dir=bundle_dir,
            fee_sompi=options["fee_sompi"],
        )
        selector = KaspaRpcUtxoSelector(utxo_config)
        try:
            return selector.select_locking_utxos(
                network=config.network,
                bridge_address=config.canonical_bridge_address,
                bridge_script_public_key=config.canonical_bridge_script_public_key,
                derivation_path=config.canonical_derivation_path,
                required_sompi=required_sompi,
            )
        except KaspaExitProposalBuilderError as exc:
            raise CommandError(str(exc)) from exc

    def _keb_config(self, config, options) -> KebBundleRunnerConfig | None:
        base = config.keb
        overrides = {}
        option_map = {
            "keb_config": "config_path",
            "keb_reports_dir": "reports_dir",
            "keb_runner_cwd": "runner_cwd",
            "keb_delta_blocks": "delta_blocks",
            "keb_start_block": "start_block",
            "keb_end_block": "end_block",
            "keb_previous_checkpoint_file": "previous_checkpoint_file",
            "keb_contract_expected_values_file": "contract_expected_values_file",
            "keb_manifest_signing_private_key": "manifest_signing_private_key",
            "keb_manifest_signing_public_key": "manifest_signing_public_key",
            "keb_manifest_signing_key_id": "manifest_signing_key_id",
            "keb_manifest_signing_key_type": "manifest_signing_key_type",
            "keb_timeout": "timeout",
        }
        for option_name, field_name in option_map.items():
            value = options.get(option_name)
            if value not in (None, ""):
                overrides[field_name] = value
        if options.get("keb_runner_command"):
            overrides["runner_command"] = shlex.split(options["keb_runner_command"])
        if options.get("keb_cleanup_created_outside_bundle"):
            overrides["cleanup_created_outside_bundle"] = True

        if base is None and not overrides:
            return None
        return replace(base or KebBundleRunnerConfig(), **overrides)

    def _utxo_config(self, config, options) -> KaspaUtxoSelectorConfig | None:
        base = config.kaspa_utxos
        overrides = {}
        option_map = {
            "kaspa_rpc_url": "rpc_url",
            "kaspa_utxo_api_url": "api_url",
            "kaspa_utxo_timeout": "timeout",
            "coinbase_maturity_daa": "coinbase_maturity_daa",
            "min_utxo_confirmations_daa": "min_confirmations_daa",
            "min_utxo_amount_sompi": "min_amount_sompi",
            "max_utxo_inputs": "max_inputs",
        }
        for option_name, field_name in option_map.items():
            value = options.get(option_name)
            if value not in (None, ""):
                overrides[field_name] = value
        if options.get("kaspa_utxo_helper_command"):
            overrides["helper_command"] = shlex.split(
                options["kaspa_utxo_helper_command"]
            )

        if base is None and not overrides:
            return None
        candidate = replace(base or KaspaUtxoSelectorConfig(), **overrides)
        if candidate.rpc_url and not candidate.helper_command and not candidate.api_url:
            candidate = replace(candidate, helper_command=["kaspa-pst", "utxos"])
        return candidate

    def _add_keb_arguments(self, parser):
        parser.add_argument("--keb-config", help="kasExitBridge runDelta --config path")
        parser.add_argument("--keb-reports-dir", help="KEB reports/bundles directory")
        parser.add_argument(
            "--keb-runner-cwd",
            help="Working directory for npm run kas-exit:run-delta",
        )
        parser.add_argument(
            "--keb-runner-command",
            help='Command for KEB runner, default: "npm run kas-exit:run-delta --"',
        )
        parser.add_argument("--keb-delta-blocks", type=int)
        parser.add_argument("--keb-start-block", type=int)
        parser.add_argument("--keb-end-block", type=int)
        parser.add_argument("--keb-previous-checkpoint-file")
        parser.add_argument("--keb-contract-expected-values-file")
        parser.add_argument("--keb-manifest-signing-private-key")
        parser.add_argument("--keb-manifest-signing-public-key")
        parser.add_argument("--keb-manifest-signing-key-id")
        parser.add_argument("--keb-manifest-signing-key-type")
        parser.add_argument("--keb-timeout", type=int)
        parser.add_argument(
            "--keb-cleanup-created-outside-bundle",
            action="store_true",
            help="Delete top-level artifacts that runDelta also put inside the bundle",
        )

    def _add_utxo_arguments(self, parser):
        parser.add_argument("--kaspa-rpc-url", help="Kaspa node RPC URL/address")
        parser.add_argument(
            "--kaspa-utxo-helper-command",
            help='Kaspa RPC UTXO helper command, default: "kaspa-pst utxos"',
        )
        parser.add_argument("--kaspa-utxo-api-url", help="HTTP UTXO API fallback")
        parser.add_argument("--kaspa-utxo-timeout", type=int)
        parser.add_argument("--coinbase-maturity-daa", type=int)
        parser.add_argument("--min-utxo-confirmations-daa", type=int)
        parser.add_argument("--min-utxo-amount-sompi", type=int)
        parser.add_argument("--max-utxo-inputs", type=int)

    def _bundle_to_block(self, bundle_dir: Path) -> int:
        manifest = self._load_required_json(bundle_dir / "manifest.json")
        return int(manifest["context"]["toBlock"])

    def _load_optional_json(self, path: str | None):
        if not path:
            return None
        return self._load_required_json(path)

    def _load_required_json(self, path):
        with Path(path).open() as fp:
            return json.load(fp)

    def _load_optional_text(self, path: str | None) -> str | None:
        if not path:
            return None
        return Path(path).read_text().strip()
