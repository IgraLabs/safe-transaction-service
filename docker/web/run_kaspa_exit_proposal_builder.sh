#!/usr/bin/env bash
set -euo pipefail

: "${KASPA_EXIT_BUILDER_CONFIG:?KASPA_EXIT_BUILDER_CONFIG is required}"
: "${KASPA_FEDERATION_ID:?KASPA_FEDERATION_ID is required}"

poll_seconds="${KASPA_EXIT_BUILDER_POLL_SECONDS:-300}"

exec python manage.py build_kaspa_exit_proposal \
  --config "${KASPA_EXIT_BUILDER_CONFIG}" \
  --federation "${KASPA_FEDERATION_ID}" \
  --daemon \
  --poll-seconds "${poll_seconds}"
