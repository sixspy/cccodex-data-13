from app.services.dedup import normalize_item_key


def test_normalize_item_key_collapses_title_space():
    a = normalize_item_key("/mock/catalog/alpha", "排水井巡查台账")
    b = normalize_item_key("/mock/catalog/alpha", "  排水井巡查  台账  ")
    assert a == b
