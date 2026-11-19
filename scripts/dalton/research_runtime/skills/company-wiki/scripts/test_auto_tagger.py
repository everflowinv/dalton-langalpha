#!/usr/bin/env python3
"""Unit tests for auto_tagger (no network, no agy). Run: venv/bin/python scripts/test_auto_tagger.py"""
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_tagger as at  # noqa: E402

SECTORS = [{"id": "global-semiconductor", "label": "Global Semiconductor"},
           {"id": "us-datacenter-infra", "label": "US Data Center Infra"}]
ALIASES = {"us semiconductor": "global-semiconductor", "美光": "MU"}
CONFIG = {"tagger": {"enabled": True, "provider": "agy", "model": "google/gemini-4-hydrogen",
                     "rate_limit_rpm": 6000},
          "aliases": ALIASES, "sectors": SECTORS}
GOOD = '{"tags": ["数据中心", "数据中心"], "related_companies": ["MU"], "related_sectors": ["us-semiconductor"]}'


def _stream(agent="wikitagger", steps=("user_input", "agent_response"), status="SUCCESS", response=GOOD):
    lines = [{"event": "init", "init": {"agent": agent, "tools": ["run_command"]}}]
    lines += [{"event": "step_update", "step_update": {"step_type": t}} for t in steps]
    lines.append({"event": "result", "result": {"status": status, "response": response}})
    return "\n".join(json.dumps(x) for x in lines)


class FakeGenaiClient:
    class models:
        @staticmethod
        def generate_content(model, contents, config=None):
            raise NotImplementedError


class NormalizeTest(unittest.TestCase):
    def test_maps_alias_drops_unknown_dedupes(self):
        r = at._normalize({"tags": ["a", "a", " b "], "related_companies": ["MU", "MU"],
                           "related_sectors": ["us-semiconductor", "us-made-up", "global-semiconductor"]},
                          SECTORS, ALIASES)
        self.assertEqual(r, {"tags": ["a", "b"], "related_companies": ["MU"],
                             "related_sectors": ["global-semiconductor"]})

    def test_keeps_filing_sector_not_in_config(self):
        r = at._normalize({"tags": [], "related_companies": [], "related_sectors": ["us-agriculture"]},
                          SECTORS, ALIASES, {"category_type": "sector", "category_name": "us-agriculture"})
        self.assertEqual(r["related_sectors"], ["us-agriculture"])

    def test_cleans_items_that_could_break_frontmatter(self):
        r = at._normalize({"tags": ["a]\nsource_url: http://evil", "x,y", "长" * 60, "\x00"],
                           "related_companies": ["MU]"], "related_sectors": []}, SECTORS, ALIASES)
        self.assertEqual(r["tags"][:2], ["a source_url： http://evil", "x y"])
        self.assertEqual(len(r["tags"][2]), at.MAX_ITEM_CHARS)
        self.assertEqual(len(r["tags"]), 3)
        self.assertEqual(r["related_companies"], ["MU"])
        fm = at.build_frontmatter(r)
        self.assertEqual(fm.count("\n"), 4)
        self.assertEqual(at.parse_frontmatter(fm + "body")["related_companies"], ["MU"])

    def test_items_stay_plain_yaml_strings(self):
        cases = {"a: b # c": "a： b c", "{x: 1}": "x： 1", "*alias": "alias", "!tag": "tag", "?q": "q",
                 "|p": "p", "@m": "m", "&n": "n", "- d": "d", "k:": "k：", "'s'": "s", '"t"': "t",
                 "a:b": "a:b", "AT&T": "AT&T", "S&P 500": "S&P 500", "100%": "100%", "-5%": "-5%",
                 "Q3：业绩": "Q3：业绩", "x" * 39 + ":y": "x" * 39 + "："}
        self.assertEqual(at._clean_items(list(cases)), list(cases.values()))
        r = at._normalize({"tags": [], "related_companies": list(cases), "related_sectors": []}, SECTORS, ALIASES)
        self.assertEqual(r["related_companies"], list(cases.values()))
        fm = at.build_frontmatter(r)
        self.assertEqual(at.parse_frontmatter(fm + "body")["related_companies"], r["related_companies"])
        try:
            import yaml
        except ImportError:
            return  # the venv has no PyYAML; the parse_frontmatter round-trip above still runs
        self.assertEqual(yaml.safe_load(fm.strip().strip("-"))["related_companies"], r["related_companies"])

    def test_caps_tags_at_8(self):
        r = at._normalize({"tags": [str(i) for i in range(12)], "related_companies": [], "related_sectors": []},
                          SECTORS, ALIASES)
        self.assertEqual(len(r["tags"]), 8)


