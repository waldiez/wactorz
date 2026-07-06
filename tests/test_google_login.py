import unittest
from unittest import mock

from wactorz.interfaces import google_login


class GoogleLoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_unconfigured(self):
        cal = mock.Mock()
        cal.config.client_id.return_value = ""
        cal.config.client_secret.return_value = ""
        cal.authorize_direct = mock.AsyncMock()
        gm = mock.Mock()
        gm.config.client_id.return_value = ""
        gm.config.client_secret.return_value = ""
        gm.authorize_direct = mock.AsyncMock()

        with (
            mock.patch.object(google_login, "GoogleCalendarMcpClient", return_value=cal),
            mock.patch.object(google_login, "GmailMcpClient", return_value=gm),
        ):
            rc = await google_login._run("both")

        self.assertEqual(rc, 0)
        cal.authorize_direct.assert_not_awaited()
        gm.authorize_direct.assert_not_awaited()

    async def test_authorizes_selected_service(self):
        cal = mock.Mock()
        cal.config.client_id.return_value = "cid"
        cal.config.client_secret.return_value = "secret"
        cal.authorize_direct = mock.AsyncMock(return_value="Calendar: authorized (direct REST).")
        gm = mock.Mock()
        gm.authorize_direct = mock.AsyncMock()

        with (
            mock.patch.object(google_login, "GoogleCalendarMcpClient", return_value=cal),
            mock.patch.object(google_login, "GmailMcpClient", return_value=gm),
        ):
            rc = await google_login._run("calendar")

        self.assertEqual(rc, 0)
        cal.authorize_direct.assert_awaited_once()
        gm.authorize_direct.assert_not_awaited()  # gmail not selected

    async def test_returns_nonzero_on_failure(self):
        cal = mock.Mock()
        cal.config.client_id.return_value = "cid"
        cal.config.client_secret.return_value = "secret"
        cal.authorize_direct = mock.AsyncMock(return_value="Calendar: authorization was cancelled")

        with mock.patch.object(google_login, "GoogleCalendarMcpClient", return_value=cal):
            rc = await google_login._run("calendar")

        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
