-- TennisScout schema. Written in the SQL subset shared by SQLite and Postgres so
-- the same DDL can move to Postgres/Supabase later. Timestamps are ISO-8601 UTC
-- text; dates are YYYY-MM-DD text.
--
-- Every fact that can affect a prediction carries three times (spec,
-- "Availability time"): when it happened (event_*), when the source published it
-- (published_at, NULL when the source does not say) and when we stored it
-- (collected_at). available_at is the earliest time we are willing to treat the
-- fact as known, and availability_basis says how it was derived.

CREATE TABLE IF NOT EXISTS source_snapshot (
    snapshot_id   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    revision      TEXT NOT NULL,
    revision_date TEXT,
    collected_at  TEXT NOT NULL,
    license       TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player (
    player_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    name_norm     TEXT NOT NULL,
    hand          TEXT,
    backhand      TEXT,
    birthdate     TEXT,
    height_cm     INTEGER,
    ioc           TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS player_name_norm ON player(name_norm);

CREATE TABLE IF NOT EXISTS player_external_id (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    player_id    TEXT NOT NULL REFERENCES player(player_id),
    PRIMARY KEY (source, external_id)
);

-- Every display name a source has attached to a player, so lookups can find
-- "Konstantin Pugayev" as well as "Konstantin Pugaev".
CREATE TABLE IF NOT EXISTS player_name (
    player_id  TEXT NOT NULL REFERENCES player(player_id),
    name       TEXT NOT NULL,
    name_norm  TEXT NOT NULL,
    source     TEXT NOT NULL,
    n_rows     INTEGER NOT NULL,
    PRIMARY KEY (player_id, name, source)
);
CREATE INDEX IF NOT EXISTS player_name_norm_idx ON player_name(name_norm);

-- A tournament is the event licence (ATP tournament code). It can change
-- city, surface, level and roof over the years, so those live on the edition.
CREATE TABLE IF NOT EXISTS tournament (
    tournament_id TEXT PRIMARY KEY,
    atp_code      TEXT,
    latest_name   TEXT
);

CREATE TABLE IF NOT EXISTS tournament_edition (
    edition_id     TEXT PRIMARY KEY,
    tournament_id  TEXT NOT NULL REFERENCES tournament(tournament_id),
    source_key     TEXT NOT NULL,       -- e.g. TML tourney_id "2024-339"
    year           INTEGER NOT NULL,
    name           TEXT NOT NULL,
    level          TEXT,
    surface        TEXT,
    indoor         TEXT,                -- 'I', 'O' or NULL; tournament-level only
    draw_size      INTEGER,
    start_date     TEXT,
    end_date_bound TEXT,
    date_precision TEXT NOT NULL,       -- 'day' when the source dates each match, else 'tourney_week'
    venue_city     TEXT,
    latitude       REAL,
    longitude      REAL,
    elevation_m    REAL,
    venue_source   TEXT,                -- provenance of the venue fields
    snapshot_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match (
    match_id        TEXT PRIMARY KEY,
    edition_id      TEXT NOT NULL REFERENCES tournament_edition(edition_id),
    seq             INTEGER NOT NULL,   -- global chronological replay order
    round           TEXT,
    round_order     INTEGER,
    best_of         INTEGER,
    winner_id       TEXT,               -- internal player IDs; NULL when unresolved
    loser_id        TEXT,
    winner_src_id   TEXT,
    loser_src_id    TEXT,
    winner_src_name TEXT,
    loser_src_name  TEXT,
    winner_rank     INTEGER,
    loser_rank      INTEGER,
    winner_rank_points INTEGER,
    loser_rank_points  INTEGER,
    winner_entry    TEXT,
    loser_entry     TEXT,
    score           TEXT,
    outcome         TEXT NOT NULL,      -- completed / retired / walkover / default / unknown
    minutes         INTEGER,
    surface         TEXT,
    indoor          TEXT,
    level           TEXT,
    -- serve stats (winner w_, loser l_)
    w_ace INTEGER, w_df INTEGER, w_svpt INTEGER, w_1stin INTEGER, w_1stwon INTEGER, w_2ndwon INTEGER,
    w_svgms INTEGER, w_bpsaved INTEGER, w_bpfaced INTEGER,
    l_ace INTEGER, l_df INTEGER, l_svpt INTEGER, l_1stin INTEGER, l_1stwon INTEGER, l_2ndwon INTEGER,
    l_svgms INTEGER, l_bpsaved INTEGER, l_bpfaced INTEGER,
    -- availability
    event_date_lo   TEXT,
    event_date_hi   TEXT,
    event_time_precision TEXT NOT NULL,
    published_at    TEXT,
    collected_at    TEXT NOT NULL,
    available_at    TEXT,
    availability_basis TEXT NOT NULL,
    -- identity
    identity_status TEXT NOT NULL,      -- ok / id_name_conflict / unknown_id / missing_id / placeholder
    identity_note   TEXT,
    source          TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    source_file     TEXT,
    snapshot_id     TEXT NOT NULL,
    start_time      TEXT                -- scheduled start (UTC) when the source gives one
);
CREATE INDEX IF NOT EXISTS match_seq ON match(seq);
CREATE INDEX IF NOT EXISTS match_winner ON match(winner_id);
CREATE INDEX IF NOT EXISTS match_loser ON match(loser_id);

CREATE TABLE IF NOT EXISTS match_external_id (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    match_id    TEXT NOT NULL REFERENCES match(match_id),
    PRIMARY KEY (source, external_id)
);

-- Match Charting Project records and how (or whether) they link to our IDs.
CREATE TABLE IF NOT EXISTS charting_match (
    mcp_match_id   TEXT PRIMARY KEY,
    match_date     TEXT,
    start_time     TEXT,
    court          TEXT,
    tournament     TEXT,
    round          TEXT,
    surface        TEXT,
    best_of        INTEGER,
    p1_name        TEXT,
    p2_name        TEXT,
    p1_player_id   TEXT,
    p2_player_id   TEXT,
    p1_status      TEXT,                -- resolved / ambiguous / unresolved / resolved_via_match
    p2_status      TEXT,
    match_id       TEXT,                -- linked internal match, NULL if not linked
    link_status    TEXT NOT NULL,       -- linked / no_candidate / ambiguous / player_unresolved / not_tour_level
    link_note      TEXT,
    collected_at   TEXT NOT NULL,
    snapshot_id    TEXT NOT NULL
);

-- Pre-match predictions written by the backtest, one row per (model, match).
CREATE TABLE IF NOT EXISTS prediction (
    model          TEXT NOT NULL,
    match_id       TEXT NOT NULL,
    player1_id     TEXT NOT NULL,
    player2_id     TEXT NOT NULL,
    p_player1      REAL NOT NULL,
    player1_won    INTEGER NOT NULL,
    as_of_seq      INTEGER NOT NULL,   -- prediction uses only matches with seq < this
    confidence     TEXT,
    PRIMARY KEY (model, match_id)
);

-- Current ratings after replaying all history (what the API serves).
CREATE TABLE IF NOT EXISTS elo_rating (
    model          TEXT NOT NULL,
    player_id      TEXT NOT NULL,
    bucket         TEXT NOT NULL,      -- 'Overall' or a surface key
    rating         REAL NOT NULL,
    n_matches      INTEGER NOT NULL,
    last_date      TEXT,
    PRIMARY KEY (model, player_id, bucket)
);

-- Serve/return ratings (logit scale) after replaying all history.
CREATE TABLE IF NOT EXISTS sr_rating (
    model          TEXT NOT NULL,
    player_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,      -- 'serve' or 'return'
    mean           REAL NOT NULL,
    var            REAL NOT NULL,
    n_matches      INTEGER NOT NULL,   -- matches with a usable stat line
    PRIMARY KEY (model, player_id, kind)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
