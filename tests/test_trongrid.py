"""Parser tested against the real TronGrid response shape (captured live)."""
from swap.clients.trongrid import parse_transfers


# A real /v1/accounts/{addr}/transactions/trc20 body (trimmed to the fields used).
SAMPLE = {
    "data": [
        {
            "transaction_id": "7991f17817709ba76a98abee80972548ce7b9f4e81328c9890b6fa21d8c60ce7",
            "token_info": {"symbol": "USDT", "address": "TR7N...", "decimals": 6, "name": "Tether USD"},
            "block_timestamp": 1781419224000,
            "from": "TDqSquXBgUCLYvYC4XZgrprLK589dkhSCf",
            "to": "TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G",
            "type": "Transfer",
            "value": "25082458451770",
        }
    ],
    "success": True,
    "meta": {"fingerprint": "abc", "page_size": 1},
}


def test_parse_scales_by_decimals():
    [t] = parse_transfers(SAMPLE)
    assert t.txid.startswith("7991f178")
    assert t.from_address == "TDqSquXBgUCLYvYC4XZgrprLK589dkhSCf"
    assert t.amount_usdt == 25082458.45177       # 25082458451770 / 1e6
    assert t.block_timestamp == 1781419224000


def test_parse_empty_and_malformed_are_safe():
    assert parse_transfers({"data": []}) == []
    assert parse_transfers({}) == []
    # missing value → row skipped, not a crash
    bad = {"data": [{"transaction_id": "x", "from": "a", "to": "b", "token_info": {"decimals": 6}}]}
    assert parse_transfers(bad) == []
