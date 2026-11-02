"""Extraction layer (retries, backoff, caching, validation) and the CLI in --dry-run mode,
against a local fake server that mimics Frankfurter, Nager.Date and BrasilAPI."""
import json
import shutil
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import pytest

from pipeline import run as run_mod
from pipeline.extract.api_client import ApiClient, ExtractionError
from pipeline.extract.apis import fetch_fx, fetch_holidays, fetch_states
from pipeline.extract.files import KAGGLE_FILES, read_kaggle, snapshot_raw
from tests.synth import ROOT, make_kaggle

UFS = [("AC", "Acre", "Norte"), ("AL", "Alagoas", "Nordeste"), ("AP", "Amapá", "Norte"), ("AM", "Amazonas", "Norte"),
       ("BA", "Bahia", "Nordeste"), ("CE", "Ceará", "Nordeste"), ("DF", "Distrito Federal", "Centro-Oeste"),
       ("ES", "Espírito Santo", "Sudeste"), ("GO", "Goiás", "Centro-Oeste"), ("MA", "Maranhão", "Nordeste"),
       ("MT", "Mato Grosso", "Centro-Oeste"), ("MS", "Mato Grosso do Sul", "Centro-Oeste"),
       ("MG", "Minas Gerais", "Sudeste"), ("PA", "Pará", "Norte"), ("PB", "Paraíba", "Nordeste"),
       ("PR", "Paraná", "Sul"), ("PE", "Pernambuco", "Nordeste"), ("PI", "Piauí", "Nordeste"),
       ("RJ", "Rio de Janeiro", "Sudeste"), ("RN", "Rio Grande do Norte", "Nordeste"), ("RS", "Rio Grande do Sul", "Sul"),
       ("RO", "Rondônia", "Norte"), ("RR", "Roraima", "Norte"), ("SC", "Santa Catarina", "Sul"),
       ("SP", "São Paulo", "Sudeste"), ("SE", "Sergipe", "Nordeste"), ("TO", "Tocantins", "Norte")]


class FakeApis(BaseHTTPRequestHandler):
    hits: list = []
    fail_first = 0           # number of initial 500s on the FX endpoint
    always_status = None     # force a status code on every call
    aggregated = False       # simulate a coarse (weekly) FX series

    def log_message(self, *a):  # silence
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).hits.append(self.path)
        path = urlparse(self.path).path
        if type(self).always_status:
            return self._send(type(self).always_status, {"error": "forced"})
        if path.startswith("/fx/"):
            if type(self).fail_first > 0:
                type(self).fail_first -= 1
                return self._send(500, {"error": "boom"})
            a, b = path[len("/fx/"):].split("..")
            d, end, rates = date.fromisoformat(a), date.fromisoformat(b), {}
            step = 7 if type(self).aggregated else 1
            while d <= end:
                if type(self).aggregated or d.weekday() < 5:
                    rates[d.isoformat()] = {"USD": 0.30 + (d - date(2018, 1, 1)).days * 0.0001}
                d += timedelta(days=step)
            return self._send(200, {"amount": 1.0, "base": "BRL", "start_date": a, "end_date": b, "rates": rates})
        if path.startswith("/holidays/PublicHolidays/"):
            year = int(path.split("/")[3])
            return self._send(200, [
                {"date": f"{year}-02-13", "localName": "Test National", "name": "Test National", "countryCode": "BR",
                 "global": True, "counties": None, "types": ["Public"]},
                {"date": f"{year}-02-14", "localName": "Test Regional RS", "name": "Test Regional RS",
                 "countryCode": "BR", "global": False, "counties": ["BR-RS"], "types": ["Public"]}])
        if path == "/states":
            return self._send(200, [{"id": i, "sigla": s, "nome": n, "regiao": {"id": 1, "sigla": "X", "nome": r}}
                                    for i, (s, n, r) in enumerate(UFS)])
        return self._send(404, {"error": "nope"})


@pytest.fixture()
def server():
    FakeApis.hits, FakeApis.fail_first, FakeApis.always_status, FakeApis.aggregated = [], 0, None, False
    httpd = HTTPServer(("127.0.0.1", 0), FakeApis)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


