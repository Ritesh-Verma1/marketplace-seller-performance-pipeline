"""Small synthetic Olist-style dataset with hand-computed expectations (see test_pipeline_e2e.py).

Scenario (range used by the tests: 2018-02-01 .. 2018-03-31)
  S1 (SP) had 6,000.00 of delivered sales in Jan-2018 (outside the range)  -> Silver in Feb (R12)
  Holidays (synthetic): 2018-02-13 national, 2018-02-14 regional (RS only), 2018-03-01 'Optional' (ignored)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from pipeline.transform.build import Internal, Reference
from pipeline.transform.common import DQLog
from pipeline.transform import internal_files as inf

ROOT = Path(__file__).resolve().parents[1]


def load_internal(dq: DQLog | None = None) -> Internal:
    dq = dq if dq is not None else DQLog()
    d = ROOT / "data" / "internal"
    return Internal(
        rates=inf.load_commission_rates(d / "commission_rates.csv", dq),
        overrides=inf.load_category_overrides(d / "category_commission_overrides.csv", dq),
        patch=inf.load_translation_patch(d / "category_translation_patch.csv", dq),
        campaigns=inf.load_campaigns(d / "campaigns.json", dq),
    )


def make_reference() -> Reference:
    fx_points = {"2018-01-10": "0.30", "2018-02-05": "0.31", "2018-02-09": "0.32", "2018-02-15": "0.33",
                 "2018-02-18": "0.335", "2018-02-20": "0.34", "2018-03-06": "0.35", "2018-03-09": "0.355",
                 "2018-03-12": "0.36", "2018-03-20": "0.365"}
    fx = pd.DataFrame({"rate_date": [date.fromisoformat(k) for k in fx_points],
                       "brl_usd": [Decimal(v) for v in fx_points.values()]})
    hol = pd.DataFrame([
        {"date": date(2018, 2, 13), "local_name": "Test National", "name": "Test National",
         "global_flag": True, "counties": [], "types": ["Public"]},
        {"date": date(2018, 2, 14), "local_name": "Test Regional RS", "name": "Test Regional RS",
         "global_flag": False, "counties": ["BR-RS"], "types": ["Public"]},
        {"date": date(2018, 3, 1), "local_name": "Test Optional", "name": "Test Optional",
         "global_flag": True, "counties": [], "types": ["Optional"]},
    ])
    states = pd.DataFrame([("SP", "Sao Paulo", "Sudeste"), ("RJ", "Rio de Janeiro", "Sudeste"),
                           ("MG", "Minas Gerais", "Sudeste"), ("RS", "Rio Grande do Sul", "Sul"),
                           ("BA", "Bahia", "Nordeste")], columns=["state_code", "state_name", "region"])
    return Reference(fx=fx, holidays=hol, states=states)


def _df(cols, rows):
    return pd.DataFrame(rows, columns=cols, dtype=str)


def make_kaggle() -> dict[str, pd.DataFrame]:
    sellers = _df(["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"], [
        ("S1", "1001", "sao paulo", "SP"), ("S2", "20000", "rio", "RJ"), ("S3", "90000", "porto alegre", "RS")])

    customers = _df(["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city",
                     "customer_state"], [
        ("c1", "U1", "1310", "sao paulo", "SP"), ("c2", "U2", "90010", "poa", "RS"),
        ("c3", "U1", "20010", "rio", "RJ"),   # same person, later order, other state
        ("c4", "U3", "30100", "bh", "MG"), ("c5", "U4", "1310", "sao paulo", "SP"),
        ("c6", "U5", "99999", "nowhere", "BA"),
        ("c7", "U6", "1310", "sao paulo", "SP"), ("c8", "U7", "1310", "sao paulo", "SP")])

    products = _df(["product_id", "product_category_name"], [
        ("P1", "telefonia"), ("P2", "pc_gamer"), ("P3", "casa_conforto_2"), ("P4", "xyz_sem_traducao"),
        ("P5", None), ("P6", "informatica_acessorios"), ("P7", "papelaria"), ("P8", "perfumaria")])
    translation = _df(["product_category_name", "product_category_name_english"], [
        ("telefonia", "telephony"), ("casa_conforto_2", "home_comfort_2"),
        ("informatica_acessorios", "computers_accessories"), ("papelaria", "stationery"),
        ("perfumaria", "perfumery")])

    def order(oid, cid, status, purchase, approved, carrier, delivered, estimated):
        return (oid, cid, status, purchase, approved, carrier, delivered, estimated)

    orders = _df(["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
                  "order_delivered_carrier_date", "order_delivered_customer_date",
                  "order_estimated_delivery_date"], [
        order("OJ1", "c1", "delivered", "2018-01-10 10:00:00", "2018-01-10 10:30:00", "2018-01-11 10:00:00",
              "2018-01-15 10:00:00", "2018-01-20 00:00:00"),
        order("O1", "c1", "delivered", "2018-02-05 10:00:00", "2018-02-05 10:30:00", "2018-02-06 10:00:00",
              "2018-02-09 15:00:00", "2018-02-12 00:00:00"),
        order("O2", "c2", "delivered", "2018-02-09 20:00:00", None, "2018-02-12 10:00:00",
              "2018-02-14 09:00:00", "2018-02-13 00:00:00"),
        order("O3", "c1", "canceled", "2018-02-10 09:00:00", None, None, None, "2018-02-20 00:00:00"),
        order("O4", "c1", "unavailable", "2018-02-11 09:00:00", None, None, None, "2018-02-20 00:00:00"),
        order("O5", "c2", "shipped", "2018-02-15 09:00:00", "2018-02-15 10:00:00", "2018-02-16 10:00:00",
              None, "2018-02-25 00:00:00"),
        order("O6", "c1", "delivered", "2018-02-18 09:00:00", "2018-02-18 10:00:00", "2018-02-19 10:00:00",
              None, "2018-02-27 00:00:00"),
        order("O7", "c5", "delivered", "2018-02-20 09:00:00", "2018-02-20 10:00:00", "2018-02-21 10:00:00",
              "2018-02-24 10:00:00", "2018-02-28 00:00:00"),
        order("O8", "c4", "delivered", "2018-03-06 12:00:00", "2018-03-06 12:30:00", "2018-03-07 10:00:00",
              "2018-03-10 10:00:00", "2018-03-15 00:00:00"),
        order("O9", "c1", "delivered", "2018-03-12 00:00:10", "2018-03-12 01:00:00", "2018-03-13 10:00:00",
              "2018-03-15 10:00:00", "2018-03-20 00:00:00"),
        order("O10", "c5", "delivered", "2018-03-10 09:00:00", "2018-03-10 10:00:00", "2018-03-12 10:00:00",
              "2018-03-14 10:00:00", "2018-03-20 00:00:00"),
        order("O11", "c3", "delivered", "2018-03-20 09:00:00", "2018-03-20 10:00:00", "2018-03-21 10:00:00",
              "2018-03-25 10:00:00", "2018-03-28 00:00:00"),
        order("O12", "c6", "delivered", "2018-03-21 09:00:00", "2018-03-21 10:00:00", "2018-03-22 10:00:00",
              "2018-03-26 10:00:00", "2018-03-30 00:00:00"),
        order("O13", "c8", "delivered", "2018-03-11 23:59:30", "2018-03-12 00:30:00", "2018-03-13 10:00:00",
              "2018-03-15 10:00:00", "2018-03-20 00:00:00"),
    ])

    def item(oid, n, pid, sid, limit, price, freight):
        return (oid, str(n), pid, sid, limit, price, freight)

    items = _df(["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price",
                 "freight_value"], [
        item("OJ1", 1, "P7", "S1", "2018-01-13 00:00:00", "6000.00", "10.00"),
        item("O1", 1, "P7", "S1", "2018-02-07 00:00:00", "100.00", "20.00"),
        item("O2", 1, "P1", "S1", "2018-02-10 00:00:00", "50.00", "5.00"),
        item("O2", 2, "P6", "S2", "2018-02-20 00:00:00", "200.00", "30.00"),
        item("O3", 1, "P1", "S1", "2018-02-12 00:00:00", "100.00", "10.00"),
        item("O4", 1, "P1", "S1", "2018-02-12 00:00:00", "100.00", "10.00"),
        item("O5", 1, "P4", "S2", "2018-02-17 00:00:00", "40.00", "5.00"),
        item("O6", 1, "P8", "S3", "2018-02-20 00:00:00", "80.00", "10.00"),
        item("O7", 1, "P6", "S3", "2018-02-22 00:00:00", "80.00", "20.00"),
        item("O8", 1, "P1", "S1", "2018-03-08 00:00:00", "300.00", "15.00"),
        item("O9", 1, "P1", "S1", "2018-03-14 00:00:00", "100.00", "10.00"),
        item("O10", 1, "P2", "S3", "2018-03-13 00:00:00", "1000.00", "0.00"),
        item("O11", 1, "P3", "S2", "2018-03-22 00:00:00", "60.00", "10.00"),
        item("O11", 2, "P5", "S2", "2018-03-22 00:00:00", "25.00", "5.00"),
        item("O12", 1, "P2", "S3", "2018-03-23 00:00:00", "10.00", "0.00"),
        item("O13", 1, "P1", "S1", "2018-03-14 00:00:00", "70.00", "10.00"),
    ])

    def pay(oid, kind, value, seq=1):
        return (oid, str(seq), kind, "1", value)

    payments = _df(["order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"], [
        pay("OJ1", "credit_card", "6010.00"), pay("O1", "credit_card", "120.00"),
        pay("O2", "credit_card", "200.00"), pay("O2", "voucher", "85.00", 2),
        pay("O5", "boleto", "45.00"), pay("O6", "credit_card", "90.00"),
        pay("O7", "credit_card", "90.00"),                       # gross is 100.00 -> mismatch (diff 10.00)
        pay("O8", "credit_card", "315.00"), pay("O9", "credit_card", "110.00"),
        pay("O10", "credit_card", "1000.00"), pay("O11", "credit_card", "100.00"),
        pay("O12", "credit_card", "10.00"), pay("O13", "credit_card", "80.50"),   # diff 0.50 -> tolerated
    ])

    reviews = _df(["review_id", "order_id", "review_score", "review_creation_date", "review_answer_timestamp"], [
        ("r1", "O7", "1", "2018-02-24 00:00:00", "2018-02-24 10:00:00"),
        ("r2", "O7", "5", "2018-02-25 00:00:00", "2018-02-26 09:00:00"),   # latest answer -> kept
        ("r3", "O1", "4", "2018-02-10 00:00:00", "2018-02-11 09:00:00"),
        ("r4", "O2", "2", "2018-02-15 00:00:00", "2018-02-16 09:00:00"),
    ])

    geolocation = _df(["geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng"], [
        ("1001", "-23.55", "-46.63"), ("1001", "-23.57", "-46.65"), ("1001", "-23.59", "-46.61"),
        ("1001", "0.0", "0.0"), ("1001", "40.0", "-3.7"),                  # outside Brazil -> removed (R15)
        ("1310", "-23.56", "-46.66"), ("20000", "-22.90", "-43.20"), ("20010", "-22.91", "-43.18"),
        ("90000", "-30.03", "-51.22"), ("90010", "-30.04", "-51.23"), ("30100", "-19.92", "-43.94"),
    ])

    return {"orders": orders, "order_items": items, "payments": payments, "reviews": reviews,
            "customers": customers, "sellers": sellers, "products": products, "geolocation": geolocation,
            "translation": translation}
