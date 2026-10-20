#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

: "${NOVATEK_READER_PASSWORD:?set in .env}"
: "${OPENMETADATA_JWT_TOKEN:?set in .env}"

sed \
  -e "s|__READER_PW__|${NOVATEK_READER_PASSWORD}|" \
  -e "s|__OM_JWT__|${OPENMETADATA_JWT_TOKEN}|" \
  novatek_ingestion.yaml.tmpl > novatek_ingestion.rendered.yaml

echo "Wrote $(pwd)/novatek_ingestion.rendered.yaml"
