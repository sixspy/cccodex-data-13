"""站内 mock 目录页，模拟可采集列表（无外网依赖）。"""

from html import escape

CATALOGS = {
    "/mock/catalog/alpha": [
        {"sku": "A-001", "title": "市政管线测绘记录表"},
        {"sku": "A-002", "title": "排水井巡查台账"},
        {"sku": "A-003", "title": "排水井巡查台账"},
    ],
    "/mock/catalog/beta": [
        {"sku": "B-101", "title": "泵站运行日报"},
        {"sku": "B-102", "title": "雨污分流抽检单"},
    ],
}


def render_catalog_page(path: str) -> str:
    items = CATALOGS.get(path)
    if not items:
        return _not_found(path)
    rows = "".join(
        f"<li data-sku='{escape(it['sku'])}'>{escape(it['title'])}</li>"
        for it in items
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Mock 目录 {escape(path)}</title></head>
<body>
<h1>采集演练目录</h1>
<p class="seed">{escape(path)}</p>
<ul id="items">{rows}</ul>
</body></html>"""


def _not_found(path: str) -> str:
    return f"<html><body><h1>404</h1><p>{escape(path)}</p></body></html>", 404


def parse_catalog_html(html: str, seed_path: str) -> list[dict]:
    """极简解析：从 &lt;li data-sku&gt; 抽条目（基线够用，后续题可换解析器）。"""
    import re

    pattern = re.compile(
        r"<li\s+data-sku='([^']+)'\s*>([^<]+)</li>",
        re.IGNORECASE,
    )
    out = []
    for sku, title in pattern.findall(html):
        out.append({"sku": sku, "title": title.strip(), "seed_path": seed_path})
    return out
