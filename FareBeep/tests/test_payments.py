"""THE SETTLEMENT ENGINE - price math, Paystack link generation, webhook
signature verification (all from payments.py)."""
import hashlib
import hmac

import httpx
import pytest

from FareBeep import payments


# ---------------------------------------------------------------------------
# calculate_final_price - the brief's formula
# ---------------------------------------------------------------------------
def test_mission_formula_exact():
    """(Net_Fare + 5000 + 100) / (1 - 0.015), with the default ARHA markup."""
    p = payments.calculate_final_price(50000.0)
    assert p["net_fare"] == 50000.0
    assert p["markup"] == 5000.0
    assert p["base_amount"] == 55000.0
    assert p["total_amount"] == pytest.approx((50000 + 5000 + 100) / 0.985)
    assert p["processing_fee"] == pytest.approx(p["total_amount"] - 55000.0)


def test_mission_formula_with_explicit_params():
    p = payments.calculate_final_price(90000.0, markup=3000.0,
                                       flat_fee=100.0, fee_rate=0.015)
    assert p["base_amount"] == 93000.0
    assert p["total_amount"] == pytest.approx((90000 + 3000 + 100) / 0.985)


def test_fee_cap_never_overcharges_big_tickets():
    """Paystack caps local fees at NGN 2,000 - above that the customer pays
    base + cap exactly and the net is unchanged."""
    p = payments.calculate_final_price(159000.0)
    assert p["base_amount"] == 164000.0
    assert p["processing_fee"] == 2000.0
    assert p["total_amount"] == 166000.0
    assert p["total_amount"] - p["processing_fee"] == p["base_amount"]


def test_net_to_utility_never_below_base():
    for price in (20000.0, 50000.0, 90000.0, 118500.0, 157500.0, 200000.0):
        p = payments.calculate_final_price(price)
        assert p["total_amount"] - p["processing_fee"] >= p["base_amount"] - 0.01


# ---------------------------------------------------------------------------
# initialize_paystack_payment - Test Mode link
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_initialize_creates_test_link_with_kobo_amount(monkeypatch):
    captured = {}

    class _Client:
        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return _FakeResp({"status": True, "data": {
                "access_code": "AC_X", "authorization_url":
                    "https://checkout.paystack.com/abc"}})

    out = payments.initialize_paystack_payment(
        "FB-TEST123", 91472.08, "user@farebeep.ng",
        secret_key="sk_test_abc", callback_url="https://fb.ng/cb",
        http_client=_Client())

    assert out["access_code"] == "AC_X"
    assert captured["json"]["reference"] == "FB-TEST123"
    assert captured["json"]["amount"] == 9147208      # NGN -> kobo
    assert captured["json"]["currency"] == "NGN"
    assert captured["json"]["callback_url"] == "https://fb.ng/cb"
    assert captured["headers"]["Authorization"] == "Bearer sk_test_abc"


def test_initialize_requires_secret_key(monkeypatch):
    monkeypatch.setattr(payments, "PAYSTACK_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="PAYSTACK_SECRET_KEY"):
        payments.initialize_paystack_payment(
            "FB-TEST123", 1000.0, "u@f.ng", secret_key=None)


# ---------------------------------------------------------------------------
# verify_paystack_signature - X-Paystack-Signature (HMAC-SHA512, raw body)
# ---------------------------------------------------------------------------
def test_signature_verifies_and_rejects():
    body = b'{"event":"charge.success","data":{"reference":"FB-ABC"}}'
    key = "sk_test_secret"
    good = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()

    assert payments.verify_paystack_signature(body, good, secret_key=key)
    assert not payments.verify_paystack_signature(b"tampered", good,
                                                  secret_key=key)
    assert not payments.verify_paystack_signature(body, "deadbeef",
                                                  secret_key=key)


def test_signature_fails_closed_without_key():
    assert not payments.verify_paystack_signature(b"{}", "abc",
                                                  secret_key="")