class PromptTest(unittest.TestCase):
    def test_no_example_tags_and_chinese_rule(self):
        p = at._build_prompt("正文", ALIASES, SECTORS)
        for t in ("跨境电商", "AI变现", "用户增长", "财报分析"):
            self.assertNotIn(t, p)
        self.assertIn("Written in Chinese", p)
        self.assertNotIn("Filing context", p)

    def test_context_block(self):
        p = at._build_prompt("x", ALIASES, SECTORS, {"category_type": "sector",
                                                     "category_name": "us-datacenter-infra",
                                                     "content_type": "专家访谈"})
        self.assertIn("sector `us-datacenter-infra` (US Data Center Infra)", p)
        self.assertIn("Content type: 专家访谈", p)
        p = at._build_prompt("x", ALIASES, SECTORS, {"category_type": "company", "category_name": "SPCX"})
        self.assertIn("company `SPCX`", p)

    def test_sibling_sector_convention(self):
        ctx = {"category_type": "company", "category_name": "TIGR", "sibling_docs": 9,
               "sibling_related_counts": {"us-datacenter-infra": 9, "global-semiconductor": 5,
                                          "FUTU": 7, "us-made-up": 3}}
        p = at._build_prompt("x", ALIASES, SECTORS, ctx)
        self.assertIn("The 9 other documents filed under `TIGR` carry these sectors: "
                      "us-datacenter-infra (9), global-semiconductor (5).", p)
        self.assertNotIn("us-made-up", p)


class TaggerContextTest(unittest.TestCase):
    def test_counts_company_siblings_excluding_self(self):
        import sqlite3
        import company_wiki_cli as cli
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, category_type TEXT, category_name TEXT, related TEXT)")
        conn.executemany("INSERT INTO documents VALUES (?,?,?,?)", [
            (1, "company", "TIGR", '["TIGR", "china-internet", "china-internet"]'),
            (2, "company", "TIGR", '["us-financials", "china-internet"]'),
            (3, "company", "TIGR", "not json"),
            (4, "company", "FUTU", '["china-internet"]'),
            (5, "sector", "TIGR", '["china-internet"]')])
        ctx = cli.tagger_context("company", "TIGR", "纪要", conn=conn, exclude_doc_id=2)
        self.assertEqual(ctx["sibling_docs"], 2)
        self.assertEqual(ctx["sibling_related_counts"], {"TIGR": 1, "china-internet": 1})
        ctx = cli.tagger_context("company", "TIGR", "纪要", conn=conn)
        self.assertEqual(ctx["sibling_related_counts"]["china-internet"], 2)
        self.assertEqual(cli.tagger_context("sector", "x", "y", conn=conn),
                         {"category_type": "sector", "category_name": "x", "content_type": "y"})


class ParseResponseTest(unittest.TestCase):
    def test_non_object_json_is_empty(self):
        for raw in ("[1, 2]", "42", '"x"', "null"):
            self.assertEqual(at._parse_response(raw),
                             {"tags": [], "related_companies": [], "related_sectors": []})


