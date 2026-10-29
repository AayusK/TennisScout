def status(conn, mcp_id):
    return dict(conn.execute("SELECT * FROM charting_match WHERE mcp_match_id = ?", (mcp_id,)).fetchone())


def test_unique_join_links(loaded):
    conn, *_ = loaded
    r = status(conn, "c1")
    assert r["link_status"] == "linked"
    assert r["start_time"] == "2pm"
    linked = conn.execute("SELECT source_key FROM match WHERE match_id = ?", (r["match_id"],)).fetchone()[0]
    assert linked == "2024-339#3"


def test_homonyms_link_the_match_but_not_the_players(loaded):
    conn, *_ = loaded
    r = status(conn, "c2")
    assert r["link_status"] == "linked"
    assert {r["p1_status"], r["p2_status"]} == {"ambiguous_assignment"}
    assert r["p1_player_id"] is None and r["p2_player_id"] is None


def test_overlapping_windows_are_broken_by_tournament_name(loaded):
    conn, *_ = loaded
    r = status(conn, "c3")
    assert r["link_status"] == "linked"
    assert "tournament name" in r["link_note"]
    tourney = conn.execute(
        "SELECT e.name FROM match m JOIN tournament_edition e USING (edition_id) WHERE m.match_id = ?", (r["match_id"],)
    ).fetchone()[0]
    assert tourney == "Cincinnati Masters"


def test_name_variant_needs_a_unique_join(loaded):
    conn, *_ = loaded
    r = status(conn, "c4")
    assert r["link_status"] == "linked"
    assert r["p1_status"] == "resolved_via_match_fuzzy"


def test_challenger_and_out_of_coverage(loaded):
    conn, *_ = loaded
    assert status(conn, "c5")["link_status"] == "not_tour_level"
    assert status(conn, "c6")["link_status"] == "beyond_source_coverage"
