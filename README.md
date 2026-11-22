# transcript-cleanup

A Claude Code skill (packaged as a plugin) that finds what bloats your Claude Code conversation history — base64 images, and repeated system data such as saved API request records — and, after you approve, removes it. The history stays and `claude --resume` keeps working. Only the pixels and the copies Claude Code does not read back go.

## Why

Claude Code saves every session as JSONL under `~/.claude/projects/`. Every image a session reads gets embedded there as base64: files opened with the Read tool, browser screenshots, pasted images. It is stored **twice**: once in the message the model sees and once in a `toolUseResult` copy for the UI. Copies also land in `api-request-blob` records. A project that reads scanned documents or takes a lot of screenshots can easily grow to several gigabytes.

Real-world example: one machine went from **9.0 GB to 0.4 GB**. A single 725 MB session dropped to 72 MB and still resumed normally.

Claude Code's own retention setting, [`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference.md#cleanupperioddays) (default 30), deletes whole session transcripts — and their file history, plans and caches — once they are older than that. This tool is for keeping old sessions resumable while dropping only the pixels; if you don't need the history at all, the setting is the simpler choice.

Related upstream report: [anthropics/claude-code#27869](https://github.com/anthropics/claude-code/issues/27869) — screenshots kept in a resumed session's context drain tokens (closed as not planned).

## What it does

For every `image` or `document` block with base64 data:

```
[image stripped 2026-11-22: /path/to/scan_042.jpg; image/jpeg, 998904 B, sha256:77067312382b]
```

- The path comes from the matching Read tool call. If there was none, an image is labelled `inline image (screenshot/paste)` and a document `inline document (see preceding tool result)` (older Claude Code versions put a read PDF in a separate record; the record before it names the file).
- Size and `sha256` are of the embedded image bytes. They match the original file (`shasum -a 256`, first 12 hex digits) unless Claude Code downscaled the image before embedding it, which it does for large images.
- The UI duplicate (`toolUseResult.file.base64`) is replaced by a 1×1 PNG.
- Record uuids, timestamps, order and the file's mtime stay unchanged. Text, tool calls and tool output are untouched.

## System data

Besides images, recent Claude Code versions (2.1.300 and later) save API request records in every transcript: `api-request`, `api-request-blob` and `api-request-shape`. They repeat the request parameters, tool definitions and message content already stored elsewhere in the file, are not part of the conversation chain (they carry no `uuid`), and in practice make up roughly 15 % of an image-free history. With `--clean system-data` (or `--clean all`) the script:

| what | how | measured on a 1 GB history |
|---|---|---:|
| `api-request`, `api-request-blob`, `api-request-shape` records without a `uuid` | removed | ~155 MB |
| `skill_listing` attachments: the skill list text, when the `rendered` copy the model saw contains it | emptied (`""`), record kept | ~7 MB |

Left alone, because a resume reads them (measured on Claude Code 2.1.330 by comparing the API request a resumed session sends with and without them):

- `prompt_snapshot` — the resumed request's system prompt is rebuilt from it.
- `instructions` (CLAUDE.md copies) — a resume compares the stored copy with the file on disk and re-sends the whole file when they differ.
- the `rendered` text of any attachment — it is what the model saw.
- small per-turn attachments (deferred-tool and agent-list deltas, reminders), `progress` and `file-history-snapshot` records (used by rewind) — not repeated system data.

How it was checked: test sessions (including a compacted one, one with a subagent, one started interactively, and one written by 2.1.300) were cleaned and then resumed with `--resume`, `--continue` and `--fork-session`, a second turn was sent, a subagent was continued, and the interactive transcript view, `/rewind` list and session picker were captured. The request sent to the API was byte-identical to that of the uncleaned session in every case, and the model still recalled the earlier conversation.

## Safety

- **Dry run by default.** It writes nothing without `--apply`.
- **Skips live sessions**, i.e. sessions of running `claude` processes, plus any file modified in the last 30 minutes.
- **Originals are kept, not deleted.** They go to the macOS Trash, or `~/.local/share/transcript-cleanup/backups` on Linux, or `--backup-dir` — as a hard link (instant, no extra space) or a copy when that folder is on another volume. An existing backup is never overwritten. To undo, move a file back. The space is freed only once you empty that location.
- **Validated, atomic swap.** Image stripping must keep every line with the same record uuid and type, and every line must parse. System-data cleanup is checked independently before the swap: every other line must stay byte-for-byte identical and in order, only uuid-less API request records may disappear, and a `skill_listing` may only lose text that its `rendered` copy still holds. A file that changed while it was being processed (a session that came alive) is skipped and reported; only a write in the instant between that last check and the swap would end up in the backup alone. The stripped version then replaces the original in one atomic rename, so an error or Ctrl-C leaves either the original or the complete stripped file.

## Install

As a plugin (this repo is its own marketplace):

```
/plugin marketplace add MatejNovak77/transcript-cleanup
/plugin install transcript-cleanup@transcript-cleanup
```

Or as a personal skill:

```
git clone https://github.com/MatejNovak77/transcript-cleanup
ln -s "$PWD/transcript-cleanup/skills/transcript-cleanup" ~/.claude/skills/transcript-cleanup
```

## Use

In Claude Code, ask something like *"how much space do images take in my Claude history?"*, *"slim down my Claude Code transcripts"* or *"clean images out of the transcripts of project X"*, or run `/transcript-cleanup`. Claude maps first, shows a per-project table with images and system data separately, asks which kinds to remove and for approval, cleans, then verifies that the biggest session still resumes.

The script also works on its own (Python 3.8+, no dependencies):

```
python3 skills/transcript-cleanup/scripts/strip_images.py                       # report, all projects
python3 skills/transcript-cleanup/scripts/strip_images.py --project my-app -v   # one project, list big files
python3 skills/transcript-cleanup/scripts/strip_images.py --apply               # strip images
python3 skills/transcript-cleanup/scripts/strip_images.py --apply --clean system-data   # system data only
python3 skills/transcript-cleanup/scripts/strip_images.py --apply --clean all   # both
```

The report always shows both kinds; `--clean` (default `images`) only selects what `--apply` removes. System data is counted after images, so the two columns add up to what `--clean all` frees. API request records can hold copies of images; those copies count as images in the report, so `--clean system-data` alone frees more than its column shows.

Example report (illustrative):

```
| project | total MB | sessions with images | image blocks | images savable MB | system data savable MB | skipped (live/recent) MB |
|---|---:|---:|---:|---:|---:|---:|
| dev-scan-archive | 8 412 | 214 | 9 870 | 8 290 | 21 | 0 |
| dev-ui-tests | 109 | 4 | 188 | 93 | 4 | 0 |
| **total** | 9 030 | 290 | 10 512 | 8 610 | 58 | 52 |
```

`CLAUDE_CONFIG_DIR` is respected.

## Caveats

- This edits files whose format belongs to Claude Code and is not a public API. Last checked with Claude Code 2.1.330; only record kinds measured there are touched, by exact name. Keep the backup until you have resumed a cleaned session.
- Removed API request records may serve Claude Code features the measurement did not exercise (bug reports, internal diagnostics); prompt caching of a resumed session was unaffected in the tests. They come back only from the backup.
- Live-session detection reads Claude Code's internal `~/.claude/sessions/<pid>.json` registry. The modification-age guard is the fallback.
- macOS and Linux only; on Windows the script refuses to run (use it inside WSL for WSL-based Claude Code).
- The script holds a whole transcript in memory several times over: expect about 3–4× the size of the largest session file.
- After stripping, a resumed session no longer contains the images. If the model needs one again, it re-reads the file from the path in the placeholder.

## License

MIT