# ------------------------------------------------------------------------------ ApiClient
def test_retries_with_exponential_backoff_then_succeeds(server, tmp_path):
    FakeApis.fail_first = 2
    sleeps = []
    client = ApiClient(tmp_path, attempts=4, backoff_base=1.0, sleep=sleeps.append)
    text = client.get_text("x", f"{server}/fx/2018-02-05..2018-02-09", {"from": "BRL", "to": "USD"})
    assert json.loads(text)["rates"]
    assert sleeps == [1.0, 2.0]                     # 1s then 2s between the 3 attempts
    assert len(FakeApis.hits) == 3


def test_gives_up_after_all_attempts_with_clear_message(server, tmp_path):
    FakeApis.always_status = 503
    sleeps = []
    client = ApiClient(tmp_path, attempts=3, backoff_base=0.5, sleep=sleeps.append)
    with pytest.raises(ExtractionError) as e:
        client.get_text("x", f"{server}/fx/2018-02-05..2018-02-09")
    assert "failed after 3 attempts" in str(e.value) and "HTTP 503" in str(e.value) and "partial data" in str(e.value)
    assert len(FakeApis.hits) == 3 and sleeps == [0.5, 1.0]
    assert not (tmp_path / "x.json").exists()       # nothing half-written / cached on failure


def test_non_retryable_4xx_fails_fast(server, tmp_path):
    client = ApiClient(tmp_path, attempts=4, sleep=lambda s: None)
    with pytest.raises(ExtractionError) as e:
        client.get_text("x", f"{server}/does-not-exist")
    assert "non-retryable HTTP 404" in str(e.value) and len(FakeApis.hits) == 1


def test_connection_refused_is_an_extraction_error(tmp_path):
    client = ApiClient(tmp_path, attempts=3, timeout=1, sleep=lambda s: None)
    with pytest.raises(ExtractionError) as e:
        client.get_text("x", "http://127.0.0.1:9/nothing")
    msg = str(e.value)
    assert "ConnectionError" in msg or "ConnectTimeout" in msg
    assert "failed after 3 attempts" in msg


def test_response_is_cached_as_received_and_reused(server, tmp_path):
    client = ApiClient(tmp_path, sleep=lambda s: None)
    first = client.get_text("fx1", f"{server}/fx/2018-02-05..2018-02-09")
    assert (tmp_path / "fx1.json").read_text(encoding="utf-8") == first      # raw layer = body as received
    n = len(FakeApis.hits)
    assert client.get_text("fx1", f"{server}/fx/2018-02-05..2018-02-09") == first
    assert len(FakeApis.hits) == n                                            # cache hit, no new request
    ApiClient(tmp_path, refresh=True, sleep=lambda s: None).get_text("fx1", f"{server}/fx/2018-02-05..2018-02-09")
    assert len(FakeApis.hits) == n + 1                                        # --refresh-cache re-calls the API


def test_min_three_attempts_enforced(tmp_path):
    with pytest.raises(ValueError):
        ApiClient(tmp_path, attempts=2)


# ------------------------------------------------------------------------------ API parsers
def test_fx_chunks_long_ranges_and_returns_decimals(server, tmp_path):
    client = ApiClient(tmp_path, sleep=lambda s: None)
    fx = fetch_fx(client, f"{server}/fx", date(2017, 1, 1), date(2017, 12, 31))
    assert len([h for h in FakeApis.hits if h.startswith("/fx/")]) >= 5          # <= 80-day windows
    assert fx["rate_date"].is_monotonic_increasing and fx["rate_date"].is_unique
    assert str(type(fx["brl_usd"].iloc[0])) == "<class 'decimal.Decimal'>"       # never float
    assert fx["rate_date"].iloc[0] <= date(2017, 1, 8) and fx["rate_date"].iloc[-1] >= date(2017, 12, 25)


def test_fx_rejects_aggregated_series(server, tmp_path):
    FakeApis.aggregated = True
    with pytest.raises(ExtractionError):
        fetch_fx(ApiClient(tmp_path, sleep=lambda s: None), f"{server}/fx", date(2018, 1, 1), date(2018, 1, 20))


def test_holidays_and_states_parse(server, tmp_path):
    client = ApiClient(tmp_path, sleep=lambda s: None)
    h = fetch_holidays(client, f"{server}/holidays", [2018])
    assert len(h) == 2 and h.loc[1, "counties"] == ["BR-RS"] and h.loc[0, "counties"] == []
    s = fetch_states(client, f"{server}/states")
    assert len(s) == 27 and s.set_index("state_code").loc["RS", "region"] == "Sul"


