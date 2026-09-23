import json

from app import astro_depth_cloud as relay


class StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_okx_quote_uses_local_mapping_identity_and_returns_decimal_amounts(monkeypatch, tmp_path):
    key_file = tmp_path / "keys.json"
    key_file.write_text(json.dumps({"K": "key", "S": "secret", "P": "pass"}))
    monkeypatch.setattr(relay, "OKX_CREDENTIAL_FILE", key_file)

    def fake_get(url, **kwargs):
        assert url.startswith("https://web3.okx.com/api/v6/dex/aggregator/quote?")
        assert set(kwargs["headers"]) == {
            "OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE", "OK-ACCESS-TIMESTAMP",
        }
        return StubResponse({"code": "0", "data": [{
            "fromTokenAmount": "10000000000000000000",
            "toTokenAmount": "2500000000000000000",
            "tradeFee": "0.01",
            "fromToken": {"decimal": "18", "tokenContractAddress": relay.PANCAKE_CHAINS["56"]["quote"]},
            "toToken": {"decimal": "18", "tokenContractAddress": "0x1111111111111111111111111111111111111111"},
            "dexRouterList": [{"dexProtocol": {"dexName": "PancakeSwap V3"}}],
        }]})

    monkeypatch.setattr(relay._client, "get", fake_get)
    result = relay.dex_quote(
        "ABC", "56", "0x1111111111111111111111111111111111111111", 10.0, "okxdex",
    )
    assert result["source"] == "okx_v6_quote_api"
    assert result["fromAmount"] == "10"
    assert result["toAmount"] == "2.5"
    assert result["routeDexes"] == ["PancakeSwap V3"]


def test_pancake_quote_keeps_read_only_source_marker(monkeypatch, tmp_path):
    address = "0x2222222222222222222222222222222222222222"

    def fake_rpc(_url, method, params):
        if method == "eth_gasPrice":
            return hex(1_000_000_000)
        data = params[0]["data"]
        if data == "0x313ce567":
            return hex(18)
        return "0x" + "".join(relay._word(value) for value in (2 * 10**18, 0, 0, 100_000))

    def fake_get(_url, **_kwargs):
        return StubResponse({"price": "600"})

    monkeypatch.setattr(relay, "_rpc_call", fake_rpc)
    monkeypatch.setattr(relay._client, "get", fake_get)
    result = relay.dex_quote("ABC", "56", address, 10.0, "pancakeswapv3")
    assert result["source"] == "pancakeswap_v3_quoter_eth_call"
    assert result["exchange"] == "pancakeswapv3"
    assert result["toAmount"] == "2"
    assert float(result["tradeFeeUsd"]) > 0
