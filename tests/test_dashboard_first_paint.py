"""Dashboard first paint: the server stamps the configured language into the page."""

import http.client
import os
import re
import threading
import unittest
from unittest import mock

from limitpilot import state
from limitpilot.dashboard import server
from tests.support import temp_home

TOKEN = "first-paint-token-value"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


class FirstPaintLanguageTests(unittest.TestCase):
    def setUp(self):
        # sandbox proxies must not intercept 127.0.0.1; NO_PROXY is irrelevant once they are gone
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in PROXY_VARS:
            os.environ.pop(var, None)
        home = temp_home()
        home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        for var in ("LC_ALL", "LC_MESSAGES"):
            os.environ.pop(var, None)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.api_token = TOKEN
        self.httpd.shutdown_nonce = "n" * 32
        self.httpd.instance_id = "i" * 24
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def page(self) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        return body

    def assert_lang(self, html: str, lang: str) -> None:
        self.assertIn(f'<html lang="{lang}">', html)
        self.assertIn(f'const SERVER_LANG = "{lang}";', html)
        self.assertNotIn("__DASHBOARD_", html)

    def test_english_env_gives_english_page(self):
        os.environ["LIMITPILOT_LANG"] = "en"
        os.environ["LANG"] = "ko_KR.UTF-8"
        self.assert_lang(self.page(), "en")

    def test_korean_env_gives_korean_page(self):
        os.environ["LIMITPILOT_LANG"] = "ko"
        os.environ["LANG"] = "en_US.UTF-8"
        self.assert_lang(self.page(), "ko")

    def test_locale_used_without_override(self):
        os.environ.pop("LIMITPILOT_LANG", None)
        os.environ["LANG"] = "ko_KR.UTF-8"
        self.assert_lang(self.page(), "ko")
        os.environ["LANG"] = "en_US.UTF-8"
        self.assert_lang(self.page(), "en")

    def test_saved_setting_beats_environment(self):
        st = state.load_state()
        state.set_setting(st, "language", "en")
        state.save_state(st)
        os.environ["LIMITPILOT_LANG"] = "ko"
        os.environ["LANG"] = "ko_KR.UTF-8"
        self.assert_lang(self.page(), "en")

    def test_static_markup_is_english_and_boot_applies_korean_labels(self):
        os.environ["LIMITPILOT_LANG"] = "en"
        html = self.page()
        markup = html.split("<body", 1)[1].split("<script", 1)[0]
        markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)  # Korean code comments are fine
        self.assertEqual(re.findall(r"[가-힣]+", markup), [], "static markup must default to English")
        script = html.split("<script>", 2)[-1]
        # every static label id and aria-label/title is re-applied through L() at boot
        boot = script.split('document.documentElement.lang = LANG;', 2)[-1]
        for needle in ('$("thrLbl")', '$("wthrLbl")', '$("updated")', '$("thr").setAttribute("aria-label"',
                       '$("wthr").setAttribute("aria-label"', '$("wthrFollow")', '$("autoLbl")',
                       '$("kaLbl")', '$("prioLbl")', '$("burnLbl")', '$("refreshAllBtn").title',
                       '$("refreshAllBtn").setAttribute("aria-label"'):
            self.assertIn(needle, boot)
        self.assertIn("applyTheme(themeMode())", script)  # theme button aria-label/title via L()

    def test_server_lang_is_whitelisted(self):
        with mock.patch.object(state, "get_settings", return_value={"language": '"><script>x'}):
            self.assertEqual(server._page_lang(), "en")


if __name__ == "__main__":
    unittest.main()
