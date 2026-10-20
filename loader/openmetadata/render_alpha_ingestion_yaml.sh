#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

: "${ONTOTEK_READER_PASSWORD:?set in .env}"
: "${OPENMETADATA_JWT_TOKEN:?set in .env}"

sed \
  -e "s|__READER_PW__|${ONTOTEK_READER_PASSWORD}|" \
  -e "s|__OM_JWT__|${OPENMETADATA_JWT_TOKEN}|" \
  alpha_ingestion.yaml.tmpl > alpha_ingestion.rendered.yaml

echo "Wrote $(pwd)/alpha_ingestion.rendered.yaml"
