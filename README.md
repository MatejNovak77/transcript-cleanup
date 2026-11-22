# transcript-cleanup

Shrink your Claude Code conversation history without losing it. This Claude Code skill finds the base64 images and repeated system data stored in your session transcripts and, after you approve, removes them. The conversations stay readable and `claude --resume` keeps working.

- **Images and PDFs** become one-line text placeholders that name the original file.
- **Saved API request records** and duplicate skill-list text are removed.
- **Nothing is deleted outright:** originals go to the Trash or a backup folder, and the default run only reports.

One machine went from **9.0 GB to 0.4 GB**; a single 725 MB session dropped to 72 MB and still resumed normally.

## Quick start

Install as a plugin (this repo is its own marketplace):

```
/plugin marketplace add MatejNovak77/transcript-cleanup
/plugin install transcript-cleanup@transcript-cleanup
```

Then ask Claude Code something like:

> How much space do images take in my Claude history?
>
> Slim down my Claude Code transcripts, but only sessions older than 3 months.

or run `/transcript-cleanup`. Claude shows a per-project report, asks which kinds to remove and which age limit to use, waits for your approval, cleans, and offers to check that the largest cleaned session still resumes.

<details>
<summary>Install as a personal skill instead</summary>

```
git clone https://github.com/MatejNovak77/transcript-cleanup
ln -s "$PWD/transcript-cleanup/skills/transcript-cleanup" ~/.claude/skills/transcript-cleanup
```

</details>

## Why transcripts grow

Claude Code saves every session as JSONL under `~/.claude/projects/`. Every image a session reads is embedded there as base64: files opened with the Read tool, browser screenshots, pasted images. Each one is stored twice, once in the message the model sees and once in a `toolUseResult` copy for the UI, and further copies land in `api-request-blob` records. A project that reads scanned documents or takes many screenshots can grow to several gigabytes.

Claude Code's own retention setting, [`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference#cleanupperioddays) (default 30), deletes whole sessions once they are older than that, along with their file history, plans and caches. If you don't need old conversations at all, that setting is the simpler choice. This tool is for people who keep their history and want it smaller.

## What gets removed

| Kind | What happens | Option |
|---|---|---|
| `image` and `document` blocks with base64 data | replaced by a text placeholder | `--clean images` (default) |
| UI duplicate of a read image (`toolUseResult.file.base64`) | replaced by a 1×1 PNG | `--clean images` |
| `api-request`, `api-request-blob`, `api-request-shape` records without a `uuid` | removed | `--clean system-data` |
| `skill_listing` attachment text, when the `rendered` copy the model saw contains it | emptied, record kept | `--clean system-data` |

A placeholder looks like this:

```
[image stripped 2026-11-22: /path/to/scan_042.jpg; image/jpeg, 998904 B, sha256:77067312382b]
```

- The path comes from the matching Read tool call. Without one, an image is labelled `inline image (screenshot/paste)` and a document `inline document (see preceding tool result)`.
- Size and `sha256` are of the embedded bytes. They match the original file (`shasum -a 256`, first 12 hex digits) unless Claude Code downscaled the image before embedding it, which it does for large images.

API request records exist since Claude Code 2.1.300. They repeat request parameters, tool definitions and message content already stored elsewhere in the file and are not part of the conversation chain. On a 1 GB image-free history they came to about 155 MB, and the skill-list text to about 7 MB.

**What stays.** Record uuids, timestamps, order and the file's modification time are unchanged. Text, tool calls and tool output are untouched. These records are deliberately left alone because a resume reads them:

- `prompt_snapshot`: the resumed request's system prompt is rebuilt from it.
- `instructions` (CLAUDE.md copies): a resume compares the stored copy with the file on disk and re-sends the whole file when they differ.
- The `rendered` text of any attachment: it is what the model saw.
- Small per-turn attachments, `progress` and `file-history-snapshot` records (used by rewind).

## Safety

