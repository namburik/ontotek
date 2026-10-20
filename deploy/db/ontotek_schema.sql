-- OntoTek demo schema, fully isolated inside the existing `mltek` database.
-- Never touches the pre-existing `alpha` or `beta` schemas or their data.
-- Usage:
--   READER_PW=$(openssl rand -base64 24)
--   psql "postgresql://namburi@localhost:5432/mltek" -v reader_password="$READER_PW" -f db/ontotek_schema.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS ontotek;

CREATE TABLE ontotek.movietek_catalog (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    genre_name    TEXT NOT NULL,
    release_year  INT
);

CREATE TABLE ontotek.movietek_sales (
    id                 SERIAL PRIMARY KEY,
    movie_id           INT NOT NULL REFERENCES ontotek.movietek_catalog(id),
    gross_revenue      NUMERIC(14,2) NOT NULL,
    distribution_fees  NUMERIC(14,2) NOT NULL,
    sale_date          DATE NOT NULL DEFAULT CURRENT_DATE
);

CREATE TABLE ontotek.stocktek_equities (
    id             SERIAL PRIMARY KEY,
    ticker_symbol  TEXT NOT NULL UNIQUE,
    cap_tier       TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO ontotek.movietek_catalog (title, genre_name, release_year) VALUES
    ('Stellar Drift',      'Sci-Fi', 2023),
    ('Neon Heist',         'Action', 2022),
    ('Quiet Harbor',       'Drama',  2021),
    ('The Wedding Plan B', 'Comedy', 2023),
    ('Orbital Decay',      'Sci-Fi', 2024),
    ('Midnight Pursuit',   'Action', 2020),
    ('Autumn Letters',     'Drama',  2019),
    ('Office Chaos',       'Comedy', 2022);

INSERT INTO ontotek.movietek_sales (movie_id, gross_revenue, distribution_fees, sale_date) VALUES
    (1, 82000000.00, 9500000.00, '2023-07-14'),
    (2, 41000000.00, 5200000.00, '2022-03-02'),
    (3, 12500000.00, 1800000.00, '2021-11-19'),
    (4, 28000000.00, 3100000.00, '2023-02-10'),
    (5, 95000000.00, 11000000.00, '2024-06-21'),
    (6, 33000000.00, 4200000.00, '2020-09-05'),
    (7, 9800000.00,  1500000.00, '2019-10-30'),
    (8, 21000000.00, 2700000.00, '2022-12-16');

INSERT INTO ontotek.stocktek_equities (ticker_symbol, cap_tier, is_active) VALUES
    ('MTKX', 'MEGA',  TRUE),
    ('STKY', 'SMALL', FALSE),
    ('LRGV', 'LARGE', TRUE),
    ('MIDC', 'MID',   TRUE),
    ('MEGB', 'MEGA',  TRUE),
    ('SMLQ', 'SMALL', TRUE),
    ('LRGW', 'LARGE', FALSE),
    ('MIDD', 'MID',   TRUE);

-- Least-privilege role for server.py's POSTGRES_READ_ONLY_URI and for
-- OpenMetadata's ingestion connector. Scoped ONLY to the ontotek schema.
CREATE ROLE ontotek_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE mltek TO ontotek_reader;
GRANT USAGE ON SCHEMA ontotek TO ontotek_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ontotek TO ontotek_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ontotek GRANT SELECT ON TABLES TO ontotek_reader;

-- Lets server.py's existing bare table names ("movietek_sales", not
-- "ontotek.movietek_sales") keep resolving correctly with no code change.
ALTER ROLE ontotek_reader SET search_path = ontotek, public;

COMMIT;
