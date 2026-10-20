#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

: "${SIFTTEK_DB_PASSWORD:?set in .env}"
: "${OPENMETADATA_JWT_TOKEN:?set in .env}"

sed \
  -e "s|__SIFTTEK_PW__|${SIFTTEK_DB_PASSWORD}|" \
  -e "s|__OM_JWT__|${OPENMETADATA_JWT_TOKEN}|" \
  sifttek_ingestion.yaml.tmpl > sifttek_ingestion.rendered.yaml

echo "Wrote $(pwd)/sifttek_ingestion.rendered.yaml"
