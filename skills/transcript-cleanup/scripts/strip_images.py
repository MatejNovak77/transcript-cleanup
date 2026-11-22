#!/usr/bin/env python3
"""Map and strip embedded base64 images/PDFs and repeated system data from Claude Code transcripts.

Claude Code stores every image it reads (Read tool, screenshots, pastes) inside
the session JSONL as base64 -- twice: once in `message.content` (what the model
sees) and once in `toolUseResult.file.base64` (for the UI), plus copies in
`api-request-blob` records. Projects that read many images grow to gigabytes.

This script replaces each such block with a short text placeholder naming the
original file (taken from the matching Read tool call) and its size, and shrinks
the UI duplicate to a 1x1 PNG. Every record keeps its uuid, timestamp and order;
the file's mtime is preserved, so `claude --resume` keeps working and listing
order does not change.

With --clean system-data (or all) it also removes repeated system data: the
api-request / -blob / -shape records (no uuid, not in the conversation chain)
and the skill_listing text that the record's `rendered` copy repeats.
prompt_snapshot and instructions records are kept: a resume reads them.

Safety:
  * dry-run report by default; nothing is written without --apply
  * sessions of running `claude` processes and files modified in the last
    --min-age minutes are skipped
  * originals are kept in a backup directory (default: the macOS Trash, or
    ~/.local/share/transcript-cleanup/backups elsewhere) as a hard link,
    or a copy when that directory is on another volume; an existing backup is
    never overwritten; undo = move the backup back
  * each rewritten file is validated (images: same record uuids/types, every
    line parses; system data: every other line byte-identical and in order);
    a file that changed while being processed is skipped; the cleaned version
    then replaces the original in one atomic rename
  * macOS and Linux only (live-session detection uses POSIX signals)

Usage:
  strip_images.py [--apply [--clean images|system-data|all]] [--project NAME ...]
                  [--min-age 30] [-v] [PATH ...]

PATH is a .jsonl file or a directory; default is the projects directory under
$CLAUDE_CONFIG_DIR (or ~/.claude). Requires Python 3.8+, no dependencies.
"""

import argparse
import base64
import binascii
import datetime
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CONFIG_DIR, "projects")
TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
STAMP = datetime.date.today().isoformat()
MIN_B64 = 1000  # base64 strings shorter than this are left alone


def default_backup_root():
    trash = os.path.join(HOME, ".Trash")
    if sys.platform == "darwin" and os.path.isdir(trash):
        return trash
    return os.path.join(HOME, ".local", "share", "transcript-cleanup", "backups")


