"""HTTP-level tests of the FastAPI app (status codes, validation, pagination limits).

Skipped automatically when FastAPI / SQLAlchemy are not installed.  They run against an in-memory SQLite copy of
the synthetic model, so no MySQL server is needed:   pytest tests/test_api_http.py
"""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient            # noqa: E402
from sqlalchemy import create_engine                  # noqa: E402
from sqlalchemy.pool import StaticPool                # noqa: E402

from api import main as api_main                      # noqa: E402
from api.db import get_conn                           # noqa: E402
from tests.api_fixture import RUN_ID, load_sqlite     # noqa: E402


@pytest.fixture(scope="module")
def client():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    load_sqlite(engine)

    def override():
        with engine.connect() as conn:
            yield conn

    api_main.app.dependency_overrides[get_conn] = override
    original = api_main.get_engine
    api_main.get_engine = lambda: engine
    yield TestClient(api_main.app)
    api_main.get_engine = original
    api_main.app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "up"}


def test_order_found_and_content(client):
    r = client.get("/orders/O2")
    assert r.status_code == 200
    body = r.json()
    assert body["fx"]["rate_date_used"] == "2018-02-09" and body["delivery_business_days"] == 1
    assert {s["seller_id"]: s["sla_result"] for s in body["sellers"]} == {"S1": "seller_late", "S2": "carrier_late"}
    assert len(body["items"]) == 2 and isinstance(body["gross_usd"], float)


def test_order_with_dq_issues_and_campaign(client):
    body = client.get("/orders/O7").json()
    assert any(i["issue_type"] == "payment_mismatch" for i in body["data_quality_issues"])
    assert client.get("/orders/O8").json()["campaigns"] == ["C003"]


def test_order_not_found_and_excluded_are_404(client):
    assert client.get("/orders/NOPE").status_code == 404
    r = client.get("/orders/O3")                                  # canceled -> excluded by R2
    assert r.status_code == 404 and "R2" in r.json()["detail"]


def test_scorecard(client):
    r = client.get("/sellers/S1/scorecard", params={"month": "2018-02"})
    assert r.status_code == 200
    b = r.json()
    assert b["tier"] == "Silver" and b["item_count"] == 2 and b["on_time_pct"] == 50.0 and b["seller_late_count"] == 1
    assert client.get("/sellers/NOPE/scorecard", params={"month": "2018-02"}).status_code == 404
    assert client.get("/sellers/S1/scorecard", params={"month": "2018-13"}).status_code == 422
    assert client.get("/sellers/S1/scorecard", params={"month": "201802"}).status_code == 422
    assert client.get("/sellers/S1/scorecard").status_code == 422


def test_top_sellers(client):
    r = client.get("/sellers/top", params={"month": "201803", "metric": "commission_usd", "limit": 2})
    assert r.status_code == 200 and [x["seller_id"] for x in r.json()["results"]] == ["S3", "S1"]
    r2 = client.get("/sellers/top", params={"month": "201803", "metric": "commission_usd", "limit": 2, "offset": 2})
    assert [x["rank"] for x in r2.json()["results"]] == [3]
    assert client.get("/sellers/top", params={"month": "201803", "metric": "on_time_rate"}).json()["results"] == []
    assert client.get("/sellers/top", params={"month": "2018-03"}).status_code == 422          # wrong format
    assert client.get("/sellers/top", params={"month": "201803", "metric": "bogus"}).status_code == 422
    assert client.get("/sellers/top", params={"month": "201803", "limit": 1000}).status_code == 422   # max limit
    assert client.get("/sellers/top", params={"month": "201803", "limit": 0}).status_code == 422


def test_kpis(client):
    r = client.get("/kpis/monthly", params={"start": "2018-02", "end": "2018-03"})
    assert r.status_code == 200
    b = r.json()
    assert b["page"]["total"] == 2 and b["results"][0]["unique_customers"] == 3
    south = client.get("/kpis/monthly", params={"start": "2018-02", "end": "2018-03", "region": "sul"}).json()
    assert south["region"] == "Sul" and south["results"][0]["orders"] == 1
    assert client.get("/kpis/monthly", params={"start": "2018-03", "end": "2018-02"}).status_code == 422
    assert client.get("/kpis/monthly", params={"start": "2018-02", "end": "2018-03", "region": "Mars"}).status_code == 422
    assert client.get("/kpis/monthly", params={"start": "2018-02"}).status_code == 422


def test_campaign_performance(client):
    r = client.get("/campaigns/C003/performance")
    assert r.status_code == 200
    b = r.json()
    assert b["window_days"] == 7 and b["campaign"]["items"] == 2 and b["baseline"]["items"] == 0
    assert b["baseline_end_exclusive"].startswith("2018-03-05")
    assert client.get("/campaigns/C999/performance").status_code == 404


def test_data_quality_summary(client):
    r = client.get("/data-quality/summary")
    assert r.status_code == 200 and r.json()["run_id"] == RUN_ID and r.json()["total_issues"] > 0
    assert client.get("/data-quality/summary", params={"run_id": RUN_ID, "limit": 1}).json()["page"]["total"] > 1
    assert client.get("/data-quality/summary", params={"run_id": "nope"}).status_code == 404
    assert client.get("/data-quality/summary", params={"limit": 101}).status_code == 422
