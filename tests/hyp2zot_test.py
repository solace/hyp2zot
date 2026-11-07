"""Offline tests for hyp2zot using mocks (no network, no real accounts)."""
import os
import sys
import unittest
from typing import cast
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hyp2zot  # noqa: E402

CFG = hyp2zot.Config("123", "zkey", "acct:me@hypothes.is", "hkey")


def ann(i, exact="quoted text", text="my comment", refs=None, tags=None):
    a = {"id": i, "text": text, "tags": tags or ["t1"], "created": "2026-11-01T10:00:00+00:00",
         "target": [{"selector": [{"type": "TextQuoteSelector", "exact": exact, "prefix": "a", "suffix": "b"}]}]}
    if refs:
        a["references"] = refs
    return a


class FakeZot:
    def __init__(self, items, children):
        self._items, self._children = items, children
        self.created = []

    def top(self, **kw): return self._items
    def everything(self, x): return x
    def children(self, key): return self._children.get(key, [])
    def item_template(self, t): return {"itemType": t, "note": "", "tags": []}

    def create_items(self, items, parent):
        self.created.append((parent, items))
        return {"successful": {str(n): {} for n in range(len(items))}, "failed": {}}


def child_note(annotation_id):
    """A child note exactly as the script would have created it."""
    return {"data": {"itemType": "note", "note": hyp2zot.build_note(ann(annotation_id)), "tags": []}}


def items(*specs):
    return [{"key": k, "data": {"title": f"T{k}", "url": u}} for k, u in specs]


