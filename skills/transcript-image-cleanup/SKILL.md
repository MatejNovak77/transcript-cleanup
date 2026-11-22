---
name: transcript-image-cleanup
description: Map the base64 images and PDFs embedded in Claude Code conversation history (~/.claude/projects) for one project or all of them, and, after the user approves, replace them with short text placeholders so the history and `claude --resume` keep working while gigabytes are freed. Use when the user asks how much space images take in Claude Code transcripts, wants to clean up / shrink / slim down Claude Code history, remove images from past conversations, when ~/.claude is eating disk space, or when a session fails to resume because it is too large or hits "image exceeds dimension limit".
---

# Clean embedded images out of Claude Code transcripts

Claude Code stores every image it reads (Read tool, browser screenshots, pasted images) inside the session JSONL as base64 — **twice** (`message.content` for the model plus `toolUseResult.file.base64` for the UI), plus copies in `api-request-blob` records. Projects that read scans or take many screenshots grow to gigabytes.

Tool: `scripts/strip_images.py` in this skill's base directory. Always run it with `python3 -I "<skill-dir>/scripts/strip_images.py"`.

What it does to each image: the block becomes `[image stripped <date>: <path of the file that was Read>; image/jpeg, <bytes>, sha256:…]` and the UI duplicate becomes a 1×1 PNG. Record uuids, timestamps, order and the file's mtime stay unchanged, so resume and the session picker keep working. Conversation text, tool calls and tool output are untouched; only the pixels go. Image files on disk are never touched.

Built-in safety:
- Without `--apply` it only reports (dry run).
- It skips sessions of running `claude` processes and files modified in the last 30 minutes (`--min-age`).
- It **moves** originals to a backup folder: the macOS Trash by default, otherwise `~/.local/share/transcript-image-cleanup/backups` (or `--backup-dir`). Space is freed only after the user empties it. To undo, move a file back.
- Each file is validated before the swap (same line count, same record uuids, every line parses).

## Procedure

1. **Map (read-only, always first).** Run for all projects, or for one or more projects matched by a substring of the project directory name:
   ```
   python3 -I "<skill-dir>/scripts/strip_images.py"
   python3 -I "<skill-dir>/scripts/strip_images.py" --project my-project
   ```
   `-v` also lists individual files that would shrink by more than 5 MB. The output is a markdown table (project, total MB, sessions with images, image blocks, savable MB, MB skipped as live/recent). Show it to the user.

2. **Live sessions.** If the "skipped (live/recent)" column holds a significant share, name those projects and offer to re-run once the user has closed those sessions. The session you are running in is always skipped, which is expected.

3. **Approval.** Do not rewrite anything until the user explicitly approves that specific scope (everything, or the named projects).

4. **Apply** with the same filters used for mapping:
   ```
   python3 -I "<skill-dir>/scripts/strip_images.py" --apply [--project …]
   ```
   The script prints the backup folder.

5. **Verify.** Check that the largest stripped session still loads, without appending to it. Run this from that session's original working directory (the `cwd` field in its JSONL):
   ```
   cd "<cwd>" && claude -p --resume <session-id> --fork-session --model haiku "In one sentence: how did this conversation end?"
   ```
   The fork creates a new `.jsonl` in that project directory: the newest one, containing the question. Move it into the same backup folder.

6. **Summarize:** how much space will be freed once the backup is emptied, the backup folder path, and what was skipped. Do not empty the Trash or delete the backup yourself. That is the user's safety net.

## Notes
- Re-running is safe: already-stripped files have nothing left to strip.
- Long sessions are usually compacted, so after resume the model sees only the summary and the tail. Missing placeholders in the model's view are therefore normal.
- The live-session check relies on Claude Code's internal `~/.claude/sessions/<pid>.json` registry. The `--min-age` guard is the fallback if that format changes.
