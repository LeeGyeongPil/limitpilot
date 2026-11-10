"""AC-7 (i18n): en/ko message tables match and every key the code uses exists."""

import ast
import os
import string
import unittest
from unittest import mock

from limitpilot import i18n
from tests.support import SRC


def _placeholders(text: str) -> set:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def _used_keys() -> set:
    """Every literal key passed to t(...) / h(...) / i18n.t(...) in the package sources."""
    keys = set()
    for base, _dirs, files in os.walk(os.path.join(SRC, "limitpilot")):
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                f = node.func
                called = (f.id if isinstance(f, ast.Name)
                          else f.attr if isinstance(f, ast.Attribute)
                          and isinstance(f.value, ast.Name) and f.value.id == "i18n" else None)
                if called not in ("t", "h"):
                    continue
                arg = node.args[0]
                candidates = [arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else [arg]
                for c in candidates:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        keys.add(c.value)
    return keys


class MessageTableTests(unittest.TestCase):
    def test_both_languages_have_the_same_keys(self):
        self.assertEqual(set(i18n.MESSAGES["en"]), set(i18n.MESSAGES["ko"]))

    def test_placeholders_match_between_languages(self):
        for key, en in i18n.MESSAGES["en"].items():
            self.assertEqual(_placeholders(en), _placeholders(i18n.MESSAGES["ko"][key]), key)

    def test_no_empty_messages(self):
        for lang, table in i18n.MESSAGES.items():
            for key, text in table.items():
                self.assertTrue(text.strip(), f"{lang}:{key}")

    def test_every_key_used_by_the_code_exists(self):
        used = _used_keys()
        self.assertGreater(len(used), 80)
        missing = sorted(k for k in used if k not in i18n.MESSAGES["en"])
        self.assertEqual(missing, [])


class LanguageChoiceTests(unittest.TestCase):
    def test_env_override_then_locale_then_english(self):
        with mock.patch.dict(os.environ, {"LIMITPILOT_LANG": "en"}):
            self.assertEqual(i18n.default_lang(), "en")
        with mock.patch.dict(os.environ, {"LIMITPILOT_LANG": "", "LC_ALL": "ko_KR.UTF-8"}):
            self.assertEqual(i18n.default_lang(), "ko")
        with mock.patch.dict(os.environ, {"LIMITPILOT_LANG": "", "LC_ALL": "de_DE.UTF-8"}):
            self.assertEqual(i18n.default_lang(), "en")

    def test_set_lang_switches_output(self):
        saved = i18n._override
        try:
            i18n.set_lang("en")
            self.assertEqual(i18n.t("on"), "on")
            i18n.set_lang("ko")
            self.assertEqual(i18n.t("on"), "켜짐")
        finally:
            i18n._override = saved
            os.environ["LIMITPILOT_LANG"] = "ko"

    def test_decision_reasons_follow_the_language(self):
        from limitpilot import state
        from limitpilot.policy import decide
        from tests.support import usage

        st = state.default_state()
        st["activeSlot"] = 1
        st["slots"] = {"1": {"enabled": True}, "2": {"enabled": True}}
        saved = i18n._override
        try:
            i18n._override = "en"
            reason = decide.decide(st, {1: usage(95, 10), 2: usage(5, 5)})[2]
            self.assertIn("threshold reached", reason)
            i18n._override = "ko"
            reason = decide.decide(st, {1: usage(95, 10), 2: usage(5, 5)})[2]
            self.assertIn("선제 전환", reason)
        finally:
            i18n._override = saved


if __name__ == "__main__":
    unittest.main()
