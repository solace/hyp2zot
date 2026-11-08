#!/usr/bin/env python3
"""
hyp2zot: Copy your Hypothesis annotations into Zotero as child notes.

Original import functionality code:
Copyright (c) 2018 John David Pressman

Headless CLI adaptation:
Copyright (c) 2026 Michele Ong

Licensed under the MIT License.

Arguments:
- `--env-file PATH` the file's values replace any already in the environment.

Exit codes: 0 ok, 1 finished with item-level errors, 2 failed to run.
"""
import argparse
import copy
import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests
from pyzotero import zotero

__version__ = "1.0.0"
USER_AGENT = f"hyp2zot/{__version__}"
HYP_API = "https://hypothes.is/api"
MARKER_TAG = "hyp-annotation"
NOTE_LINK_RE = re.compile(r"""href=["']https?://hyp\.is/([A-Za-z0-9_-]+)["']""")
WRITE_BATCH = 50            # Zotero accepts at most 50 objects per write request
HYP_PAGE = 200              # Hypothesis search maximum page size
HYP_MAX_OFFSET = 9800       # Hypothesis search offset ceiling (limit + offset <= 10000)
ZOT_PAGE_MAX = 100          # Zotero read maximum per request

log = logging.getLogger("hyp2zot")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    library_id: str
    zotero_key: str
    hyp_user: str
    hyp_key: str


@dataclass
class Result:
    items_checked: int = 0
    items_skipped_no_url: int = 0
    notes_created: int = 0
    errors: list = field(default_factory=list)


def load_env_file(path: str) -> None:
    """Load a dotenv file into os.environ. Its values replace existing ones.

    A missing file is an error: silently falling back to whatever is already in
    the environment could run the script against the wrong account.
    """
    from dotenv import load_dotenv  # only needed when --env-file is used

    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise ConfigError(f"env file not found: {env_path}")
    if env_path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; consider chmod 600", env_path)
    load_dotenv(env_path, override=True)


def load_config() -> Config:
    names = ["ZOTERO_LIBRARY_ID", "ZOTERO_API_KEY", "HYPOTHESIS_USERNAME", "HYPOTHESIS_API_KEY"]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(missing)
                          + " (set them in the environment or pass --env-file PATH)")
    user = os.environ["HYPOTHESIS_USERNAME"].strip()
    if not user.startswith("acct:"):
        user = f"acct:{user}@hypothes.is"
    return Config(
        library_id=os.environ["ZOTERO_LIBRARY_ID"].strip(),
        zotero_key=os.environ["ZOTERO_API_KEY"].strip(),
        hyp_user=user,
        hyp_key=os.environ["HYPOTHESIS_API_KEY"].strip(),
    )


def notify(title: str, message: str) -> None:
    """Show a macOS notification. Silent no-op elsewhere or if osascript fails."""
    if sys.platform != "darwin":
        return
    message = message.replace("\n", " ")[:200]
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:  # never let the notifier mask the real error
        log.warning("could not show notification")


# ---------------------------------------------------------------- Hypothesis

