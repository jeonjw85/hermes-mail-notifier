import unittest
from string import Formatter

from tests.support import feature


class I18nTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.i18n")

    def test_locale_priority_and_fallback(self):
        resolve = self.module.resolve_ui_language
        self.assertEqual(resolve(explicit="en", saved="ko", detected="ko-KR"), "en")
        self.assertEqual(resolve(saved="ko", detected="en-US"), "ko")
        self.assertEqual(resolve(detected="ko_KR.UTF-8"), "ko")
        self.assertEqual(resolve(detected="en-US"), "en")
        self.assertEqual(resolve(detected="fr-FR"), "en")
        self.assertEqual(resolve(), "en")

    def test_locale_normalization_never_uses_unsupported_selected_language(self):
        for detected, expected in (("KO-kr", "ko"), ("en_US.UTF-8", "en"),
                                   ("ko_KR.UTF-8@modifier", "ko"), ("C", "en"),
                                   ("", "en"), (None, "en"), ("korean", "en")):
            with self.subTest(detected=detected):
                self.assertEqual(self.module.resolve_ui_language(detected=detected), expected)
        self.assertEqual(self.module.resolve_ui_language(explicit="fr", saved="ko"), "en")

    def test_catalogs_have_matching_keys_and_format_placeholders(self):
        catalogs = self.module.MESSAGES
        self.assertEqual(set(catalogs), {"ko", "en"})
        self.assertTrue(catalogs["en"])
        self.assertEqual(set(catalogs["ko"]), set(catalogs["en"]))
        for key in catalogs["en"]:
            with self.subTest(key=key):
                fields = lambda template: {name for _, name, _, _ in Formatter().parse(template) if name is not None}
                self.assertEqual(fields(catalogs["ko"][key]), fields(catalogs["en"][key]))
                values = dict.fromkeys(fields(catalogs["en"][key]), "sample")
                for language in ("ko", "en"):
                    self.assertEqual(self.module.text(key, language, **values), catalogs[language][key].format(**values))

    def test_text_normalizes_locale_and_falls_back_to_english(self):
        self.assertEqual(self.module.text("notification.priority", "ko-KR", value="높음"), "우선순위: 높음")
        self.assertEqual(self.module.text("notification.priority", "fr", value="High"), "Priority: High")
        self.assertEqual(self.module.text("notification.not_specified", "en"), "Not specified")
