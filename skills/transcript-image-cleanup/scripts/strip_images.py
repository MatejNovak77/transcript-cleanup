#!/usr/bin/env python3
"""Map and strip embedded base64 images/PDFs from Claude Code session transcripts.

Claude Code stores every image it reads (Read tool, screenshots, pastes) inside
the session JSONL as base64 -- twice: once in `message.content` (what the model
sees) and once in `toolUseResult.file.base64` (for the UI), plus copies in
`api-request-blob` records. Projects that read many images grow to gigabytes.

This script replaces each such block with a short text placeholder naming the
original file (taken from the matching Read tool call) and its size, and shrinks
the UI duplicate to a 1x1 PNG. Every record keeps its uuid, timestamp and order;
the file's mtime is preserved, so `claude --resume` keeps working and listing
order does not change.

Safety:
  * dry-run report by default; nothing is written without --apply
  * sessions of running `claude` processes and files modified in the last
    --min-age minutes are skipped
  * originals are moved to a backup directory (default: the macOS Trash, or
    ~/.local/share/transcript-image-cleanup/backups elsewhere); undo = move back
  * each rewritten file is validated (same line count, same record uuids/types,
    every line parses) before the atomic swap

Usage:
  strip_images.py [--apply] [--project NAME ...] [--min-age 30] [-v] [PATH ...]

PATH is a .jsonl file or a directory; default is the projects directory under
$CLAUDE_CONFIG_DIR (or ~/.claude). Requires Python 3.8+, no dependencies.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import shutil
import sys

HOME = os.path.expanduser('~')
CONFIG_DIR = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(HOME, '.claude')
PROJECTS = os.path.join(CONFIG_DIR, 'projects')
TINY_PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')
STAMP = datetime.date.today().isoformat()
MIN_B64 = 1000  # base64 strings shorter than this are left alone


def default_backup_root():
    trash = os.path.join(HOME, '.Trash')
    if sys.platform == 'darwin' and os.path.isdir(trash):
        return trash
    return os.path.join(HOME, '.local', 'share', 'transcript-image-cleanup', 'backups')


def live_sessions():
    """Session ids of running claude processes (Claude Code's pid registry)."""
    ids = set()
    for f in glob.glob(os.path.join(CONFIG_DIR, 'sessions', '*.json')):
        try:
            with open(f) as h:
                d = json.load(h)
            os.kill(int(d['pid']), 0)
            ids.add(d['sessionId'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ids


def session_id(f):
    """<sid>.jsonl -> sid; <sid>/subagents/agent-*.jsonl -> sid."""
    parent = os.path.dirname(f)
    if os.path.basename(parent) == 'subagents':
        return os.path.basename(os.path.dirname(parent))
    return os.path.basename(f)[:-len('.jsonl')]


class Stripper:
    def __init__(self, path_for):
        self.path_for = path_for  # tool_use id -> file path of the Read call
        self.count = 0
        self.removed = 0

    def placeholder(self, block, tool_id):
        src = block['source']
        data = src.get('data', '')
        self.count += 1
        self.removed += len(data)
        what = self.path_for.get(tool_id) or 'inline image (screenshot/paste)'
        sha = hashlib.sha256(data.encode()).hexdigest()[:12]
        return {'type': 'text',
                'text': f'[{block["type"]} stripped {STAMP}: {what}; '
                        f'{src.get("media_type", "?")}, {len(data) * 3 // 4} B, sha256:{sha}]'}

    def walk(self, obj, tool_id=None):
        if isinstance(obj, list):
            return [self.placeholder(x, tool_id) if is_b64_block(x) else self.walk(x, tool_id)
                    for x in obj]
        if isinstance(obj, dict):
            tid = obj.get('tool_use_id', tool_id)
            out = {}
            for k, v in obj.items():
                if k == 'base64' and isinstance(v, str) and len(v) > MIN_B64:
                    out[k] = TINY_PNG
                    self.removed += len(v)
                else:
                    out[k] = self.walk(v, tid)
            return out
        return obj


def is_b64_block(x):
    return (isinstance(x, dict) and x.get('type') in ('image', 'document')
            and isinstance(x.get('source'), dict) and x['source'].get('type') == 'base64'
            and len(x['source'].get('data', '')) > MIN_B64)


def tool_paths(records):
    """Map tool_use id -> input path, so placeholders can name the source file."""
    m = {}
    for o in records:
        msg = o.get('message') if isinstance(o, dict) else None
        content = msg.get('content') if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get('type') == 'tool_use':
                inp = b.get('input') if isinstance(b.get('input'), dict) else {}
                p = inp.get('file_path') or inp.get('path') or inp.get('imagePath')
                if p:
                    m[b.get('id')] = p
    return m


def process(f, apply, backup_root):
    """Return (bytes_before, bytes_after, blocks_replaced); 0s if nothing to do."""
    with open(f, 'rb') as h:
        raw = h.read().splitlines(keepends=True)
    if not any(b'"base64"' in line for line in raw):
        return 0, 0, 0
    parsed = [json.loads(line) if line.strip() else None for line in raw]
    s = Stripper(tool_paths([p for p in parsed if p]))
    out = []
    for line, o in zip(raw, parsed):
        if o is None or b'"base64"' not in line:
            out.append(line)
            continue
        before = s.removed
        n = s.walk(o)
        if s.removed > before:
            out.append((json.dumps(n, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
        else:
            out.append(line)
    if not s.removed:
        return 0, 0, 0

    if len(out) != len(raw):
        raise RuntimeError('line count changed')
    for a, b in zip(parsed, out):
        if a is not None:
            nb = json.loads(b)
            if nb.get('uuid') != a.get('uuid') or nb.get('type') != a.get('type'):
                raise RuntimeError('record identity changed')

    new = b''.join(out)
    if apply:
        st = os.stat(f)
        tmp = f + '.strip-tmp'
        with open(tmp, 'wb') as h:
            h.write(new)
            h.flush()
            os.fsync(h.fileno())
        os.chmod(tmp, st.st_mode & 0o777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        rel = os.path.relpath(f, PROJECTS)
        if rel.startswith('..'):
            rel = os.path.basename(f)
        dest = os.path.join(backup_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(f, dest)  # rename on the same volume: instant, no extra space
        os.replace(tmp, f)
    return sum(map(len, raw)), len(new), s.count


def project_label(name):
    home = HOME.replace(os.sep, '-').replace('.', '-') + '-'
    return name[len(home):] if name.startswith(home) else name


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('paths', nargs='*', default=[PROJECTS],
                    help='.jsonl files or directories (default: %(default)s)')
    ap.add_argument('--apply', action='store_true', help='rewrite files (default: dry-run report)')
    ap.add_argument('--project', action='append', default=[], metavar='NAME',
                    help='only project dirs whose name contains NAME (repeatable)')
    ap.add_argument('--min-age', type=float, default=30, metavar='MIN',
                    help='skip files modified less than MIN minutes ago (default: 30)')
    ap.add_argument('--backup-dir', default=None,
                    help='where originals go (default: macOS Trash or ~/.local/share/...)')
    ap.add_argument('-v', '--verbose', action='store_true', help='list files saving more than 5 MB')
    a = ap.parse_args()

    files = []
    for p in a.paths:
        p = os.path.expanduser(p)
        files += [p] if p.endswith('.jsonl') else glob.glob(os.path.join(p, '**', '*.jsonl'), recursive=True)

    def proj_of(f):
        rel = os.path.relpath(f, PROJECTS)
        return os.path.basename(os.path.dirname(f)) if rel.startswith('..') else rel.split(os.sep)[0]

    if a.project:
        files = [f for f in files if any(x in proj_of(f) for x in a.project)]

    live = live_sessions()
    backup = os.path.join(a.backup_dir or default_backup_root(),
                          f'claude-transcripts-{datetime.datetime.now():%Y%m%d-%H%M%S}')
    now = datetime.datetime.now().timestamp()
    rows = {}  # project -> [sessions_with_images, blocks, before, after, skipped, total]
    errors = 0
    for f in sorted(files):
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
            b, n, c = process(f, a.apply, backup)
        except Exception as e:  # files are swapped only after validation
            print(f'ERROR {f}: {e}', file=sys.stderr)
            errors += 1
            continue
        if b:
            r[0] += 1
            r[1] += c
            r[2] += b
            r[3] += n
            if a.verbose and b - n > 5_000_000:
                print(f'{"stripped" if a.apply else "would strip"} {b / 1e6:8.1f} -> {n / 1e6:7.1f} MB  '
                      f'{os.path.relpath(f, PROJECTS)}')

    mb = lambda x: f'{x / 1e6:,.0f}'.replace(',', ' ')
    verb = 'freed' if a.apply else 'savable'
    print(f'| project | total MB | sessions with images | image blocks | {verb} MB | skipped (live/recent) MB |')
    print('|---|---:|---:|---:|---:|---:|')
    tot = [0] * 6
    for p, r in sorted(rows.items(), key=lambda kv: -(kv[1][2] - kv[1][3] + kv[1][4])):
        tot = [x + y for x, y in zip(tot, r)]
        if r[1] or r[2] or r[4] > 1_000_000:
            print(f'| {project_label(p)} | {mb(r[5])} | {r[0]} | {r[1]} | {mb(r[2] - r[3])} | {mb(r[4])} |')
    print(f'| **total** | {mb(tot[5])} | {tot[0]} | {tot[1]} | {mb(tot[2] - tot[3])} | {mb(tot[4])} |')
    if a.apply and tot[2]:
        print(f'\noriginals moved to: {backup}')
    elif not a.apply and tot[2]:
        print('\ndry run -- nothing changed; re-run with --apply to strip')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
