from tennisscout import ingest_api_tennis as ia
from tennisscout import registry


def test_score_from_winner_side():
    scores = [{"score_first": "0", "score_second": "6", "score_set": "1"},
              {"score_first": "6.3", "score_second": "7.7", "score_set": "2"}]
    assert ia._score(scores, winner_first=False) == "6-0 7-6(3)"
    assert ia._score(scores, winner_first=True) == "0-6 6-7(3)"


def test_birthdate_and_serve_stats():
    assert ia._bday("12.09.1998") == "19980912"
    assert ia._bday(None) is None
    stats = [{"player_key": 1, "stat_period": "match", "stat_name": "1st serve points won", "stat_won": 32, "stat_total": 51},
             {"player_key": 1, "stat_period": "match", "stat_name": "2nd serve points won", "stat_won": 10, "stat_total": 30},
             {"player_key": 1, "stat_period": "set1", "stat_name": "2nd serve points won", "stat_won": 1, "stat_total": 9}]
    assert ia._serve(stats, 1) == (81, 51, 32, 10)
    assert ia._serve(stats, 2) == (None, None, None, None)


def test_player_mapping(loaded, tmp_path):
    conn, reg, *_ = loaded
    preg = registry.players(reg)
    api_players = {
        10: {"player_full_name": "Alex Alpha", "player_bday": "01.01.1999"},  # birthdate matches
        11: {"player_full_name": "John Smith", "player_bday": "01.01.1992"},  # homonym, birthdate picks one
        12: {"player_full_name": "Ben Beta", "player_bday": "05.05.1990"},  # same name, different birthdate
        13: {"player_full_name": "Zed Newcomer", "player_bday": None},  # not in our data
    }
    pmap, how, new_rows = ia.map_players(conn, api_players, preg)
    ext = dict(conn.execute("SELECT external_id, player_id FROM player_external_id WHERE source = 'atp'").fetchall())
    assert pmap[10] == ext["AAA1"]
    assert pmap[11] == ext["FFF6"]
    assert pmap[12] not in ext.values()  # a different Ben Beta gets his own ID
    assert pmap[13] not in ext.values()
    assert how["name+birthdate"] == 2 and how["new_player"] == 2
    assert len(new_rows) == 2
