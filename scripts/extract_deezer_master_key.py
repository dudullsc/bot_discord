#!/usr/bin/env python3
"""Extract Deezer masterDecryptionKey from the public web player bundle.

LavaSrc uses the key as a UTF-8 string (first 16 bytes) for Blowfish decryption.
Run locally and put the output in DEEZER_MASTER_DECRYPTION_KEY (.env / SealedSecret).
"""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.parse
import urllib.request

EXPLORE_URL = "https://www.deezer.com/en/channels/explore/"
LAVASRC_SHA256 = bytes(
    [
        52,
        76,
        41,
        0x8A,
        120,
        0x85,
        48,
        72,
        0xC6,
        74,
        16,
        75,
        82,
        101,
        0xBA,
        0xDF,
        15,
        0xBE,
        111,
        0xDA,
        0xB0,
        71,
        103,
        11,
        0xB5,
        0x88,
        0x9B,
        0xF7,
        66,
        0xCB,
        0xDA,
        0xF0,
    ]
)
LEGACY_MD5 = "7ebf40da848f4a0fb3cc56ddbe6c2d09"


def fetch_master_key() -> str:
    html = urllib.request.urlopen(EXPLORE_URL, timeout=30).read().decode("utf-8", "replace")
    js_match = re.search(r'"(https://[^"]+app-web[^"]+\.js)"', html) or re.search(
        r"(https://[^\"']+app-web[^\"']+\.js)", html
    )
    if not js_match:
        js_match = re.search(r"app-web[^\"']+\.js", html)
    if not js_match:
        raise RuntimeError("Could not find Deezer app-web JS bundle URL")

    js_url = js_match.group(1 if js_match.lastindex else 0)
    if not js_url.startswith("http"):
        js_url = "https://www.deezer.com/" + js_url.lstrip("/")

    js = urllib.request.urlopen(js_url, timeout=30).read().decode("utf-8", "replace")
    part_a = re.search(r"0x61%2C(0x[0-9a-f]{2}%2C){6}0x67", js, re.I)
    part_b = re.search(r"0x31%2C(0x[0-9a-f]{2}%2C){6}0x34", js, re.I)
    if not part_a or not part_b:
        raise RuntimeError("Could not find master key byte arrays in Deezer JS")

    def parse_half(match: re.Match[str]) -> list[int]:
        decoded = urllib.parse.unquote(match.group(0))
        return [int(x, 16) for x in decoded.split(",")]

    a = list(reversed(parse_half(part_a)))
    b = list(reversed(parse_half(part_b)))
    key_bytes = bytes(a[i // 2] if i % 2 == 0 else b[i // 2] for i in range(16))
    return key_bytes.decode("latin-1")


def lavasrc_valid(key: str) -> bool:
    return hashlib.sha256(key.encode("utf-8")).digest() == LAVASRC_SHA256


def main() -> int:
    try:
        key = fetch_master_key()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    md5 = hashlib.md5(key.encode("latin-1")).hexdigest()
    ok_lavasrc = lavasrc_valid(key)
    ok_legacy = md5 == LEGACY_MD5

    print(key)
    print(f"lavasrc_sha256_ok={ok_lavasrc}", file=sys.stderr)
    print(f"legacy_md5_ok={ok_legacy}", file=sys.stderr)
    if not ok_lavasrc:
        print(
            "warning: key does not match LavaSrc 4.8.2+ hash; playback may sound garbled",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
