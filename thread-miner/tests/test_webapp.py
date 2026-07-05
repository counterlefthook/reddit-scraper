"""Web-layer tests: auth, upload validation, job lifecycle (worker disabled)."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
CSV = b"url,tag\nhttps://www.reddit.com/r/python/comments/1abc01/title/,test\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    shutil.copy(REPO / "config.yaml", cfg_file)  # relative paths resolve to tmp
    monkeypatch.setenv("TM_CONFIG", str(cfg_file))
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("TM_INSECURE_COOKIE", "1")  # TestClient speaks plain http
    monkeypatch.setenv("APP_PASSWORD", "team-pw")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    from webapp.main import create_app
    with TestClient(create_app()) as c:
        yield c


def login(client, name="Chris"):
    resp = client.post("/login", data={"name": name, "password": "team-pw"},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def upload(client, filename="urls.csv", content=CSV):
    return client.post(
        "/jobs", files={"file": (filename, content, "text/csv")},
        data={"tag_col": "tag"}, follow_redirects=False,
    )


def test_pages_require_login(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/jobs/abc", follow_redirects=False).status_code == 303
    assert client.get("/api/jobs/abc/progress").status_code == 401
    assert client.get("/jobs/abc/report", follow_redirects=False).status_code == 303
    assert client.get("/healthz").status_code == 200  # health stays open


def test_wrong_password_rejected(client):
    resp = client.post("/login", data={"name": "Chris", "password": "nope"})
    assert resp.status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_then_dashboard(client):
    login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "New research run" in resp.text


def test_upload_rejects_wrong_type(client):
    login(client)
    resp = upload(client, filename="notes.txt")
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.text


def test_upload_creates_queued_job(client):
    login(client)
    resp = upload(client)
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[-1]

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200

    progress = client.get(f"/api/jobs/{job_id}/progress").json()
    assert progress["status"] == "queued"
    assert progress["report_ready"] is False

    # report endpoints refuse until the job is done
    assert client.get(f"/jobs/{job_id}/report").status_code == 404
    assert client.get(f"/jobs/{job_id}/report.html").status_code == 404

    # run history shows it, attributed to the logged-in user
    dash = client.get("/").text
    assert "urls.csv" in dash and "Chris" in dash


def test_second_upload_also_queues(client):
    login(client)
    first = upload(client)
    second = upload(client, filename="more.csv")
    assert first.status_code == 303 and second.status_code == 303
    ids = {r.headers["location"] for r in (first, second)}
    assert len(ids) == 2


def test_unknown_job_progress_404(client):
    login(client)
    assert client.get("/api/jobs/nope/progress").status_code == 404