def test_states_api_with_too_few_entries_aborts(tmp_path):
    class Bad:
        def get_text(self, *a, **k):
            return json.dumps([{"sigla": "SP", "nome": "Sao Paulo", "regiao": {"nome": "Sudeste"}}])
    with pytest.raises(ExtractionError):
        fetch_states(Bad(), "x")


# ------------------------------------------------------------------------------ files / raw layer
def _write_inputs(tmp_path):
    inp = tmp_path / "kaggle"
    inp.mkdir()
    for key, df in make_kaggle().items():
        df.to_csv(inp / KAGGLE_FILES[key], index=False)
    internal = tmp_path / "internal"
    shutil.copytree(ROOT / "data" / "internal", internal)
    return inp, internal


def test_missing_kaggle_files_reported(tmp_path):
    with pytest.raises(ExtractionError) as e:
        read_kaggle(tmp_path)
    assert "olist_orders_dataset.csv" in str(e.value)


def test_raw_layer_is_byte_identical_copy_with_manifest(tmp_path):
    inp, internal = _write_inputs(tmp_path)
    kaggle = read_kaggle(inp)
    raw = tmp_path / "raw"
    manifest = snapshot_raw(inp, internal, raw, kaggle)
    assert (raw / "kaggle" / "olist_orders_dataset.csv").read_bytes() == (inp / "olist_orders_dataset.csv").read_bytes()
    assert (raw / "internal" / "campaigns.json").exists()
    data = json.loads(manifest.read_text())
    assert data["files"]["kaggle/olist_orders_dataset.csv"]["rows"] == len(kaggle["orders"])


# ------------------------------------------------------------------------------ CLI (dry run)
def _env(monkeypatch, server):
    monkeypatch.setenv("FX_BASE_URL", f"{server}/fx")
    monkeypatch.setenv("HOLIDAYS_BASE_URL", f"{server}/holidays")
    monkeypatch.setenv("STATES_URL", f"{server}/states")
    monkeypatch.setenv("API_BACKOFF_BASE", "0")


def test_cli_dry_run_end_to_end(server, tmp_path, monkeypatch):
    _env(monkeypatch, server)
    inp, internal = _write_inputs(tmp_path)
    dump = tmp_path / "dump"
    code = run_mod.main(["--start-date", "2018-02-01", "--enddate", "2018-03-31",      # '--enddate' alias works
                         "--input-dir", str(inp), "--internal-dir", str(internal), "--raw-dir", str(tmp_path / "raw"),
                         "--dry-run", "--dump-dir", str(dump)])
    assert code == 0
    fo = pd.read_csv(dump / "fact_order.csv")
    assert len(fo) == 11 and fo["order_id"].is_unique
    assert (tmp_path / "raw" / "api").glob("fx_BRL_USD_*.json")
    assert any((tmp_path / "raw" / "api").glob("fx_BRL_USD_*.json"))
    dq = pd.read_csv(dump / "data_quality_issue.csv")
    assert {"R2", "R3", "R5", "R13"} <= set(dq["rule_id"])


def test_cli_fails_clearly_when_an_api_is_down(server, tmp_path, monkeypatch, caplog):
    _env(monkeypatch, server)
    FakeApis.always_status = 503
    inp, internal = _write_inputs(tmp_path)
    dump = tmp_path / "dump"
    code = run_mod.main(["--start-date", "2018-02-01", "--end-date", "2018-03-31", "--input-dir", str(inp),
                         "--internal-dir", str(internal), "--raw-dir", str(tmp_path / "raw"), "--dry-run",
                         "--dump-dir", str(dump)])
    assert code == 2
    assert "EXTRACTION FAILED" in caplog.text and "partial data" in caplog.text
    assert not dump.exists()                          # nothing was produced from partial data


def test_cli_rejects_reversed_range(tmp_path):
    assert run_mod.main(["--start-date", "2018-03-01", "--end-date", "2018-02-01", "--dry-run"]) == 2


def test_scope_dates_are_data_driven():
    fx_start, fx_end, years = run_mod.scope_dates(make_kaggle(), date(2018, 2, 1), date(2018, 3, 31))
    assert fx_start == date(2018, 2, 5) - timedelta(days=14) and fx_end == date(2018, 3, 21)
    assert years == [2018]