class FirstJsonTest(unittest.TestCase):
    def test_repeated_object(self):
        self.assertEqual(json.loads(at._first_json_object('{"a": 1}\n{"a": 1}')), {"a": 1})

    def test_no_object(self):
        self.assertEqual(at._first_json_object("no json"), "no json")


class CallAgyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cwd = root / "cwd"
        agent_src = root / "agent.md"
        agent_src.write_text("---\nname: wikitagger\ntools: []\nexcludeDefaultComponents: true\n---\n# x\n",
                             encoding="utf-8")
        fake_bin = root / "agy"
        fake_bin.write_text("", encoding="utf-8")
        self.quarantine = root / "quarantine"
        self.patches = [mock.patch.object(at, "AGY_CWD", self.cwd),
                        mock.patch.object(at, "AGY_QUARANTINE", self.quarantine),
                        mock.patch.object(at, "AGY_AGENT_SRC", agent_src),
                        mock.patch.object(at, "AGY_AGENT_FILE", root / "agents/wikitagger.md"),
                        mock.patch.object(at, "AGY_BIN", fake_bin)]
        for p in self.patches:
            p.start()
        self.popen = mock.create_autospec(subprocess.Popen)
        self.proc = self.popen.return_value
        self.proc.pid = 4242
        self.patches.append(mock.patch.object(at.subprocess, "Popen", self.popen))
        self.patches[-1].start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _set(self, stdout, rc=0):
        self.proc.communicate.return_value = (stdout, "")
        self.proc.returncode = rc

    def _call_cwd(self):
        return Path(self.popen.call_args.kwargs["cwd"])

    def test_success_and_env_scrubbed(self):
        self._set(_stream())
        with mock.patch.dict(at.os.environ, {"GEMINI_API_KEY": "secret", "HOME": "/h",
                                             "HTTPS_PROXY": "http://evil:1"}):
            self.assertEqual(at._call_agy("p", "gemini-4-hydrogen-low", 60, 7897), GOOD)
        self.assertEqual(self._call_cwd().parent, self.cwd.resolve())
        self.assertFalse(self._call_cwd().exists())  # per-call dir removed when empty
        args, kwargs = self.popen.call_args
        self.assertIn(f"CALL_DIR={self._call_cwd()}", args[0])
        cmd = args[0]
        self.assertEqual(cmd[0], "/usr/bin/sandbox-exec")
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "wikitagger")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("GEMINI_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(at.AGY_AGENT_FILE.read_text(encoding="utf-8"), at.AGY_AGENT_SRC.read_text(encoding="utf-8"))

    def test_timeout_kills_process_group(self):
        self.proc.communicate.side_effect = [subprocess.TimeoutExpired("agy", 1), ("", "")]
        with mock.patch.object(at.os, "killpg", autospec=True) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                at._call_agy("p", "m", 60, 7897)
        killpg.assert_called_once_with(4242, at.signal.SIGKILL)
        self.assertEqual(self.proc.communicate.call_args.kwargs, {"timeout": 30})
        self.assertFalse(self._call_cwd().exists())

    def test_timeout_survives_exit_race_and_stuck_pipes(self):
        self.proc.communicate.side_effect = [subprocess.TimeoutExpired("agy", 1),
                                             subprocess.TimeoutExpired("agy", 30),
                                             subprocess.TimeoutExpired("agy", 30)]
        with mock.patch.object(at.os, "killpg", autospec=True, side_effect=ProcessLookupError) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired) as cm:
                at._call_agy("p", "m", 60, 7897)
        self.assertEqual(cm.exception.timeout, 1)  # the original timeout, not the cleanup one
        self.assertEqual(killpg.call_count, 2)

    def test_rejects_wrong_agent(self):
        self._set(_stream(agent="default"))
        with self.assertRaisesRegex(RuntimeError, "agent not applied"):
            at._call_agy("p", "m", 60, 7897)

    def test_rejects_tool_step(self):
        self._set(_stream(steps=("user_input", "run_command", "agent_response")))
        with self.assertRaisesRegex(RuntimeError, "unexpected lines/steps/events \\['run_command'\\]"):
            at._call_agy("p", "m", 60, 7897)

    def test_rejects_step_without_type_and_unknown_event(self):
        self._set(_stream() + "\n" + json.dumps({"event": "step_update", "step_update": {}}))
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            at._call_agy("p", "m", 60, 7897)
        self._set(_stream() + "\n" + json.dumps({"event": "tool_call", "name": "run_command"}))
        with self.assertRaisesRegex(RuntimeError, "event:tool_call"):
            at._call_agy("p", "m", 60, 7897)

    def _write_in_cwd_then(self, stdout, exc=None):
        def communicate(*args, **kwargs):
            (self._call_cwd() / "written.txt").write_text("x")
            if exc:
                raise exc
            return stdout, ""
        self.proc.communicate.side_effect = communicate
        self.proc.returncode = 0

    def test_rejects_leftover_files_and_quarantines_them(self):
        self._write_in_cwd_then(_stream())
        with self.assertRaisesRegex(RuntimeError, "left files"):
            at._call_agy("p", "m", 60, 7897)
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual([f.name for f in self.quarantine.glob("*/*")], ["written.txt"])
        self.proc.communicate.side_effect = None
        self._set(_stream())
        self.assertEqual(at._call_agy("p", "m", 60, 7897), GOOD)

    def test_quarantines_leftovers_on_failure_paths_too(self):
        self._write_in_cwd_then(_stream(agent="default"))
        with self.assertRaisesRegex(RuntimeError, "left files"):
            at._call_agy("p", "m", 60, 7897)
        self._write_in_cwd_then("", exc=OSError("argv too long"))
        with self.assertRaises(OSError):
            at._call_agy("p", "m", 60, 7897)
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual(len(list(self.quarantine.glob("*/written.txt"))), 2)

    def test_rejects_failed_status(self):
        self._set(_stream(status="ERROR"), rc=1)
        with self.assertRaisesRegex(RuntimeError, "agy failed"):
            at._call_agy("p", "m", 60, 7897)

    def test_rejects_nonzero_exit_even_on_success_status(self):
        self._set(_stream(), rc=3)
        with self.assertRaisesRegex(RuntimeError, "rc=3"):
            at._call_agy("p", "m", 60, 7897)

    def test_rejects_repeated_or_misordered_init_and_result(self):
        init = json.dumps({"event": "init", "init": {"agent": "wikitagger"}})
        result = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "{}"}})
        step = json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response"}})
        for stdout in (_stream() + "\n" + result, init + "\n" + _stream(), _stream() + "\n" + step,
                       json.dumps({"event": "init", "init": "x"}) + "\n" + _stream()):
            self._set(stdout)
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                at._call_agy("p", "m", 60, 7897)

    def test_sweep_failure_fails_closed_without_masking(self):
        self._set(_stream())
        with mock.patch.object(at.Path, "rmdir", autospec=True, side_effect=OSError("busy")):
            with self.assertRaisesRegex(RuntimeError, "cwd check failed: busy"):
                at._call_agy("p", "m", 60, 7897)
        self.proc.communicate.side_effect = subprocess.TimeoutExpired("agy", 1)
        with mock.patch.object(at.Path, "rmdir", autospec=True, side_effect=OSError("busy")), \
                mock.patch.object(at.os, "killpg", autospec=True):
            with self.assertRaises(subprocess.TimeoutExpired):
                at._call_agy("p", "m", 60, 7897)

    def test_rejects_non_json_and_non_object_lines(self):
        for extra in ("Running tool...", "[1, 2]", '"x"'):
            self._set(_stream() + "\n" + extra)
            with self.assertRaisesRegex(RuntimeError, "unexpected lines"):
                at._call_agy("p", "m", 60, 7897)

    def test_agent_src_must_be_toolless(self):
        for body in ("---\nname: wikitagger\nexcludeDefaultComponents: true\n---\n",
                     "---\nname: wikitagger\ntools: []\n---\n",
                     "name: wikitagger\ntools: []\nexcludeDefaultComponents: true\n",
                     "---\nname: wikitagger\n---\ntools: []\nexcludeDefaultComponents: true\n",
                     "---\nname: wikitagger\ntools: []\nexcludeDefaultComponents: true\ntools: [run_command]\n---\n",
                     "---\nname: other\ntools: []\nexcludeDefaultComponents: true\n---\n",
                     "---\nname: wikitagger\ntools: []\n  - run_command\nexcludeDefaultComponents: true\n---\n",
                     '---\nname: wikitagger\ntools: []\n"tools": [run_command]\nexcludeDefaultComponents: true\n---\n',
                     "---\nname: wikitagger\ntools:[]\nexcludeDefaultComponents: true\n---\n",
                     "---\nname: wikitagger\ntools: []\nexcludeDefaultComponents: true\ninheritMcp: true\n---\n"):
            at.AGY_AGENT_SRC.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "frontmatter must declare"):
                at._call_agy("p", "m", 60, 7897)
        self.popen.assert_not_called()

    def test_shipped_agent_file_passes(self):
        at._check_agent_frontmatter((Path(at.__file__).parent / "agy_wikitagger_agent.md").read_text(encoding="utf-8"))


