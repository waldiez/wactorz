"""Tests for wactorz.core.handles — readable entity handles.

Handles are deterministic labels (``swid:<class>:<ns>:<slug>-<fp>``), not DIDs:
no ``did:swid:`` acceptance, no parsing API. The canonical identifier for acting
entities is a real ``did:swid:z…`` carrying the handle in ``alsoKnownAs``.
"""

import unittest

from wactorz.core.handles import (
    DEFAULT_FINGERPRINT_LEN,
    fingerprint,
    make_handle,
    normalize_segment,
    slugify,
)


class TestMakeHandle(unittest.TestCase):
    def test_deterministic(self):
        a = make_handle("device", "home", "dev-1", name="Thermostat")
        b = make_handle("device", "home", "dev-1", name="Thermostat")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("swid:device:home:thermostat-"))

    def test_display_name_change_does_not_move_fingerprint(self):
        a = make_handle("device", "home", "dev-1", name="Old")
        b = make_handle("device", "home", "dev-1", name="New")
        self.assertEqual(a.rsplit("-", 1)[1], b.rsplit("-", 1)[1])

    def test_namespace_scopes_fingerprint(self):
        a = make_handle("device", "site-a", "shared", name="x")
        b = make_handle("device", "site-b", "shared", name="x")
        self.assertNotEqual(a, b)

    def test_all_known_classes(self):
        for cls in ("space", "device", "agent", "user"):
            h = make_handle(cls, "home", "k", name="n")
            self.assertTrue(h.startswith(f"swid:{cls}:home:n-"))

    def test_unknown_class_raises(self):
        with self.assertRaises(ValueError):
            make_handle("building", "home", "k")

    def test_empty_natural_key_raises(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                make_handle("device", "home", bad)

    def test_nameless_handle_is_bare_fingerprint(self):
        h = make_handle("device", "home", "dev-1")
        local = h.rsplit(":", 1)[1]
        self.assertEqual(local, fingerprint("home", "dev-1"))

    def test_name_slugified_to_nothing_falls_back_to_fingerprint(self):
        self.assertEqual(
            make_handle("device", "home", "dev-1", name="!!!"),
            make_handle("device", "home", "dev-1"),
        )

    def test_fingerprint_len_is_respected(self):
        h = make_handle("device", "home", "dev-1", fingerprint_len=12)
        self.assertEqual(len(h.rsplit(":", 1)[1]), 12)

    def test_fingerprint_len_bounds(self):
        for bad in (5, 33):
            with self.assertRaises(ValueError):
                fingerprint("home", "k", bad)

    def test_default_fingerprint_len(self):
        self.assertEqual(len(fingerprint("home", "k")), DEFAULT_FINGERPRINT_LEN)


class TestSegments(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify(" Kitchen_Light!! "), "kitchen-light")
        self.assertEqual(slugify("Name.v2"), "name-v2")  # no dots in slugs
        self.assertEqual(slugify(" !!! "), "")

    def test_normalize_segment_keeps_dots(self):
        self.assertEqual(normalize_segment("Name.v2"), "name.v2")
        self.assertEqual(normalize_segment("A__  B---C"), "a-b-c")
        self.assertEqual(normalize_segment(" !!! "), "")


if __name__ == "__main__":
    unittest.main()
