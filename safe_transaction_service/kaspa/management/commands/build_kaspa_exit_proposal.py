# SPDX-License-Identifier: FSL-1.1-MIT
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from safe_transaction_service.kaspa.models import KaspaFederation
from safe_transaction_service.kaspa.services.exit_proposal import (
    FoundryExitBuilder,
    KaspaExitProposalBuilder,
    KaspaExitProposalBuilderError,
    L2FinalityClient,
    load_builder_config,
)


class Command(BaseCommand):
    help = "Build and submit a Kaspa multisig proposal from verified Igra exit artifacts"

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="Proposal-builder JSON config")
        parser.add_argument("--federation", required=True, help="Kaspa federation UUID")
        parser.add_argument("--bundle-dir", required=True, help="Verified KEB .bundle directory")
        parser.add_argument(
            "--locking-utxos-json",
            help="JSON array of live bridge locking UTXOs for Foundry build-exit",
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

    def handle(self, *args, **options):
        config = load_builder_config(options["config"])
        try:
            federation = KaspaFederation.objects.get(pk=options["federation"])
        except KaspaFederation.DoesNotExist as exc:
            raise CommandError("Federation not found") from exc

        builder = KaspaExitProposalBuilder(config=config, federation=federation)
        bundle_dir = Path(options["bundle_dir"])

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
                    locking_utxos_path = options.get("locking_utxos_json")
                    if not locking_utxos_path:
                        raise CommandError(
                            "--locking-utxos-json or --build-input-json is required "
                            "when unsigned artifacts are not provided"
                        )
                    locking_utxos = self._load_required_json(locking_utxos_path)
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

        output = {
            "exitBatch": str(result.exit_batch.pk),
            "exitBatchStatus": result.exit_batch.status,
            "evidenceHash": result.evidence_hash,
            "proposalHash": result.proposal.proposal_hash if result.proposal else None,
            "kaspaTxId": result.unsigned_manifest.get("protocol", {}).get("kaspa_tx_id"),
        }
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))

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
