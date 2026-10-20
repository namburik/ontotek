#!/usr/bin/env bash
# Renders postgres_ingestion.yaml.tmpl into postgres_ingestion.rendered.yaml
# (gitignored) by substituting secrets from the environment. Avoids relying
# on OpenMetadata's own YAML env-var interpolation, whose exact convention
# varies by version.
set -euo pipefail
cd "$(dirname "$0")"

: "${OPENMETADATA_DB_SERVICE_NAME:?set in .env}"
: "${ONTOTEK_READER_PASSWORD:?set in .env}"
: "${OPENMETADATA_JWT_TOKEN:?set in .env}"

sed \
  -e "s|__OM_SERVICE_NAME__|${OPENMETADATA_DB_SERVICE_NAME}|" \
  -e "s|__READER_PW__|${ONTOTEK_READER_PASSWORD}|" \
  -e "s|__OM_JWT__|${OPENMETADATA_JWT_TOKEN}|" \
  postgres_ingestion.yaml.tmpl > postgres_ingestion.rendered.yaml

echo "Wrote $(pwd)/postgres_ingestion.rendered.yaml"