def fetch_annotations(session: requests.Session, url: str, user: str) -> list:
    rows, offset = [], 0
    while True:
        params: dict[str, str | int] = {
            "url": url, "user": user, "limit": HYP_PAGE, "offset": offset,
            "sort": "created", "order": "asc",
        }
        resp = session.get(f"{HYP_API}/search", params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json().get("rows", [])
        rows.extend(batch)
        if len(batch) < HYP_PAGE:
            return rows
        offset += HYP_PAGE
        if offset >= HYP_MAX_OFFSET:
            log.warning("hit Hypothesis offset ceiling for %s; later annotations skipped", url)
            return rows


def extract_quote(annotation: dict):
    for target in annotation.get("target") or []:
        for selector in target.get("selector") or []:
            exact = selector.get("exact")
            if exact:
                return exact
    return None


def build_note(annotation: dict):
    """Return note HTML, or None if there is nothing worth importing."""
    quote = extract_quote(annotation)
    comment = (annotation.get("text") or "").strip()
    if not quote and not comment:
        return None

    def esc(s: str) -> str:
        return html.escape(s).replace("\n", "<br>")

    parts = []
    if quote:
        parts.append(f"<blockquote><p>{esc(quote)}</p></blockquote>")
    if comment:
        parts.append(f"<p>{esc(comment)}</p>")
    created = (annotation.get("created") or "")[:10]
    link = f"https://hyp.is/{annotation['id']}"
    parts.append(f'<p><small>Hypothesis, {created} &middot; <a href="{link}">view in context</a></small></p>')
    return "\n".join(parts)


# -------------------------------------------------------------------- Zotero

def list_items(zot, limit: int) -> list:
    kwargs = {"sort": "dateModified", "direction": "desc"}
    if limit <= ZOT_PAGE_MAX:
        return zot.top(limit=limit, **kwargs)
    return zot.everything(zot.top(**kwargs))[:limit]


def existing_annotation_ids(zot, item_key: str) -> set:
    """IDs of annotations already imported under this item.

    Read from the 'view in context' link in each child note's body, so no
    per-annotation tag is needed. Only an href counts: an ID that merely appears
    as text (for example inside a comment) is not treated as imported.
    """
    ids = set()
    for child in zot.everything(zot.children(item_key)):
        data = child.get("data", {})
        if data.get("itemType") != "note":
            continue
        ids.update(NOTE_LINK_RE.findall(data.get("note") or ""))
    return ids


# ----------------------------------------------------------------------- run

def run(cfg: Config, limit: int, dry_run: bool) -> Result:
    result = Result()
    zot = zotero.Zotero(cfg.library_id, "user", cfg.zotero_key)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Authorization"] = f"Bearer {cfg.hyp_key}"

    items = list_items(zot, limit)
    log.info("checking %d items", len(items))
    note_template = None  # fetched lazily, so dry runs and empty runs skip the request

    for entry in items:
        data = entry.get("data", {})
        key = entry.get("key", "?")
        title = data.get("title") or key
        url = (data.get("url") or "").strip()
        result.items_checked += 1
        if not url.lower().startswith(("http://", "https://")):
            result.items_skipped_no_url += 1
            continue

        try:
            annotations = fetch_annotations(session, url, cfg.hyp_user)
            if not annotations:
                continue
            seen = existing_annotation_ids(zot, key)
            new_notes = []
            for ann in annotations:
                if ann.get("references"):        # a reply, not a highlight
                    continue
                if ann["id"] in seen:
                    continue
                body = build_note(ann)
                if body is None:
                    continue
                if dry_run:
                    log.info("[dry run] would import %s on %r", ann["id"], title)
                    result.notes_created += 1
                    continue
                if note_template is None:
                    note_template = zot.item_template("note")
                note = copy.deepcopy(note_template)
                note["note"] = body
                note["tags"] = [{"tag": t, "type": 0} for t in ann.get("tags", [])]
                note["tags"] += [{"tag": MARKER_TAG, "type": 1}]
                new_notes.append(note)

            for i in range(0, len(new_notes), WRITE_BATCH):
                chunk = new_notes[i:i + WRITE_BATCH]
                resp = zot.create_items(chunk, key)
                failed = resp.get("failed") or {}
                result.notes_created += len(resp.get("successful") or {})
                if failed:
                    raise RuntimeError(f"Zotero rejected {len(failed)} notes: {failed}")
            if new_notes:
                log.info("imported %d notes on %r", len(new_notes), title)
        except Exception as exc:  # keep going; report at the end
            log.exception("error on item %s (%r)", key, title)
            result.errors.append(f"{title}: {type(exc).__name__}: {exc}")

    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--limit", type=int, default=50,
                        help="check the N most recently modified top-level items (default 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported without writing to Zotero")
    parser.add_argument("--notify-success", action="store_true",
                        help="also show a notification when notes were imported")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", metavar="PATH",
                        help="load credentials from this dotenv file; its values replace "
                             "any already set in the environment. Use an absolute path "
                             "when running under launchd")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.env_file:
            load_env_file(args.env_file)
        result = run(load_config(), args.limit, args.dry_run)
    except Exception as exc:
        log.exception("hyp2zot could not run")
        notify("Hypothesis to Zotero failed", f"{type(exc).__name__}: {exc}. Check the error log.")
        return 2

    log.info("done: %d items checked, %d without URL skipped, %d notes %s, %d errors",
             result.items_checked, result.items_skipped_no_url, result.notes_created,
             "would be created" if args.dry_run else "created", len(result.errors))

    if result.errors:
        notify("Hypothesis to Zotero finished with errors",
               f"{len(result.errors)} item(s) failed. Check the error log.")
        return 1
    if args.notify_success and result.notes_created and not args.dry_run:
        notify("Hypothesis to Zotero", f"Imported {result.notes_created} new notes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())