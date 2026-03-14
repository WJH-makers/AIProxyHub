import json
import unittest

from helpers import isolated_launcher_fs


class TestResponsesModelAliasNormalization(unittest.TestCase):
    def _sanitize(self, launcher, obj: dict) -> dict:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        out = launcher._sanitize_responses_body_bytes(raw)
        return json.loads(out.decode("utf-8", errors="replace"))

    def test_codex_high_alias_normalized_to_base_model_and_effort_high(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt5.2-codex-high",
                    "input": "hi",
                    "stream": False,
                },
            )
            self.assertEqual(out.get("model"), "gpt-5.2-codex")
            self.assertEqual((out.get("reasoning") or {}).get("effort"), "high")

    def test_xhigh_alias_normalized_to_base_model_and_effort_xhigh(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt-5.2-xhigh",
                    "input": "hi",
                    "stream": False,
                },
            )
            self.assertEqual(out.get("model"), "gpt-5.2")
            self.assertEqual((out.get("reasoning") or {}).get("effort"), "xhigh")

    def test_missing_dash_gpt5_prefix_is_normalized(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt5.2",
                    "input": "hi",
                    "stream": False,
                },
            )
            self.assertEqual(out.get("model"), "gpt-5.2")
            # 仅修正前缀时不应强行写入 reasoning
            self.assertFalse("reasoning" in out)

    def test_fixed_effort_overrides_conflicting_effort(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt-5.2-codex-high",
                    "input": "hi",
                    "stream": False,
                    "reasoning": {"effort": "xhigh"},
                },
            )
            self.assertEqual(out.get("model"), "gpt-5.2-codex")
            self.assertEqual((out.get("reasoning") or {}).get("effort"), "high")

    def test_codex_low_verbosity_is_normalized_to_medium(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt-5.2-codex",
                    "input": "hi",
                    "stream": False,
                    "text": {"verbosity": "low"},
                },
            )
            self.assertEqual((out.get("text") or {}).get("verbosity"), "medium")

    def test_non_codex_low_verbosity_is_not_changed(self):
        with isolated_launcher_fs() as (launcher, _td):
            out = self._sanitize(
                launcher,
                {
                    "model": "gpt-5.2",
                    "input": "hi",
                    "stream": False,
                    "text": {"verbosity": "low"},
                },
            )
            self.assertEqual((out.get("text") or {}).get("verbosity"), "low")
