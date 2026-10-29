import pytest

from tennisscout.registry import Registry


@pytest.mark.parametrize("fname", ["players.csv", "matches.csv.gz"])
def test_ids_are_permanent_across_reloads(tmp_path, fname):
    path = tmp_path / fname
    r = Registry(path, "P", 6)
    a = r.get_or_mint("atp", "AAA1", "t0")
    b = r.get_or_mint("atp", "BBB2", "t0")
    assert (a, b) == ("P000001", "P000002")
    r.save()
    first_bytes = path.read_bytes()

    r2 = Registry(path, "P", 6)
    assert r2.get_or_mint("atp", "BBB2") == b  # existing key keeps its ID
    assert r2.get_or_mint("atp", "CCC3", "t1") == "P000003"  # new key appends
    r2.save()

    r3 = Registry(path, "P", 6)
    assert r3.lookup("atp", "AAA1") == a
    r3.save()  # no change -> no rewrite
    assert len(r3) == 3
    assert path.read_bytes() != first_bytes


def test_gzip_output_is_deterministic(tmp_path):
    for d in ("x", "y"):
        r = Registry(tmp_path / d / "m.csv.gz", "M", 7)
        r.get_or_mint("tml", "k1", "t0")
        r.save()
    assert (tmp_path / "x" / "m.csv.gz").read_bytes() == (tmp_path / "y" / "m.csv.gz").read_bytes()


def test_alias_refuses_to_remap(tmp_path):
    r = Registry(tmp_path / "p.csv", "P", 6)
    a = r.get_or_mint("atp", "AAA1")
    b = r.get_or_mint("atp", "BBB2")
    r.alias(a, "sportradar", "sr:1")
    with pytest.raises(ValueError):
        r.alias(b, "sportradar", "sr:1")