- **Dry run by default.** Nothing is written without `--apply`.
- **Live sessions are skipped:** sessions of running `claude` processes, plus any file modified in the last 30 minutes.
- **Originals are kept.** They go to the macOS Trash, to `~/.local/share/transcript-cleanup/backups` on Linux, or to `--backup-dir`, as a hard link (instant, no extra space) or as a copy when that folder is on another volume. An existing backup is never overwritten. To undo, move a file back. Space is freed only once you empty that location.
- **Validated before the swap.** For images, every line must keep the same record uuid and type and must parse. For system data, every other line must stay byte-for-byte identical and in order; only uuid-less API request records may disappear, and a `skill_listing` may only lose text its `rendered` copy still holds.
- **Atomic swap.** The cleaned file replaces the original in one rename, so an error or Ctrl-C leaves either the original or the complete cleaned file. A file that changed while it was being processed is skipped and reported. Only a write in the instant between that last check and the rename would end up in the backup alone.

## Command line

The script also runs on its own (Python 3.8+, no dependencies, macOS and Linux):

```
S=skills/transcript-cleanup/scripts/transcript_cleanup.py

python3 $S                                  # report, all projects
python3 $S --project my-app -v              # one project, list big files
python3 $S --apply                          # strip images
python3 $S --apply --clean system-data      # system data only
python3 $S --apply --clean all              # both
python3 $S --apply --clean all --older-than 90          # only sessions idle for 90+ days
python3 $S --apply --clean all --system-older-than 90   # images of any age, system data after 90 days
```

Example report (illustrative):

```
| project | total MB | sessions with images | image blocks | images savable MB | system data savable MB | skipped (live/recent) MB |
|---|---:|---:|---:|---:|---:|---:|
| dev-scan-archive | 8 412 | 214 | 9 870 | 8 290 | 21 | 0 |
| dev-ui-tests | 109 | 4 | 188 | 93 | 4 | 0 |
| **total** | 9 030 | 290 | 10 512 | 8 610 | 58 | 52 |
```

The report always shows both kinds; `--clean` only selects what `--apply` removes. System data is counted after images, so the two columns add up to what `--clean all` frees. API request records can hold copies of images, and those copies count as images in the report, so `--clean system-data` alone frees more than its column shows.

**Age limits.** `--older-than DAYS` cleans only sessions whose last activity is more than DAYS days ago. `--images-older-than` and `--system-older-than` set the limit for one kind and override `--older-than`. Without a limit every session is cleaned. A session's last activity is the newest modification time of its transcript and its subagent transcripts. A cleanup run preserves modification times, so it never makes a session look younger. With a limit, the report states the limits and adds a `younger than limit MB` column. The live/recent guard (`--min-age`, 30 minutes) always applies first.

`CLAUDE_CONFIG_DIR` is respected.

## How it was verified

Checked on Claude Code 2.1.330. Test sessions (a compacted one, one with a subagent, one started interactively, one written by 2.1.300) were cleaned and then resumed with `--resume`, `--continue` and `--fork-session`. A second turn was sent, a subagent was continued, and the transcript view, `/rewind` list and session picker were captured. The request sent to the API was byte-identical to that of the uncleaned session in every case, and the model still recalled the earlier conversation.

The repository has a test suite on synthetic transcripts (`python3 -m pytest tests`, needs `pytest`). It covers the apply path under injected failures: interrupts, a full or unwritable backup folder, files that change mid-run.

## Caveats

- The transcript format belongs to Claude Code and is not a public API. Only record kinds measured on 2.1.330 are touched, by exact name. Keep the backup until you have resumed a cleaned session.
- Removed API request records may serve Claude Code features the measurement did not exercise, such as bug reports or internal diagnostics. Prompt caching of a resumed session was unaffected in the tests. The records come back only from the backup.
- Live-session detection reads Claude Code's internal `~/.claude/sessions/<pid>.json` registry. The modification-age guard is the fallback.
- macOS and Linux only. On Windows the script refuses to run; use it inside WSL for WSL-based Claude Code.
- The script holds a whole transcript in memory: expect about 3–4× the size of the largest session file.
- A resumed session no longer contains the images. If the model needs one again, it re-reads the file from the path in the placeholder.
- Related upstream report: [anthropics/claude-code#27869](https://github.com/anthropics/claude-code/issues/27869), about screenshots in a resumed session's context draining tokens (closed as not planned).

## License

MIT
