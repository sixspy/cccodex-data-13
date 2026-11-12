from flask import Blueprint, Response

from app.services.mock_catalog import render_catalog_page

bp = Blueprint("mock", __name__)


@bp.get("/mock/catalog/<catalog_id>")
def catalog(catalog_id: str):
    path = f"/mock/catalog/{catalog_id}"
    html = render_catalog_page(path)
    if isinstance(html, tuple):
        body, code = html
        return Response(body, status=code, mimetype="text/html; charset=utf-8")
    return Response(html, mimetype="text/html; charset=utf-8")
