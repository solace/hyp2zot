# hyp2zot

Copies your Hypothesis annotations into Zotero as child notes (quote, your 
comment, link back to the annotation in context).

This project is not affiliated with Zotero or Hypothesis. It has been created 
as a personal tool, and as such, no support will be provided.

## Acknowledgements & Attribution

This project contains a headless rewrite of the import logic from [JD-P/hypothesis-zotero
](https://github.com/JD-P/hypothesis-zotero/). 

* **Original Project:** https://github.com/JD-P/hypothesis-zotero/
* **Original Author:** John David Pressman
* **License:** [MIT License](https://github.com/JD-P/hypothesis-zotero/blob/master/LICENSE)

## Features

This standalone version removes the GUI to allow for CLI usage and will use 
Apple notifications for success or failure messages, if available.

It has been designed specifically for running from launchd on a schedule, 
and manually via CLI as needed.

### Differences from the original

- no GUI; config comes from environment variables
- pages through Hypothesis results (the original made one request)
- writes notes in batches of 50 (the Zotero write limit per request)
- skips items with no URL, replies, and annotations with no content
- HTML-escapes quote and comment text
- one failing item does not stop the rest
- --dry-run, and a macOS notification when something fails

### Limitations

- Only your own top-level annotations are imported, replies are skipped.
- Only syncs with your personal Zotero library, not group libraries.
- Edits or deletions in Hypothesis are not propagated.
- Only imports as Notes. It does not create reader annotations, which was 
also unsupported in the original code.
- Uses the Zotero web API, so items must sync first.
- The Zotero URL field must match the Hypothesis annotated URL exactly.
- This has only been tested on macOS.

## Installation

Git clone this repository:

```
git clone https://github.com/solace/hyp2zot
```

Install the dependencies:

```
uv sync
```

## Environment variables

The following environment variables will be required for the script to run. 
They are assumed to be available in the environment, but can be set or 
overridden using the `--env-file` argument.

```
ZOTERO_LIBRARY_ID   numeric user ID
ZOTERO_API_KEY      key with read/write access to your library
HYPOTHESIS_USERNAME the username you use to log into Hypothesis
HYPOTHESIS_API_KEY  from hypothes.is/account/developer
```

The Zotero library ID and a read/write key can be found at https://www.zotero.org/settings/security#applications.
The Hypothesis key can be found at https://hypothes.is/account/developer.

## Usage

You can test that the script works by invoking:

```
uv run hyp2zot.py --dry-run
```

You can run the script manually as needed with:

```
# env vars already exported
uv run hyp2zot.py
# set or override env vars
uv run hyp2zot.py --env-var /path/to/env/file
```

Or you can add it to `launchd` to periodically update Zotero. The example template `plist` file in this repository will fetch from Hypothesis daily at 9am or at the time/frequency you require.

## Troubleshooting

### Apple notifications are not displaying

Notifications are only shown if there is an error to display, or if the `--notify-success` flag is set *and* there were notes to import.

If notifications do not display, test notifications in Terminal with:

```
osascript -e 'display notification "test" with title "Test"'
```

If that doesn't work, check System Settings -> Notifications and allow notifications for for Script Editor. 