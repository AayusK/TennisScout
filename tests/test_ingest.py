from tennisscout import db, ingest_tml
from tests.conftest import snap


def test_identity_checks(loaded):
    conn, _, report, _ = loaded
    assert report.identity_status == {"ok": 8, "id_name_conflict": 1}
    note = conn.execute("SELECT identity_note FROM match WHERE identity_status = 'id_name_conflict'").fetchone()[0]
    assert "Dan Delta" in note and "Carl Gamma" in note


def test_conflicting_row_names_stay_out_of_the_name_index(loaded):
    conn, *_ = loaded
    # The conflicting row files "Dan Delta" under Carl Gamma's ID; that pairing must not reach the index.
    rows = conn.execute(
        "SELECT DISTINCT p.display_name FROM player_name n JOIN player p USING (player_id) WHERE n.name = 'Dan Delta'"
    ).fetchall()
    assert [r[0] for r in rows] == ["Dan Delta"]


def test_replay_order_follows_rounds_not_file_order(loaded):
    conn, *_ = loaded
    rounds = [r[0] for r in conn.execute(
        "SELECT m.round FROM match m JOIN tournament_edition e USING (edition_id) WHERE e.source_key = '2024-339' ORDER BY m.seq"
    )]
    assert rounds == ["SF", "SF", "F"]


def test_timestamps(loaded):
    conn, *_ = loaded
    r = conn.execute("SELECT * FROM match WHERE source_key = '2024-339#3'").fetchone()
    assert r["event_time_precision"] == "tourney_week"
    assert r["event_date_lo"] == "2024-01-01" and r["event_date_hi"] == "2024-01-09"
    assert r["published_at"] is None
    assert r["collected_at"] == "2026-10-29T00:00:00Z"
    assert r["available_at"] == "2024-01-09T23:59:59Z"
    assert r["outcome"] == "completed"


def test_reingest_keeps_ids(tmp_path, loaded, mirrors):
    conn, reg, *_ = loaded
    before = dict(conn.execute("SELECT source_key, match_id FROM match").fetchall())
    players_before = dict(conn.execute("SELECT external_id, player_id FROM player_external_id").fetchall())
    conn2 = db.connect(tmp_path / "fresh.db")
    db.init(conn2)
    ingest_tml.ingest(conn2, snap("tml", mirrors[0]), registry_dir=reg)
    assert dict(conn2.execute("SELECT source_key, match_id FROM match").fetchall()) == before
    assert dict(conn2.execute("SELECT external_id, player_id FROM player_external_id").fetchall()) == players_before


def test_venue_metadata_is_tagged_unverified(loaded):
    conn, *_ = loaded
    r = conn.execute("SELECT * FROM tournament_edition WHERE source_key = '2024-339'").fetchone()
    assert r["venue_city"] == "Brisbane"
    assert r["venue_source"] == "manual_seed_unverified"
