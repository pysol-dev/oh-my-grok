import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ORIGINAL_SHA256 = "9643ee06f6817b08c57ea585fd8f919d4b23639bffd817a4a3084c7c9d0d30ae"


def test_original_spec_is_byte_identical():
    data = (ROOT / "spec" / "IMPLEMENTATION.md").read_bytes()
    assert hashlib.sha256(data).hexdigest() == ORIGINAL_SHA256
    recorded = (ROOT / "spec" / "IMPLEMENTATION.md.sha256").read_text().split()[0]
    assert recorded == ORIGINAL_SHA256


def test_sibling_spec_records_original_hash_and_every_supersession():
    text = (ROOT / "spec" / "ADAPTIVE.md").read_text()
    assert ORIGINAL_SHA256 in text
    for n in range(1, 10):
        assert f"SUP-{n}" in text
