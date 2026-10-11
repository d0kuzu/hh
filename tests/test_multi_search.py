from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from hh_auto_apply.config import Config
from hh_auto_apply.parser_applier import HHBrowser
from hh_auto_apply.selectors import load_selectors


def test_query_list_deduplicates_and_url_keeps_filters():
    config = Config(queries=(" бармен ", "бариста", "БАРМЕН"), areas=("159",))
    assert config.search_queries == ("бармен", "бариста")
    params = parse_qs(urlparse(config.search_url(2, query="бариста")).query)
    assert params["text"] == ["бариста"]
    assert params["area"] == ["159"]
    assert params["search_field"] == ["name"]
    assert params["page"] == ["2"]
    assert Config(query="бармен-кассир").search_queries == ("бармен-кассир",)


def test_env_query_list_overrides_legacy_query(tmp_path, monkeypatch):
    monkeypatch.setenv("HH_QUERIES", "бармен,бариста")
    monkeypatch.setenv("HH_QUERY", "старый запрос")
    assert Config.from_env(tmp_path / "absent.env").search_queries == ("бармен", "бариста")
    monkeypatch.setenv("HH_QUERIES", "")
    assert Config.from_env(tmp_path / "absent.env").search_queries == ("старый запрос",)


def fake_search(monkeypatch, pages, *, max_pages=10):
    driver, events = Mock(), Mock()
    selectors = load_selectors()
    visited = []
    current = {}
    def navigate(driver, url, *_):
        params = parse_qs(urlparse(url).query)
        key = (params["text"][0], int(params["page"][0]))
        visited.append(key)
        current["ids"], current["next"] = pages[key]
        driver.current_url = url
    def visible(root, selector):
        if root is not driver:
            return []
        if selector == selectors["next"]:
            return [Mock()] if current["next"] else []
        if selector == selectors["empty"]:
            return [Mock()] if not current["ids"] else []
        if selector != selectors["cards"]:
            return []
        cards = []
        for vid in current["ids"]:
            link, card = Mock(), Mock()
            link.get_attribute.return_value = f"https://hh.kz/vacancy/{vid}"
            link.text = "Бармен-бариста"
            card.find_elements.return_value = [link]
            card.text = "Бармен-бариста Откликнуться"
            cards.append(card)
        return cards
    monkeypatch.setattr("hh_auto_apply.parser_applier.navigate", navigate)
    monkeypatch.setattr("hh_auto_apply.parser_applier.visible", visible)
    browser = HHBrowser(driver, Config(queries=("бармен", "бариста"), max_pages=max_pages,
                                      pause_min=0, pause_max=0), selectors, events)
    browser.until = lambda predicate: predicate(driver)
    return browser, visited


def test_overlapping_first_page_does_not_stop_second_query(monkeypatch):
    browser, visited = fake_search(monkeypatch, {
        ("бармен", 0): (["1", "2"], False),
        ("бариста", 0): (["1", "2"], True),
        ("бариста", 1): (["2", "3"], False),
    })
    assert [v.id for v, _ in browser.discover()] == ["1", "2", "3"]
    assert visited == [("бармен", 0), ("бариста", 0), ("бариста", 1)]
    assert "Уникальных вакансий: 3" in browser.end_reason


def test_empty_first_query_continues_to_second(monkeypatch):
    browser, visited = fake_search(monkeypatch, {
        ("бармен", 0): ([], False), ("бариста", 0): (["3"], False),
    })
    assert [v.id for v, _ in browser.discover()] == ["3"]
    assert len(visited) == 2


def test_page_cap_is_per_query_and_does_not_skip_second_query(monkeypatch):
    browser, visited = fake_search(monkeypatch, {
        ("бармен", 0): (["1"], True), ("бариста", 0): (["2"], False),
    }, max_pages=1)
    assert [v.id for v, _ in browser.discover()] == ["1", "2"]
    assert len(visited) == 2 and "MAX_PAGES=1" in browser.end_reason
