"""IMAP modified UTF-7 codec (RFC 3501 §5.1.3) for folder names.

Handles non-ASCII names like German umlauts (`Entwürfe` → `Entw&APw-rfe`) that
IMAP mandates when talking to the wire.
"""
from __future__ import annotations

import base64


def encode(s: str) -> str:
    result: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        o = ord(c)
        if 0x20 <= o <= 0x7E:
            result.append("&-" if c == "&" else c)
            i += 1
            continue
        j = i
        while j < n and not (0x20 <= ord(s[j]) <= 0x7E):
            j += 1
        chunk = s[i:j].encode("utf-16be")
        enc = base64.b64encode(chunk).rstrip(b"=").decode("ascii").replace("/", ",")
        result.append("&" + enc + "-")
        i = j
    return "".join(result)


def decode(s: str) -> str:
    result: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "&":
            result.append(c)
            i += 1
            continue
        end = s.find("-", i + 1)
        if end == -1:
            result.append(s[i:])
            break
        enc = s[i + 1 : end]
        if enc == "":
            result.append("&")
        else:
            b64 = enc.replace(",", "/")
            b64 += "=" * ((-len(b64)) % 4)
            data = base64.b64decode(b64)
            result.append(data.decode("utf-16be"))
        i = end + 1
    return "".join(result)