def live_sessions():
    """Session ids of running claude processes (Claude Code's pid registry)."""
    ids = set()
    for f in glob.glob(os.path.join(CONFIG_DIR, "sessions", "*.json")):
        try:
            with open(f) as h:
                d = json.load(h)
            os.kill(int(d["pid"]), 0)
            ids.add(d["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ids


def session_id(f):
    """<sid>.jsonl -> sid; <sid>/subagents/agent-*.jsonl -> sid."""
    parent = os.path.dirname(f)
    if os.path.basename(parent) == "subagents":
        return os.path.basename(os.path.dirname(parent))
    return os.path.basename(f)[: -len(".jsonl")]


class Stripper:
    def __init__(self, path_for):
        self.path_for = path_for  # tool_use id -> file path of the Read call
        self.count = 0
        self.removed = 0

    def placeholder(self, block, tool_id):
        src = block["source"]
        data = src.get("data", "")
        self.count += 1
        self.removed += len(data)
        what = self.path_for.get(tool_id) or (
            "inline image (screenshot/paste)"
            if block["type"] == "image"
            else "inline document (see preceding tool result)"
        )
        try:  # size and hash of the embedded bytes (= the file, unless Claude Code resized it)
            raw = base64.b64decode(data, validate=True)
            size, sha = len(raw), hashlib.sha256(raw).hexdigest()[:12]
        except (binascii.Error, ValueError):  # not valid base64: describe the text
            size, sha = len(data) * 3 // 4, hashlib.sha256(data.encode()).hexdigest()[:12]
        return {
            "type": "text",
            "text": f"[{block['type']} stripped {STAMP}: {what}; "
            f"{src.get('media_type', '?')}, {size} B, sha256:{sha}]",
        }

    def walk(self, obj, tool_id=None):
        if isinstance(obj, list):
            return [
                self.placeholder(x, tool_id) if is_b64_block(x) else self.walk(x, tool_id)
                for x in obj
            ]
        if isinstance(obj, dict):
            tid = obj.get("tool_use_id", tool_id)
            out = {}
            for k, v in obj.items():
                if k == "base64" and isinstance(v, str) and len(v) > MIN_B64:
                    out[k] = TINY_PNG
                    self.removed += len(v)
                else:
                    out[k] = self.walk(v, tid)
            return out
        return obj


def is_b64_block(x):
    return (
        isinstance(x, dict)
        and x.get("type") in ("image", "document")
        and isinstance(x.get("source"), dict)
        and x["source"].get("type") == "base64"
        and len(x["source"].get("data", "")) > MIN_B64
    )


def tool_paths(records):
    """Map tool_use id -> input path, so placeholders can name the source file."""
    m = {}
    for o in records:
        msg = o.get("message") if isinstance(o, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                p = inp.get("file_path") or inp.get("path") or inp.get("imagePath")
                if p:
                    m[b.get("id")] = p
    return m


# System data (measured on Claude Code 2.1.330; see README "System data"). Only these exact kinds
# are touched; anything else -- including prompt_snapshot and instructions, which a resume reads
# -- is passed through byte for byte.
SYSTEM_DROP = frozenset(("api-request", "api-request-blob", "api-request-shape"))
SYSTEM_MARKERS = (b'"type":"api-request', b'"type":"skill_listing"')  # pre-filter, speed only


def encode(obj):
    """Compact JSON as Claude Code writes it; a lone surrogate (half an emoji) stays escaped."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    except UnicodeEncodeError:
        return json.dumps(obj, separators=(",", ":")).encode()


def rendered_text(o):
    """The text the model saw for an attachment record, or None if it has no usable copy."""
    r = o.get("rendered")
    if not isinstance(r, list) or not all(
        isinstance(x, dict) and isinstance(x.get("content"), str) for x in r
    ):
        return None
    return "".join(x["content"] for x in r)


def system_stage(lines):
    """Drop uuid-less api-request records; empty skill_listing text that `rendered` repeats.

    Returns the new list of lines. Lines that are not cleaned are passed through unchanged.
    """
    out = []
    for line in lines:
        if not any(m in line for m in SYSTEM_MARKERS):
            out.append(line)
            continue
        o = json.loads(line)
        if not isinstance(o, dict):
            out.append(line)
        elif o.get("type") in SYSTEM_DROP and "uuid" not in o:
            continue  # not part of the conversation chain: nothing points to it by uuid
        elif is_skill_listing(o) and removable_listing(o):
            o["attachment"]["content"] = ""
            out.append(encode(o) + b"\n")
        else:
            out.append(line)
    return out


def is_skill_listing(o):
    return o.get("type") == "attachment" and (
        isinstance(o.get("attachment"), dict) and o["attachment"].get("type") == "skill_listing"
    )


def removable_listing(o):
    c = o["attachment"].get("content")
    rt = rendered_text(o)
    return isinstance(c, str) and c != "" and rt is not None and c in rt


def check_system(old, new):
    """Raise unless `new` is `old` minus droppable records and with skill_listing text emptied.

    Written independently of system_stage: every other line must be byte-identical and in order.
    """
    j = 0
    for line in old:
        if j < len(new) and new[j] == line:
            j += 1
            continue
        o = json.loads(line)
        if isinstance(o, dict) and o.get("type") in SYSTEM_DROP and "uuid" not in o:
            continue  # dropped
        if j >= len(new):
            raise RuntimeError("system-data check: a record was removed")
        n = json.loads(new[j])
        j += 1
        if not (isinstance(o, dict) and isinstance(n, dict) and is_skill_listing(o)):
            raise RuntimeError("system-data check: a non-system record changed")
        oa, na = o["attachment"], n.get("attachment")
        r = o.get("rendered")
        if not (
            isinstance(na, dict)
            and na.get("content") == ""
            and isinstance(oa.get("content"), str)
            and oa["content"]
            and isinstance(r, list)
            and all(isinstance(x, dict) and isinstance(x.get("content"), str) for x in r)
            and oa["content"] in "".join(x["content"] for x in r)
            and n.get("rendered") == r
        ):
            raise RuntimeError("system-data check: skill_listing text has no copy in rendered")
        if dict(o, attachment=dict(oa, content=None)) != dict(n, attachment=dict(na, content=None)):
            raise RuntimeError("system-data check: other fields changed")
    if j != len(new):
        raise RuntimeError("system-data check: unexpected extra lines")


class Changed(Exception):
    """The transcript changed while it was being processed."""


def backup_path(f, backup_root):
    """Backup location: path relative to the projects dir, or the full path outside it."""
    full = os.path.abspath(f)
    rel = os.path.relpath(full, PROJECTS)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        rel = os.path.join("outside-projects", full.lstrip(os.sep))
    return os.path.join(backup_root, rel)


def make_backup(f, dest):
    """Hard-link f to dest, or copy it there; never overwrite, never leave a partial copy."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.link(f, dest)  # same volume: instant, no extra space; fails if dest exists
        return
    except OSError:
        pass  # other volume, no hard links, or dest exists: the "xb" open below decides
    with open(f, "rb") as src:
        dst = open(dest, "xb")  # noqa: SIM115 -- closed explicitly so a failing close is handled; "xb" never overwrites
        done = False
        try:
            try:
                shutil.copyfileobj(src, dst, 1 << 20)
                dst.flush()
                os.fsync(dst.fileno())
            finally:
                dst.close()  # can fail too (disk full while flushing)
            done = True
        finally:
            if not done:
                os.remove(dest)  # created by this call; an incomplete copy is no backup
    shutil.copystat(f, dest)


def swap_in(f, new, st0, dest):
    """Replace f with `new`, keeping the original at dest. f changes only in the final rename."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(f) or ".", prefix=os.path.basename(f) + ".", suffix=".strip-tmp"
    )  # unique name: two runs on one file never share a temp file
    try:
        with os.fdopen(fd, "wb") as h:
            h.write(new)
            h.flush()
            os.fsync(h.fileno())
        os.chmod(tmp, st0.st_mode & 0o777)
        os.utime(tmp, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        make_backup(f, dest)
        st1 = os.stat(f)
        if (st1.st_size, st1.st_mtime_ns, st1.st_ino) != (
            st0.st_size,
            st0.st_mtime_ns,
            st0.st_ino,
        ):
            os.remove(dest)  # made above from the unchanged-name file; f was not replaced
            raise Changed("changed while being processed; re-run later")
        os.replace(tmp, f)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)


def image_stage(raw):
    """Replace embedded images/PDFs; return (lines, blocks replaced). Unchanged lines pass through."""
    if not any(b'"base64"' in line for line in raw):
        return raw, 0
    parsed = [json.loads(line) if line.strip() else None for line in raw]
    s = Stripper(tool_paths([p for p in parsed if p]))
    out = []
    for line, o in zip(raw, parsed):
        if o is None or b'"base64"' not in line:
            out.append(line)
            continue
        before = s.removed
        n = s.walk(o)
        out.append(encode(n) + b"\n" if s.removed > before else line)
    if not s.removed:
        return raw, 0

    if len(out) != len(raw):
        raise RuntimeError("line count changed")
    for a, b in zip(parsed, out):
        if a is not None:
            nb = json.loads(b)
            if nb.get("uuid") != a.get("uuid") or nb.get("type") != a.get("type"):
                raise RuntimeError("record identity changed")
    return out, s.count


def process(f, apply, backup_root, clean="images"):
    """Return (bytes_before, bytes_after, image_blocks, image_bytes, system_bytes).

    A dry run measures both kinds (images first, then system data on the result); --apply runs
    and writes only the kinds selected by `clean` ("images", "system-data" or "all").
    """
    st0 = os.stat(f)  # before reading, so any later append is detected
    with open(f, "rb") as h:
        raw = h.read().splitlines(keepends=True)
    run_img = not apply or clean in ("images", "all")
    run_sys = not apply or clean in ("system-data", "all")
    size = lambda ls: sum(map(len, ls))
    lines, blocks = image_stage(raw) if run_img else (raw, 0)
    img = size(raw) - size(lines)
    sys_saved = 0
    if run_sys:
        new = system_stage(lines)
        check_system(lines, new)
        sys_saved = size(lines) - size(new)
        lines = new
    if not img and not sys_saved:
        return 0, 0, 0, 0, 0
    if apply:
        swap_in(f, b"".join(lines), st0, backup_path(f, backup_root))
    return size(raw), size(lines), blocks, img, sys_saved


def project_label(name):
    home = HOME.replace(os.sep, "-").replace(".", "-") + "-"
    return name[len(home) :] if name.startswith(home) else name


def main():
    if sys.platform == "win32":
        sys.exit(
            "transcript-cleanup: Windows is not supported (live-session detection "
            "relies on POSIX signals). Run it on macOS or Linux, or inside WSL for "
            "WSL-based Claude Code."
        )
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "paths",
        nargs="*",
        default=[PROJECTS],
        help=".jsonl files or directories (default: %(default)s)",
    )
    ap.add_argument("--apply", action="store_true", help="rewrite files (default: dry-run report)")
    ap.add_argument(
        "--clean",
        choices=("images", "system-data", "all"),
        default="images",
        help="what --apply removes: embedded images/PDFs, repeated system data, or both "
        "(default: %(default)s; a dry run always reports both)",
    )
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="NAME",
        help="only project dirs whose name contains NAME (repeatable)",
    )
    ap.add_argument(
        "--min-age",
        type=float,
        default=30,
        metavar="MIN",
        help="skip files modified less than MIN minutes ago (default: 30)",
    )
    ap.add_argument(
        "--backup-dir",
        default=None,
        help="where originals go (default: macOS Trash or ~/.local/share/...)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="list files saving more than 5 MB")
    a = ap.parse_args()

    files = []
    for p in a.paths:
        p = os.path.expanduser(p)
        files += (
            [p]
            if p.endswith(".jsonl")
            else glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True)
        )
    files = sorted(files)

    def proj_of(f):
        rel = os.path.relpath(f, PROJECTS)
        return (
            os.path.basename(os.path.dirname(f)) if rel.startswith("..") else rel.split(os.sep)[0]
        )

    if a.project:
        files = [f for f in files if any(x in proj_of(f) for x in a.project)]

    live = live_sessions()
    backup = os.path.join(
        a.backup_dir or default_backup_root(),
        f"claude-transcripts-{datetime.datetime.now():%Y%m%d-%H%M%S}",
    )
    now = datetime.datetime.now().timestamp()
    rows = {}  # project -> [sessions_with_images, blocks, image_bytes, system_bytes, skipped, total]
    errors = 0
    try:
        for f in files:
            try:
                st = os.stat(f)
            except OSError:
                continue  # vanished while scanning
            r = rows.setdefault(proj_of(f), [0, 0, 0, 0, 0, 0])
            r[5] += st.st_size
            if session_id(f) in live or now - st.st_mtime < a.min_age * 60:
                r[4] += st.st_size
                continue
            try:
                b, n, c, img, sysd = process(f, a.apply, backup, a.clean)
            except Changed as e:
                print(f"SKIPPED {f}: {e}", file=sys.stderr)
                r[4] += st.st_size
                continue
            except Exception as e:  # noqa: BLE001 -- one bad file must not stop the run;
                # the transcript is replaced only after validation, so it is intact here
                print(f"ERROR {f}: {e}", file=sys.stderr)
                errors += 1
                continue
            if b:
                r[0] += bool(c)
                r[1] += c
                r[2] += img
                r[3] += sysd
                if a.verbose and b - n > 5_000_000:
                    print(
                        f"{'cleaned' if a.apply else 'would clean'} {b / 1e6:8.1f} -> {n / 1e6:7.1f} MB  "
                        f"{os.path.relpath(f, PROJECTS)}"
                    )
    except KeyboardInterrupt:
        if a.apply and os.path.isdir(backup):
            print(
                f"\ninterrupted -- originals of files stripped so far are in: {backup}",
                file=sys.stderr,
            )
        sys.exit(130)

    mb = lambda x: f"{x / 1e6:,.0f}".replace(",", " ")
    verb = "freed" if a.apply else "savable"
    shown = (  # under --apply, a kind that was not selected shows "-"
        a.clean in ("images", "all") or not a.apply,
        a.clean in ("system-data", "all") or not a.apply,
    )
    col = lambda x, on: mb(x) if on else "-"
    print(
        f"| project | total MB | sessions with images | image blocks | images {verb} MB "
        f"| system data {verb} MB | skipped (live/recent) MB |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|")
    tot = [0] * 6
    for p, r in sorted(rows.items(), key=lambda kv: -(kv[1][2] + kv[1][3] + kv[1][4])):
        tot = [x + y for x, y in zip(tot, r)]
        if r[1] or r[2] or r[3] or r[4] > 1_000_000:
            print(
                f"| {project_label(p)} | {mb(r[5])} | {r[0]} | {r[1]} | {col(r[2], shown[0])} "
                f"| {col(r[3], shown[1])} | {mb(r[4])} |"
            )
    print(
        f"| **total** | {mb(tot[5])} | {tot[0]} | {tot[1]} | {col(tot[2], shown[0])} "
        f"| {col(tot[3], shown[1])} | {mb(tot[4])} |"
    )
    if a.apply and (tot[2] or tot[3]):
        print(f"\noriginals kept in: {backup}")
    elif not a.apply and (tot[2] or tot[3]):
        print(
            "\ndry run -- nothing changed; re-run with --apply to strip images, "
            "--apply --clean system-data for system data, or --apply --clean all for both"
        )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