class AutoTagTest(unittest.TestCase):
    def _client(self, text):
        client = mock.create_autospec(FakeGenaiClient, instance=True)
        client.models.generate_content.return_value = types.SimpleNamespace(text=text)
        return client

    def test_agy_success_skips_api(self):
        get_client = mock.create_autospec(lambda: None)
        with mock.patch.object(at, "_call_agy", autospec=True, return_value=GOOD + "\n" + GOOD):
            r = at.auto_tag("doc", CONFIG, get_client_fn=get_client)
        get_client.assert_not_called()
        self.assertEqual(r, {"tags": ["数据中心"], "related_companies": ["MU"],
                             "related_sectors": ["global-semiconductor"]})

    def test_agy_failure_falls_back_to_api(self):
        client = self._client('{"tags": ["API"], "related_companies": [], "related_sectors": ["bogus"]}')
        with mock.patch.object(at, "_call_agy", autospec=True, side_effect=RuntimeError("boom")):
            r = at.auto_tag("doc", CONFIG, get_client_fn=lambda: client)
        client.models.generate_content.assert_called_once()
        self.assertEqual(client.models.generate_content.call_args.kwargs["model"], "gemini-4-hydrogen")
        self.assertEqual(r, {"tags": ["API"], "related_companies": [], "related_sectors": []})

    def test_agy_empty_reply_falls_back(self):
        client = self._client('{"tags": ["API"], "related_companies": [], "related_sectors": []}')
        with mock.patch.object(at, "_call_agy", autospec=True, return_value="sorry"):
            r = at.auto_tag("doc", CONFIG, get_client_fn=lambda: client)
        self.assertEqual(r["tags"], ["API"])

    def test_agy_only_invalid_sectors_falls_back(self):
        client = self._client('{"tags": ["API"], "related_companies": [], "related_sectors": []}')
        bad = '{"tags": [], "related_companies": [], "related_sectors": ["us-made-up"]}'
        with mock.patch.object(at, "_call_agy", autospec=True, return_value=bad):
            r = at.auto_tag("doc", CONFIG, get_client_fn=lambda: client)
        self.assertEqual(r["tags"], ["API"])

    def test_api_provider_never_calls_agy(self):
        cfg = dict(CONFIG, tagger=dict(CONFIG["tagger"], provider="gemini-api"))
        client = self._client('{"tags": ["API"], "related_companies": [], "related_sectors": []}')
        with mock.patch.object(at, "_call_agy", autospec=True) as call:
            at.auto_tag("doc", cfg, get_client_fn=lambda: client)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
