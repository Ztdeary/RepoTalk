"""缓存 key:同一问题归一化后必须命中同一 key,不同仓库必须隔离。"""

from app.core.cache import _cache_key, _normalize


def test_normalize_collapses_space_and_case():
    assert _normalize("  PreparedRequest   在 哪 ") == "preparedrequest 在 哪"


def test_cache_key_is_stable_for_equivalent_questions():
    assert _cache_key(1, "requests", " Session  在哪 ") == _cache_key(1, "requests", "session 在哪")


def test_cache_key_namespaces_by_repo():
    assert _cache_key(1, "requests", "Session 在哪") != _cache_key(2, "requests", "Session 在哪")
    assert _cache_key(None, "requests", "Session 在哪").startswith("retrieve:coll:requests:")


def test_cache_key_uses_repo_namespace_when_repo_id_present():
    assert _cache_key(7, "requests", "q").startswith("retrieve:repo:7:")
