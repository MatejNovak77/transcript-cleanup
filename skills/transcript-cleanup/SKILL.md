---
name: transcript-cleanup
description: Map what bloats Claude Code conversation history (~/.claude/projects) for one project or all of them — base64 images and PDFs, and repeated system data (saved API request records, skill-list copies) — and, after the user approves, replace images with short text placeholders and remove the system data, so the history and `claude --resume` keep working while space is freed. Use when the user asks how much space images take in Claude Code transcripts, wants to clean up / shrink / slim down Claude Code history, remove images from past conversations, when ~/.claude is eating disk space, or when a session fails to resume because it is too large or hits "image exceeds dimension limit".
---

# Clean embedded images and repeated system data out of Claude Code transcripts

Claude Code stores every image it reads (Read tool, browser screenshots, pasted images) inside the session JSONL as base64 — **twice** (`message.content` for the model plus `toolUseResult.file.base64` for the UI), plus copies in `api-request-blob` records. Projects that read scans or take many screenshots grow to gigabytes.

Tool: `scripts/strip_images.py` in this skill's base directory. Always run it with `python3 -I "<skill-dir>/scripts/strip_images.py"`.

System data (`--clean system-data`): since Claude Code 2.1.300 every transcript also stores API request records (`api-request`, `api-request-blob`, `api-request-shape`) that repeat request parameters, tool definitions and messages; they have no `uuid` and are removed. A `skill_listing` attachment's text is emptied when its `rendered` copy (what the model saw) holds it. `prompt_snapshot` and `instructions` records are left alone: a resume reads them (measured on 2.1.330). Every other line stays byte-for-byte identical.

What it does to each image: the block becomes `[image stripped <date>: <path of the file that was Read>; image/jpeg, <bytes>, sha256:…]` and the UI duplicate becomes a 1×1 PNG. Record uuids, timestamps, order and the file's mtime stay unchanged, so resume and the session picker keep working. Conversation text, tool calls and tool output are untouched; only the pixels go. Image files on disk are never touched.

Built-in safety:
- Without `--apply` it only reports (dry run).
- It skips sessions of running `claude` processes and files modified in the last 30 minutes (`--min-age`).
- It **keeps** originals in a backup folder: the macOS Trash by default, otherwise `~/.local/share/transcript-cleanup/backups` (or `--backup-dir`), as a hard link or, across volumes, a copy; it never overwrites an existing backup. Space is freed only after the user empties it. To undo, move a file back.
- Each file is validated before the swap (images: same record uuids and types on every line, every line parses; system data: every other line byte-identical and in order, only uuid-less API request records removed); a file that changed meanwhile is skipped (`SKIPPED` line); the swap is one atomic rename.
- macOS and Linux only: on Windows it exits with a message before doing anything.

## Procedure

1. **Map (read-only, always first).** Run for all projects, or for one or more projects matched by a substring of the project directory name:
   ```
   python3 -I "<skill-dir>/scripts/strip_images.py"
   python3 -I "<skill-dir>/scripts/strip_images.py" --project my-project
   ```
   `-v` also lists individual files that would shrink by more than 5 MB. The output is a markdown table (project, total MB, sessions with images, image blocks, images savable MB, system data savable MB, MB skipped as live/recent); a dry run always reports both kinds. Show it to the user.

2. **Live sessions.** If the "skipped (live/recent)" column holds a significant share, name those projects and offer to re-run once the user has closed those sessions. The session you are running in is always skipped, which is expected.

3. **Age limit.** If the user named an age ("only older than 3 months", "keep the last month"), use it. Otherwise ask, per kind (images / system data), whether to clean only sessions older than some age, suggesting 3 months (90 days); one limit for both is fine. Pass it in days: `--older-than DAYS` for both kinds, or `--images-older-than DAYS` / `--system-older-than DAYS` for one kind. Age = last activity of the session; the dry run with the same options shows what a limit leaves alone (`younger than limit MB`).

4. **Approval.** Ask which kinds to remove — images, system data, or both (system data is counted after images; API request records can hold image copies, so system data alone frees more than its column when images remain) — and do not rewrite anything until the user explicitly approves that specific scope (kinds, and everything or the named projects).

5. **Apply** with the same filters and age limits used for mapping:
   ```
   python3 -I "<skill-dir>/scripts/strip_images.py" --apply --clean images|system-data|all [--older-than DAYS …] [--project …]
   ```
   Without `--clean`, `--apply` strips images only.
   The script prints the backup folder.

6. **Verify.** Check that the largest stripped session still loads, without appending to it. Tell the user first that this sends the resumed session to the model — it costs tokens (a lot for a big session, even with haiku) — and runs under that project's hooks and settings; skip it or pick a smaller session if they prefer. Run this from that session's original working directory (the `cwd` field in its JSONL):
   ```
   cd "<cwd>" && claude -p --resume <session-id> --fork-session --model haiku "In one sentence: how did this conversation end?"
   ```
   The fork creates a new `.jsonl` in that project directory: the newest one, containing the question. Move it into the same backup folder.

7. **Summarize:** how much space will be freed once the backup is emptied, the backup folder path, and what was skipped. Do not empty the Trash or delete the backup yourself. That is the user's safety net.

## Notes
- Re-running is safe: already-cleaned files have nothing left to remove.
- Long sessions are usually compacted, so after resume the model sees only the summary and the tail. Missing placeholders in the model's view are therefore normal.
- The live-session check relies on Claude Code's internal `~/.claude/sessions/<pid>.json` registry. The `--min-age` guard is the fallback if that format changes.
