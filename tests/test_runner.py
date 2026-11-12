from app.db import get_db
from app.services.runner import run_job


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def test_run_job_dedup_duplicate_title(app, client):
    login(client)
    client.post(
        "/jobs",
        data={"name": "t1", "seed_path": "/mock/catalog/alpha"},
        follow_redirects=True,
    )
    with app.app_context():
        job_id = get_db().execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]
        summary = run_job(job_id)
    assert summary["status"] == "done"
    assert summary["inserted"] == 2
    assert summary["skipped_dup"] == 1
    assert summary["total_source"] == 3


def test_mock_catalog_http(client):
    r = client.get("/mock/catalog/alpha")
    assert r.status_code == 200
    assert "市政管线测绘记录表" in r.get_data(as_text=True)
