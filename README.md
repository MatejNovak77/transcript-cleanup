# transcript-image-cleanup

A Claude Code skill (packaged as a plugin) that finds the base64 images embedded in your Claude Code conversation history and, after you approve, replaces them with short text placeholders. The history stays and `claude --resume` keeps working. Only the pixels go.

## Why

Claude Code saves every session as JSONL under `~/.claude/projects/`. Every image a session reads gets embedded there as base64: files opened with the Read tool, browser screenshots, pasted images. It is stored **twice**: once in the message the model sees and once in a `toolUseResult` copy for the UI. Copies also land in `api-request-blob` records. A project that reads scanned documents or takes a lot of screenshots can easily grow to several gigabytes.

Real-world example: one machine went from **9.0 GB to 0.4 GB**. A single 725 MB session dropped to 72 MB and still resumed normally.

Related upstream reports: [anthropics/claude-code#27869](https://github.com/anthropics/claude-code/issues/27869) (screenshots stored twice).

## What it does

For every `image` or `document` block with base64 data:

```
[image stripped 2026-11-22: /path/to/scan_042.jpg; image/jpeg, 998904 B, sha256:77067312382b]
```

- The path comes from the matching Read tool call. If there was none, the block is labelled as a screenshot/paste.
- The UI duplicate (`toolUseResult.file.base64`) is replaced by a 1×1 PNG.
- Record uuids, timestamps, order and the file's mtime stay unchanged. Text, tool calls and tool output are untouched.

## Safety

- **Dry run by default.** It writes nothing without `--apply`.
- **Skips live sessions**, i.e. sessions of running `claude` processes, plus any file modified in the last 30 minutes.
- **Originals are moved, not deleted.** They go to the macOS Trash, or `~/.local/share/transcript-image-cleanup/backups` on Linux, or `--backup-dir`. To undo, move a file back. The space is freed only once you empty that location.
- **Validated swap.** Each file must keep the same line count and the same record uuids, and every line must parse, before the new version atomically replaces the old one.

## Install

As a plugin (this repo is its own marketplace):

```
/plugin marketplace add OWNER/transcript-image-cleanup
/plugin install transcript-image-cleanup@transcript-image-cleanup
```

Or as a personal skill:

```
git clone https://github.com/OWNER/transcript-image-cleanup
ln -s "$PWD/transcript-image-cleanup/skills/transcript-image-cleanup" ~/.claude/skills/transcript-image-cleanup
```

## Use

In Claude Code, ask something like *"how much space do images take in my Claude history?"* or *"clean images out of the transcripts of project X"*, or run `/transcript-image-cleanup`. Claude maps first, shows a per-project table, asks for approval, strips, then verifies that the biggest session still resumes.

The script also works on its own (Python 3.8+, no dependencies):

```
python3 skills/transcript-image-cleanup/scripts/strip_images.py                       # report, all projects
python3 skills/transcript-image-cleanup/scripts/strip_images.py --project my-app -v   # one project, list big files
python3 skills/transcript-image-cleanup/scripts/strip_images.py --apply               # strip
```

Example report (illustrative):

```
| project | total MB | sessions with images | image blocks | savable MB | skipped (live/recent) MB |
|---|---:|---:|---:|---:|---:|
| dev-scan-archive | 8 412 | 214 | 9 870 | 8 290 | 0 |
| dev-ui-tests | 109 | 4 | 188 | 93 | 0 |
| **total** | 9 030 | 290 | 10 512 | 8 610 | 52 |
```

`CLAUDE_CONFIG_DIR` is respected.

## Caveats

- This edits files whose format belongs to Claude Code and is not a public API. It has been tested with Claude Code 2.1.x. Keep the backup until you have resumed a stripped session.
- Live-session detection reads Claude Code's internal `~/.claude/sessions/<pid>.json` registry. The modification-age guard is the fallback.
- After stripping, a resumed session no longer contains the images. If the model needs one again, it re-reads the file from the path in the placeholder.

## License

MIT
