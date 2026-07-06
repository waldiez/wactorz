"""Unit tests for the dashboard MQTT proxy's credential injection
(wactorz.monitor_server._inject_connect_credentials).

The proxy rewrites the browser's anonymous MQTT CONNECT in-flight so an
authenticated broker accepts it. These tests build real CONNECT packets,
inject, and re-parse the result to confirm the rewrite is spec-correct for
MQTT 3.1.1 and 5.0 — and that non-CONNECT / already-credentialed / malformed
packets are passed through untouched.
"""

import unittest

from wactorz.monitor_server import _inject_connect_credentials


def _remlen(n: int) -> bytes:
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n:
            d |= 0x80
        out.append(d)
        if not n:
            break
    return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return bytes((len(b) >> 8, len(b) & 0xFF)) + b


def _build_connect(client_id="c", proto_level=4, username=None, password=None) -> bytes:
    flags = 0x02  # clean session
    if username is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    vh = _str("MQTT") + bytes((proto_level, flags)) + b"\x00\x3c"  # keepalive 60
    if proto_level == 5:
        vh += b"\x00"  # empty CONNECT properties
    payload = _str(client_id)
    if username is not None:
        payload += _str(username)
    if password is not None:
        payload += _str(password)
    body = vh + payload
    return b"\x10" + _remlen(len(body)) + body


def _parse_connect(pkt: bytes) -> dict:
    assert pkt[0] == 0x10, "not a CONNECT"
    i, rem, mult = 1, 0, 1
    while True:
        b = pkt[i]
        rem += (b & 0x7F) * mult
        i += 1
        if not b & 0x80:
            break
        mult *= 128
    body = pkt[i : i + rem]
    assert len(body) == rem, "remaining length does not match body"
    pn = (body[0] << 8) | body[1]
    pos = 2 + pn
    level = body[pos]
    pos += 1
    flags = body[pos]
    pos += 1
    pos += 2  # keep-alive
    if level == 5:
        plen, mult = 0, 1
        while True:
            b = body[pos]
            plen += (b & 0x7F) * mult
            pos += 1
            if not b & 0x80:
                break
            mult *= 128
        pos += plen

    def rd(p):
        ln = (body[p] << 8) | body[p + 1]
        return body[p + 2 : p + 2 + ln].decode("utf-8"), p + 2 + ln

    cid, pos = rd(pos)
    user = pwd = None
    if flags & 0x80:
        user, pos = rd(pos)
    if flags & 0x40:
        pwd, pos = rd(pos)
    assert pos == len(body), "trailing bytes after payload"
    return {"flags": flags, "client_id": cid, "username": user, "password": pwd}


class InjectConnectCredentialsTest(unittest.TestCase):
    def test_anonymous_v311_gets_credentials(self):
        pkt = _build_connect(client_id="dashboard", proto_level=4)
        out = _inject_connect_credentials(pkt, "alice", "s3cret")
        parsed = _parse_connect(out)
        self.assertEqual(parsed["username"], "alice")
        self.assertEqual(parsed["password"], "s3cret")
        self.assertTrue(parsed["flags"] & 0x80)  # username flag
        self.assertTrue(parsed["flags"] & 0x40)  # password flag
        self.assertEqual(parsed["client_id"], "dashboard")  # untouched

    def test_anonymous_v5_gets_credentials(self):
        pkt = _build_connect(client_id="dash5", proto_level=5)
        out = _inject_connect_credentials(pkt, "bob", "pw")
        parsed = _parse_connect(out)
        self.assertEqual(parsed["username"], "bob")
        self.assertEqual(parsed["password"], "pw")
        self.assertEqual(parsed["client_id"], "dash5")

    def test_remaining_length_recomputed_across_127_boundary(self):
        # A client id chosen so the base packet's Remaining Length is a single
        # byte (< 128), and injecting credentials forces a 2-byte varint.
        pkt = _build_connect(client_id="x" * 108, proto_level=4)  # body = 120
        self.assertEqual(pkt[1] & 0x80, 0)  # base remaining length is one byte
        out = _inject_connect_credentials(pkt, "user", "password")
        self.assertTrue(out[1] & 0x80)  # injected packet needs a 2-byte varint
        parsed = _parse_connect(out)  # asserts remaining length matches body
        self.assertEqual(parsed["username"], "user")
        self.assertEqual(parsed["client_id"], "x" * 108)

    def test_already_credentialed_is_unchanged(self):
        pkt = _build_connect(client_id="c", username="existing", password="pw")
        out = _inject_connect_credentials(pkt, "alice", "s3cret")
        self.assertEqual(out, pkt)

    def test_non_connect_packet_unchanged(self):
        pingreq = b"\xc0\x00"
        self.assertEqual(_inject_connect_credentials(pingreq, "a", "b"), pingreq)

    def test_truncated_packet_unchanged(self):
        # Claims a remaining length of 5 but carries no body.
        truncated = b"\x10\x05"
        self.assertEqual(_inject_connect_credentials(truncated, "a", "b"), truncated)

    def test_empty_buffer_unchanged(self):
        self.assertEqual(_inject_connect_credentials(b"", "a", "b"), b"")


if __name__ == "__main__":
    unittest.main()