class Tests(unittest.TestCase):
    def run_with(self, zot, hyp_by_url, dry=False):
        def fake_fetch(session, url, user):
            if isinstance(hyp_by_url.get(url), Exception):
                raise hyp2zot.requests.HTTPError("boom")
            return hyp_by_url.get(url, [])
        with mock.patch.object(hyp2zot.zotero, "Zotero", return_value=zot), \
             mock.patch.object(hyp2zot, "fetch_annotations", side_effect=fake_fetch):
            return hyp2zot.run(CFG, 50, dry)

    def test_imports_new_skips_reply_and_empty_and_existing(self):
        zot = FakeZot(items(("A", "https://x.test/a")),
                      {"A": [child_note("old1")]})
        hyp = {"https://x.test/a": [ann("new1"), ann("old1"), ann("r1", refs=["new1"]),
                                    ann("empty", exact="", text="")]}
        # 'empty' has an exact of "" and no comment, so nothing to import
        hyp["https://x.test/a"][3]["target"][0]["selector"][0]["exact"] = ""
        res = self.run_with(zot, hyp)
        self.assertEqual(res.notes_created, 1)
        self.assertEqual(len(zot.created), 1)
        note = zot.created[0][1][0]
        self.assertIn("quoted text", note["note"])
        self.assertIn("https://hyp.is/new1", note["note"])
        tags = {t["tag"] for t in note["tags"]}
        self.assertEqual(tags, {"t1", "hyp-annotation"})  # no per-annotation ID tag
        self.assertFalse(res.errors)

    def test_second_run_is_idempotent(self):
        children = {"A": [child_note("new1")]}
        zot = FakeZot(items(("A", "https://x.test/a")), children)
        res = self.run_with(zot, {"https://x.test/a": [ann("new1")]})
        self.assertEqual(res.notes_created, 0)
        self.assertEqual(zot.created, [])

    def test_dedup_reads_ids_from_note_links(self):
        zot = FakeZot([], {"A": [child_note("abc123"), child_note("def_456-x")]})
        self.assertEqual(hyp2zot.existing_annotation_ids(zot, "A"), {"abc123", "def_456-x"})

    def test_id_in_plain_text_is_not_treated_as_imported(self):
        body = "<p>see https://hyp.is/looksreal for context</p><p>and hyp.is/another</p>"
        zot = FakeZot([], {"A": [{"data": {"itemType": "note", "note": body, "tags": []}}]})
        self.assertEqual(hyp2zot.existing_annotation_ids(zot, "A"), set())

    def test_dedup_ignores_non_notes_and_notes_without_links(self):
        children = {"A": [{"data": {"itemType": "attachment", "note": '<a href="https://hyp.is/zzz">x</a>'}},
                          {"data": {"itemType": "note", "note": "<p>my own note</p>", "tags": []}},
                          {"data": {"itemType": "note"}}]}
        self.assertEqual(hyp2zot.existing_annotation_ids(FakeZot([], children), "A"), set())

    def test_dedup_accepts_single_quoted_href(self):
        body = "<p><a href='https://hyp.is/quoted1'>view</a></p>"
        zot = FakeZot([], {"A": [{"data": {"itemType": "note", "note": body}}]})
        self.assertEqual(hyp2zot.existing_annotation_ids(zot, "A"), {"quoted1"})

    def test_skips_items_without_url(self):
        zot = FakeZot(items(("A", ""), ("B", "not a url")), {})
        res = self.run_with(zot, {})
        self.assertEqual(res.items_skipped_no_url, 2)
        self.assertEqual(zot.created, [])

    def test_one_failing_item_does_not_stop_others(self):
        hyp = {"https://x.test/a": hyp2zot.requests.HTTPError("x"), "https://x.test/b": [ann("b1")]}
        zot = FakeZot(items(("A", "https://x.test/a"), ("B", "https://x.test/b")), {})
        res = self.run_with(zot, hyp)
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.notes_created, 1)

    def test_dry_run_writes_nothing(self):
        zot = FakeZot(items(("A", "https://x.test/a")), {})
        res = self.run_with(zot, {"https://x.test/a": [ann("n1"), ann("n2")]}, dry=True)
        self.assertEqual(res.notes_created, 2)
        self.assertEqual(zot.created, [])

    def test_batches_of_50(self):
        zot = FakeZot(items(("A", "https://x.test/a")), {})
        res = self.run_with(zot, {"https://x.test/a": [ann(f"n{i}") for i in range(120)]})
        self.assertEqual([len(c[1]) for c in zot.created], [50, 50, 20])
        self.assertEqual(res.notes_created, 120)

    def test_html_is_escaped(self):
        body = hyp2zot.build_note(ann("e", exact="<script>x</script> & y", text="a < b\nline2"))
        assert body is not None  # build_note returns None only for empty annotations
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("line2", body)

    def test_comment_only_page_note_imports(self):
        a = ann("p1", text="just a comment")
        a["target"] = [{}]
        body = hyp2zot.build_note(a)
        assert body is not None
        self.assertIn("just a comment", body)

    def test_fetch_pages_through_results(self):
        pages = [{"rows": [ann(f"a{i}") for i in range(200)]}, {"rows": [ann("last")]}]
        calls = []
        class Resp:
            def __init__(self, p): self.p = p
            def raise_for_status(self): pass
            def json(self): return self.p
        class Sess:
            def get(self, url, params, timeout):
                calls.append(params["offset"]); return Resp(pages[len(calls) - 1])
        session = cast(requests.Session, Sess())  # a stand-in with just the one method used
        rows = hyp2zot.fetch_annotations(session, "https://x.test/a", "acct:me@hypothes.is")
        self.assertEqual(len(rows), 201)
        self.assertEqual(calls, [0, 200])

    def test_config_missing_and_username_normalised(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(hyp2zot.ConfigError):
                hyp2zot.load_config()
        env = {"ZOTERO_LIBRARY_ID": "1", "ZOTERO_API_KEY": "k", "HYPOTHESIS_USERNAME": "michele",
               "HYPOTHESIS_API_KEY": "h"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(hyp2zot.load_config().hyp_user, "acct:michele@hypothes.is")

    def _write_env(self, text, mode=0o600):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
        self.addCleanup(os.unlink, path)
        return path

    def test_env_file_supplies_config(self):
        path = self._write_env('ZOTERO_LIBRARY_ID=42\nZOTERO_API_KEY="zk"\n'
                               "export HYPOTHESIS_USERNAME=michele\n# comment\nHYPOTHESIS_API_KEY=hk\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            hyp2zot.load_env_file(path)
            cfg = hyp2zot.load_config()
        self.assertEqual((cfg.library_id, cfg.zotero_key, cfg.hyp_user, cfg.hyp_key),
                         ("42", "zk", "acct:michele@hypothes.is", "hk"))

    def test_env_file_replaces_existing_environment(self):
        path = self._write_env("ZOTERO_API_KEY=from_file\n")
        with mock.patch.dict(os.environ, {"ZOTERO_API_KEY": "from_env"}, clear=True):
            hyp2zot.load_env_file(path)
            self.assertEqual(os.environ["ZOTERO_API_KEY"], "from_file")

    def test_missing_env_file_is_an_error(self):
        with self.assertRaises(hyp2zot.ConfigError):
            hyp2zot.load_env_file("/nonexistent/hyp2zot.env")

    def test_loose_permissions_warn(self):
        path = self._write_env("ZOTERO_API_KEY=x\n", mode=0o644)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertLogs("hyp2zot", "WARNING"):
            hyp2zot.load_env_file(path)

    def test_missing_vars_message_mentions_flag(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(hyp2zot.ConfigError) as ctx:
                hyp2zot.load_config()
        self.assertIn("--env-file", str(ctx.exception))

    def test_main_missing_env_file_exits_2_with_toast(self):
        with mock.patch.object(hyp2zot, "notify") as n, \
             mock.patch.object(hyp2zot, "run") as r:
            self.assertEqual(hyp2zot.main(["--env-file", "/nonexistent/x.env"]), 2)
            n.assert_called_once()
            r.assert_not_called()

    def test_main_exit_codes_and_notifications(self):
        with mock.patch.object(hyp2zot, "load_config", side_effect=hyp2zot.ConfigError("missing")), \
             mock.patch.object(hyp2zot, "notify") as n:
            self.assertEqual(hyp2zot.main([]), 2)
            n.assert_called_once()
        with mock.patch.object(hyp2zot, "load_config", return_value=CFG), \
             mock.patch.object(hyp2zot, "run", return_value=hyp2zot.Result(errors=["x"])), \
             mock.patch.object(hyp2zot, "notify") as n:
            self.assertEqual(hyp2zot.main([]), 1)
            n.assert_called_once()
        with mock.patch.object(hyp2zot, "load_config", return_value=CFG), \
             mock.patch.object(hyp2zot, "run", return_value=hyp2zot.Result()), \
             mock.patch.object(hyp2zot, "notify") as n:
            self.assertEqual(hyp2zot.main([]), 0)
            n.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)