import json
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from hedgebot.models import OrderStatus, Side, Tif
from hedgebot.venues.arcus import ArcusSigner, ArcusVenue, canonical_json, isolated_liquidation_price
from hedgebot.venues.base import HttpClient

PRIV = "11" * 32
ADDR = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"


def verify(signer: ArcusSigner, message: bytes, sig_hex: str) -> None:
    pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(signer.api_key))
    pub.verify(bytes.fromhex(sig_hex), message)  # raises on mismatch


def test_order_payload_matches_documented_layout():
    s = ArcusSigner(PRIV, ADDR, 2)
    payload = s.order_payload(op=1, market_id=28, ct=1713825891591000000, g_ns=4102444800000000000, p=21105, q=100000,
                              reduce_only=False, side=Side.BUY, tif=Tif.ALO, client_id="hbabc")
    assert payload == ('{"ad":"0xabcdef0123456789abcdef0123456789abcdef01","ai":2,"c":"hbabc","ct":1713825891591000000,'
                       '"g":4102444800000000000,"m":28,"op":1,"p":21105,"q":100000,"r":0,"s":0,"t":3,"v":1}')


def test_order_payload_omits_empty_client_id_and_encodes_flags():
    s = ArcusSigner(PRIV, ADDR)
    payload = json.loads(s.order_payload(op=1, market_id=1, ct=5, g_ns=6, p=7, q=8, reduce_only=True,
                                         side=Side.SELL, tif=Tif.IOC, client_id=None))
    assert "c" not in payload
    assert (payload["r"], payload["s"], payload["t"]) == (1, 1, 2)


def test_cancel_payload_and_signature_roundtrip():
    s = ArcusSigner(PRIV, ADDR)
    payload = s.cancel_payload(market_id=3, ct=99, order_id="a1b2")
    assert payload == '{"ad":"0xabcdef0123456789abcdef0123456789abcdef01","ai":0,"ct":99,"id":"a1b2","m":3,"op":2,"v":1}'
    verify(s, payload.encode(), s.sign(payload.encode()))
    with pytest.raises(ValueError):
        s.cancel_payload(market_id=3, ct=99)


def test_legacy_message_is_timestamp_action_sorted_body():
    s = ArcusSigner(PRIV, ADDR)
    msg = s.legacy_message(123, "setLeverage", {"marketId": 1, "address": "0x1", "leverage": 5})
    assert msg == b'123setLeverage{"address":"0x1","leverage":5,"marketId":1}'


def test_signer_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ArcusSigner("abc", ADDR)
    with pytest.raises(ValueError):
        ArcusSigner(PRIV, "not-an-address")


class CaptureHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.limiter = None

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return self.response

    json_or_raise = staticmethod(HttpClient.json_or_raise)


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _venue_with_market(signer):
    from tests.conftest import make_market

    v = ArcusVenue("testnet", signer)
    m = make_market("arcus", "NVDA-USD", step_size=Decimal("0.0000001"))
    m.extra.update({"marketId": 28, "tickSize": "0.01", "tickTiers": [{"upToPrice": "5000", "tick": "0.01"},
                                                                     {"tick": "0.05"}]})
    v._markets = {"NVDA-USD": m}
    return v


def test_place_order_body_agrees_with_signed_payload():
    signer = ArcusSigner(PRIV, ADDR, 1)
    v = _venue_with_market(signer)
    v.http = CaptureHttp(Resp(202, {"orderId": "ff01", "status": "ACK"}))
    state = v.place_order("NVDA-USD", Side.BUY, Decimal("1.2345678"), Decimal("211.05"), Tif.ALO, client_id="hbX")
    assert state.order_id == "ff01" and state.status is OrderStatus.PENDING
    method, path, kw = v.http.calls[0]
    assert (method, path) == ("POST", "/v1/placeOrder")
    assert kw["params"] == {"address": ADDR}
    body = json.loads(kw["data"])
    headers = kw["headers"]
    assert headers["X-API-Key"] == signer.api_key
    assert str(body["timestamp"]) == headers["X-Timestamp"]
    assert body["quantity"] == "1.2345678" and body["price"] == "211.05" and body["timeInForce"] == "ALO"
    # rebuild the typed payload from the body exactly like the gateway does and check the signature
    payload = signer.order_payload(op=1, market_id=28, ct=body["timestamp"], g_ns=int(body["goodTilTime"]) * 1000,
                                   p=21105, q=12345678, reduce_only=False, side=Side.BUY, tif=Tif.ALO, client_id="hbX")
    verify(signer, payload.encode(), headers["X-Signature"])


