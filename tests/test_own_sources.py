"""--source / --only-sources: own proxy lists from URLs and local files."""

import asyncio

import pytest

from proxyscraper import pipeline
from proxyscraper import sources as srcs
from proxyscraper.fetchcache import FetchCache
from proxyscraper.options import RunOptions
from proxyscraper.ui import CollectView


def test_urls_keep_or_fix_their_type():
    assert srcs.parse_source_spec("https://example.com/list.txt") == ("https://example.com/list.txt", "auto")
    assert srcs.parse_source_spec("socks5=https://example.com/s.txt") == ("https://example.com/s.txt", "socks5")
    assert srcs.parse_source_spec("SOCKS5H=https://example.com/s.txt")[1] == "socks5"
    # an '=' in the query is part of the URL, not a type
    assert srcs.parse_source_spec("https://api.example.com/?type=socks5") == \
        ("https://api.example.com/?type=socks5", "auto")
    # GitHub page links become the raw file, like the built-in sources
    assert srcs.parse_source_spec("https://github.com/a/b/blob/main/p.txt")[0] == \
        "https://raw.githubusercontent.com/a/b/main/p.txt"


def test_local_files_become_file_urls(tmp_path, monkeypatch):
    f = tmp_path / "mine.txt"
    f.write_text("1.2.3.4:80\n")
    monkeypatch.chdir(tmp_path)
    assert srcs.parse_source_spec("mine.txt") == (f.resolve().as_uri(), "auto")
    assert srcs.parse_source_spec(f"http={f}") == (f.resolve().as_uri(), "http")
    assert srcs.parse_source_spec(f.resolve().as_uri())[0] == f.resolve().as_uri()


@pytest.mark.parametrize("spec", ["", "socks5=", "nope.txt", "ftp://example.com/list.txt"])
def test_bad_specs(spec, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        srcs.parse_source_spec(spec)


def test_options_round_trip_and_only_sources_needs_a_source():
    opts = RunOptions(sources=["socks5=https://example.com/s.txt", "mine.txt"], only_sources=True)
    argv = opts.to_argv()
    assert argv == ["--source", "socks5=https://example.com/s.txt", "--source", "mine.txt", "--only-sources"]
    with pytest.raises(ValueError):
        RunOptions(only_sources=True)


def test_only_sources_skips_everything_else(monkeypatch, tmp_path):
    monkeypatch.setattr(srcs, "load_source_file", lambda *a: pytest.fail("built-in sources were loaded"))
    opts = RunOptions(sources=["https://example.com/a.txt", "socks4=https://example.com/b.txt"],
                      only_sources=True, types=["http", "socks5"])
    plan = asyncio.run(pipeline.collect_sources(opts, srcs.SourceStats(tmp_path / "stats.json")))
    # socks4 isn't wanted this run, the untyped list stays
    assert plan.sources == {"https://example.com/a.txt": "auto"} and plan.n_own == 1


def test_own_sources_join_the_others_and_are_never_skipped(monkeypatch, tmp_path):
    builtin, own = "https://example.com/builtin.txt", "https://example.com/own.txt"
    monkeypatch.setattr(srcs, "load_source_file", lambda *a: ({builtin: "http"}, []))
    monkeypatch.setattr(srcs, "load_discovered", lambda *a, **k: {})
    monkeypatch.setattr(srcs, "discovered_age_days", lambda *a: 0.0)

    async def no_meta(meta, get):
        return {}, 0
    monkeypatch.setattr(srcs, "resolve_meta", no_meta)
    quality = srcs.SourceStats(tmp_path / "stats.json")
    monkeypatch.setattr(quality, "skip_now", lambda url: "dead")  # every learned source would be skipped
    opts = RunOptions(sources=[f"http={own}"], no_discover=True)
    plan = asyncio.run(pipeline.collect_sources(opts, quality))
    assert plan.sources == {own: "http"} and plan.skipped["dead"] == 1 and plan.n_own == 1


def test_a_local_file_is_scraped_without_the_network(tmp_path):
    f = tmp_path / "mine.txt"
    # public addresses – the parser drops reserved ranges like 203.0.113.0/24
    f.write_text("socks5://user:pw@45.67.89.10:1080\n45.67.89.11:8080\n45.67.89.12:3128\nnot a proxy\n")
    url, ptype = srcs.parse_source_spec(str(f))
    quality = srcs.SourceStats(tmp_path / "stats.json")
    cache = FetchCache(tmp_path / "cache")  # enabled – a local file must still not end up in it
    res = asyncio.run(pipeline.scrape({url: ptype}, ["http", "socks5"], quality, CollectView(1, 0.0), cache))
    assert res.ok_sources == 1
    assert "socks5 user:pw@45.67.89.10:1080" in res.index
    assert {"http 45.67.89.11:8080", "socks5 45.67.89.11:8080"} <= res.index.keys()  # bare: tried as both
    assert not list((tmp_path / "cache").glob("*"))


def test_only_sources_keeps_the_history_to_the_own_lists(tmp_path):
    from proxyscraper.history import ProxyHistory
    history = ProxyHistory(tmp_path / "h.json")
    history.record_ok("http 45.67.89.20:80", 100, "45.67.89.20")   # worked before, not in the list
    history.record_ok("http 45.67.89.11:8080", 100, "45.67.89.11")  # worked before and listed
    res = pipeline.ScrapeResult(["mine"], {"http 45.67.89.10:80": [0], "http 45.67.89.11:8080": [0]})
    quality = srcs.SourceStats(tmp_path / "stats.json")
    assert pipeline.prioritize(res, quality, history, ["http"], listed_only=True) == \
        ["http 45.67.89.11:8080", "http 45.67.89.10:80"]
    assert "http 45.67.89.20:80" in pipeline.prioritize(res, quality, history, ["http"])
