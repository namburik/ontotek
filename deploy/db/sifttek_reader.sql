-- Least-privilege reader role for the separate `sifttek` Postgres database
-- (personal-finance app; role/db `sifttek` already exist and own the data —
-- this does NOT touch that role, it only adds a read-only sibling for
-- Cube's query engine and OpenMetadata-driven allowed_values lookups).
--
-- Usage:
--   READER_PW=$(openssl rand -base64 24)
--   psql "postgresql://namburi@localhost:5432/sifttek" -v reader_password="$READER_PW" -f db/sifttek_reader.sql

BEGIN;

CREATE ROLE sifttek_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE sifttek TO sifttek_reader;
GRANT USAGE ON SCHEMA public TO sifttek_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sifttek_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sifttek_reader;

-- items holds Plaid's encrypted OAuth access token and an internal sync
-- cursor — neither is a semantic/BI column, and the token is a credential.
-- Revoke table-level SELECT on items and re-grant it column-by-column,
-- excluding those two (column-level revoke alone would NOT work here since
-- a table-level grant already permits every column regardless).
REVOKE SELECT ON items FROM sifttek_reader;
GRANT SELECT (
    id, plaid_item_id, institution_id, institution_name,
    created_at, last_synced_at, last_sync_status,
    last_sync_needs_reconnect
) ON items TO sifttek_reader;

COMMIT;
