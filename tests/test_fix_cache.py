import glob
import json

from cf_bypasser.cache.cookie_cache import CookieCache


def _cache_path(tmp_path):
    return str(tmp_path / "cf_cookie_cache.json")


def test_roundtrip_persists_and_reloads(tmp_path):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("example.com", {"cf_clearance": "abc"}, "UA/1.0", ttl_minutes=2)

    fresh = CookieCache(enabled=True, cache_file=path)
    got = fresh.get("example.com")
    assert got is not None
    assert got.cookies == {"cf_clearance": "abc"}
    assert got.user_agent == "UA/1.0"


def test_default_ttl_is_29_minutes(tmp_path):
    c = CookieCache(enabled=True, cache_file=_cache_path(tmp_path))
    c.set("h", {"cf_clearance": "x"}, "UA")  # no ttl -> default
    cached = c.get("h")
    delta_minutes = (cached.expires_at - cached.timestamp).total_seconds() / 60
    assert abs(delta_minutes - 29) < 0.5


def test_exit_ip_round_trips(tmp_path):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("h", {"cf_clearance": "x"}, "UA", exit_ip="203.0.113.7")
    assert CookieCache(enabled=True, cache_file=path).get("h").exit_ip == "203.0.113.7"


def test_on_disk_json_valid(tmp_path):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("example.com", {"k": "v"}, "UA", ttl_minutes=1)

    with open(path) as f:
        data = json.load(f)
    assert "example.com" in data
    assert data["example.com"]["cookies"] == {"k": "v"}


def test_failed_save_does_not_corrupt_existing(tmp_path, monkeypatch):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("good.com", {"cf_clearance": "original"}, "UA", ttl_minutes=2)

    with open(path) as f:
        original_bytes = f.read()
    assert json.loads(original_bytes)["good.com"]["cookies"] == {"cf_clearance": "original"}

    import cf_bypasser.cache.cookie_cache as mod

    def boom(*a, **k):
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr(mod.json, "dump", boom)
    # _save_cache catches and logs; must not propagate or truncate file
    c.set("good.com", {"cf_clearance": "new"}, "UA", ttl_minutes=2)

    with open(path) as f:
        after_bytes = f.read()
    assert after_bytes == original_bytes

    fresh = CookieCache(enabled=True, cache_file=path)
    got = fresh.get("good.com")
    assert got is not None
    assert got.cookies == {"cf_clearance": "original"}


def test_no_leftover_tmp_after_success(tmp_path):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("a.com", {"k": "v"}, "UA")
    leftovers = glob.glob(str(tmp_path / "*.tmp")) + glob.glob(str(tmp_path / ".cf_cache.*"))
    assert leftovers == []


def test_no_leftover_tmp_after_failure(tmp_path, monkeypatch):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    c.set("a.com", {"k": "v"}, "UA")

    import cf_bypasser.cache.cookie_cache as mod
    monkeypatch.setattr(mod.json, "dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    c.set("b.com", {"k": "v"}, "UA")

    leftovers = glob.glob(str(tmp_path / "*.tmp")) + glob.glob(str(tmp_path / ".cf_cache.*"))
    assert leftovers == []


def test_sequential_saves_keep_file_valid(tmp_path):
    path = _cache_path(tmp_path)
    c = CookieCache(enabled=True, cache_file=path)
    for i in range(25):
        c.set(f"host{i}.com", {"n": str(i)}, "UA")
        with open(path) as f:
            data = json.load(f)
        assert f"host{i}.com" in data
    assert len(json.load(open(path))) == 25


def test_unwritable_cache_dir_does_not_raise(tmp_path):
    import os
    from cf_bypasser.cache.cookie_cache import CookieCache
    ro = tmp_path / "ro"
    ro.mkdir()
    cache = CookieCache(str(ro / "cache.json"), enabled=True)
    os.chmod(ro, 0o500)
    try:
        cache.set("k", {"cf_clearance": "x"}, "UA")  # must only log, never fail the request
        assert cache.get("k").cookies == {"cf_clearance": "x"}
    finally:
        os.chmod(ro, 0o700)


def test_disabled_cache_never_touches_disk(tmp_path):
    import json, os
    from cf_bypasser.cache.cookie_cache import CookieCache
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"k": {"key": "k", "cookies": {"a": "1"}, "user_agent": "UA",
                                      "timestamp": "2099-01-01T00:00:00", "expires_at": "2099-01-01T00:00:00"}}))
    cache = CookieCache(str(path), enabled=False)
    assert cache.get("k") is None  # an existing file is not even loaded
    cache.set("x", {"b": "2"}, "UA")
    assert cache.get("x") is None and cache.cache == {}
    assert json.loads(path.read_text()).keys() == {"k"}
