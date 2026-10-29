from tennisscout.normalize import names_compatible, norm_name, outcome, round_order, surface_key


def test_norm_name_strips_accents_case_punctuation():
    assert norm_name("Félix Auger-Aliassime") == "felix auger aliassime"
    assert norm_name("  Alex  de Minaur ") == "alex de minaur"
    assert norm_name("'s-Hertogenbosch") == "s hertogenbosch"


def test_names_compatible_accepts_variants():
    assert names_compatible("Konstantin Pugaev", "Konstantin Pugayev")
    assert names_compatible("Albert Ramos", "Albert Ramos-Vinolas")
    assert names_compatible("Daniel Vallejo", "Adolfo Daniel Vallejo")
    assert names_compatible("Bu Yunchaokete", "Yunchaokete Bu")


def test_names_compatible_rejects_different_people():
    # The real TML row that files Marc-Andrea Huesler under John Millman's ID.
    assert not names_compatible("Marc-Andrea Huesler", "John Millman")
    assert not names_compatible("Ivan Lendl", "Jimmy Connors")
    assert not names_compatible("Terence Atmane", "Daniel Altmaier")


def test_outcome():
    assert outcome("6-4 6-4") == "completed"
    assert outcome("6-4 3-1 RET") == "retired"
    assert outcome("W/O") == "walkover"
    assert outcome("6-1 DEF") == "default"
    assert outcome("") == "unknown"


def test_surface_key():
    assert surface_key("Hard", "I") == "IndoorHard"
    assert surface_key("Hard", "I", split_indoor_hard=False) == "Hard"
    assert surface_key("Hard", "") == "Hard"
    assert surface_key("clay", "O") == "Clay"
    assert surface_key("", "O") == "Unknown"


def test_round_order_is_chronological():
    rounds = ["Q1", "R128", "R64", "R32", "R16", "RR", "QF", "SF", "F"]
    assert [round_order(r) for r in rounds] == sorted(round_order(r) for r in rounds)