def test_place_order_rejects_off_tick_price_locally():
    signer = ArcusSigner(PRIV, ADDR)
    v = _venue_with_market(signer)
    v.http = CaptureHttp(Resp(202, {}))
    with pytest.raises(ValueError):
        v.place_order("NVDA-USD", Side.BUY, Decimal("1"), Decimal("211.055"), Tif.ALO)


def test_place_order_4xx_becomes_rejected_state():
    signer = ArcusSigner(PRIV, ADDR)
    v = _venue_with_market(signer)
    v.http = CaptureHttp(Resp(400, {"error": "oracle deviation", "errorType": "OracleDeviation"}))
    state = v.place_order("NVDA-USD", Side.SELL, Decimal("1"), Decimal("211.05"), Tif.ALO)
    assert state.status is OrderStatus.REJECTED and "OracleDeviation" in state.reject_reason


def test_tick_tiers_round_prices():
    v = _venue_with_market(None)
    assert v.round_price("NVDA-USD", Decimal("211.057"), "down") == Decimal("211.05")
    assert v.round_price("NVDA-USD", Decimal("6000.03"), "nearest") == Decimal("6000.05")
    assert v.round_price("NVDA-USD", Decimal("6000.01"), "down") == Decimal("6000")


def test_canonical_json_is_compact_and_sorted():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_isolated_liquidation_price_long_and_short():
    # long 1 @100 with $20 margin, mmf 5%: 20 + (P-100) = 0.05P -> P = 84.21
    assert isolated_liquidation_price(Decimal(1), Decimal(100), Decimal(20), Decimal("0.05")).quantize(Decimal("0.01")) \
        == Decimal("84.21")
    # short 1 @100 with $20 margin: 20 - (P-100) = 0.05P -> P = 114.29
    assert isolated_liquidation_price(Decimal(-1), Decimal(100), Decimal(20), Decimal("0.05")).quantize(Decimal("0.01")) \
        == Decimal("114.29")


def test_position_stop_uses_untriggered_op_and_position_grouping():
    signer = ArcusSigner(PRIV, ADDR)
    v = _venue_with_market(signer)
    v.http = CaptureHttp(Resp(202, {"responses": [{"orderId": "bb01", "status": "ACK", "clientTime": 1,
                                                    "marketId": 28, "marketDisplayName": "NVDA-USD"}]}))
    st = v.place_stop("NVDA-USD", Side.SELL, Decimal("5"), Decimal("180.004"))
    assert st.status is OrderStatus.OPEN and st.order_id == "bb01" and st.trigger_price == Decimal("180.00")
    _, path, kw = v.http.calls[0]
    assert path == "/v1/batchPlaceOrders"
    body = json.loads(kw["data"])
    assert body["grouping"] == "positionTpsl"
    (o,) = body["orders"]
    assert (o["tpslType"], o["orderType"], o["timeInForce"], o["quantity"], o["reduceOnly"]) == \
        ("STOP_LOSS", "MARKET", "IOC", "0", True)
    assert o["stopPrice"] == "180" and o["price"] == "171"  # 5% slippage bound below the trigger
    payload = signer.order_payload(op=4, market_id=28, ct=o["timestamp"], g_ns=int(o["goodTilTime"]) * 1000,
                                   p=17100, q=0, reduce_only=True, side=Side.SELL, tif=Tif.IOC,
                                   client_id=o["clientId"])
    verify(signer, payload.encode(), o["signature"])
    assert kw["headers"]["X-Signature"] == o["signature"]
