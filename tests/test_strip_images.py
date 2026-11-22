"""Tests of strip_images.py on synthetic transcripts (no real ~/.claude is touched)."""

import base64
import errno
import hashlib
import json
import os
import re
import sys
import time
import uuid

import pytest
import strip_images

PLACEHOLDER_RE = re.compile(
    r"^\[(image|document) stripped \d{4}-\d\d-\d\d: .+; [^,]+, \d+ B, sha256:[0-9a-f]{12}\]$"
)


# ---------------------------------------------------------------- fixture builders


def b64(raw=None, n=3000):
    raw = os.urandom(n) if raw is None else raw
    return base64.b64encode(raw).decode()


def rec(type_, content, **extra):
    r = {
        "type": type_,
        "uuid": str(uuid.uuid4()),
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": type_, "content": content},
    }
    r.update(extra)
    return r


def img_block(data, kind="image", media="image/png"):
    return {"type": kind, "source": {"type": "base64", "media_type": media, "data": data}}


def read_image(tool_id, path, data):
    """Read tool call on an image: assistant tool_use + user tool_result with the UI copy."""
    return [
        rec(
            "assistant",
            [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": path}}],
        ),
        rec(
            "user",
            [{"type": "tool_result", "tool_use_id": tool_id, "content": [img_block(data)]}],
            toolUseResult={"type": "image", "file": {"base64": data, "type": "image/png"}},
        ),
    ]


def pasted_image(data):
    return [rec("user", [{"type": "text", "text": "look at this"}, img_block(data)])]


def pdf_shape_a(tool_id, path, data):
    """Older Read-PDF shape: tool_result is a string, the document follows in an isMeta record."""
    call = rec(
        "assistant",
        [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": path}}],
    )
    result = rec(
        "user",
        [{"type": "tool_result", "tool_use_id": tool_id, "content": "PDF file read: " + path}],
        toolUseResult={
            "type": "pdf",
            "file": {"filePath": path, "base64": data, "originalSize": 1},
        },
    )
    meta = rec(
        "user",
        [img_block(data, "document", "application/pdf")],
        isMeta=True,
        parentUuid=result["uuid"],
    )
    return [call, result, meta]


def pdf_shape_b(tool_id, path, data):
    """Newer Read-PDF shape: the document sits inside tool_result.content."""
    return [
        rec(
            "assistant",
            [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": path}}],
        ),
        rec(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": [
                        {"type": "text", "text": "PDF file read"},
                        img_block(data, "document", "application/pdf"),
                    ],
                }
            ],
        ),
    ]


def write_session(path, records, age_min=120):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(r) + "\n" for r in records).encode())
    t = time.time() - age_min * 60
    os.utime(path, (t, t))
    return path


def lines(path):
    return [json.loads(x) for x in path.read_bytes().decode("utf-8").splitlines()]


def texts(obj):
    """All 'text' values anywhere in a record."""
    if isinstance(obj, list):
        return [t for x in obj for t in texts(x)]
    if isinstance(obj, dict):
        own = (
            [obj["text"]] if obj.get("type") == "text" and isinstance(obj.get("text"), str) else []
        )
        return own + [t for v in obj.values() for t in texts(v)]
    return []


def placeholders(path):
    return [t for r in lines(path) for t in texts(r) if " stripped " in t and t.startswith("[")]


def snapshot(root):
    return {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def backups(env):
    return [p for p in env.backup.rglob("*") if p.is_file()] if env.backup.exists() else []


def run(monkeypatch, capsys, *args):
    """Run main() with argv; return (exit code, stdout, stderr). Ctrl-C escaping main is reported."""
    monkeypatch.setattr(sys, "argv", ["strip_images.py", *[str(a) for a in args]])
    try:
        strip_images.main()
        code = 0
    except SystemExit as e:
        code = e.code
    except KeyboardInterrupt:
        code = "KeyboardInterrupt escaped main"
    out = capsys.readouterr()
    return code, out.out, out.err


def apply(monkeypatch, capsys, env, *args):
    return run(monkeypatch, capsys, "--apply", "--backup-dir", env.backup, *args)


@pytest.fixture
def session(env):
    data = b64()
    f = write_session(env.projects / "-proj" / "s1.jsonl", read_image("t1", "/x/pic.png", data))
    return f


# ---------------------------------------------------------------- normal stripping


def test_strip_read_image_names_path(monkeypatch, capsys, env, session):
    code, _, _ = apply(monkeypatch, capsys, env)
    assert code == 0
    ph = placeholders(session)
    assert len(ph) == 1 and ": /x/pic.png; image/png, " in ph[0]


def test_ui_copy_becomes_tiny_png(monkeypatch, capsys, env, session):
    apply(monkeypatch, capsys, env)
    assert lines(session)[1]["toolUseResult"]["file"]["base64"] == strip_images.TINY_PNG


def test_strip_pasted_image_inline_label(monkeypatch, capsys, env):
    f = write_session(env.projects / "-proj" / "s1.jsonl", pasted_image(b64()))
    apply(monkeypatch, capsys, env)
    assert placeholders(f) == [p for p in placeholders(f) if "inline image (screenshot/paste)" in p]
    assert len(placeholders(f)) == 1


def test_strip_pdf_shape_b_names_path(monkeypatch, capsys, env):
    f = write_session(env.projects / "-proj" / "s1.jsonl", pdf_shape_b("t1", "/x/doc.pdf", b64()))
    apply(monkeypatch, capsys, env)
    ph = placeholders(f)
    assert len(ph) == 1 and ph[0].startswith("[document stripped") and "/x/doc.pdf" in ph[0]


def test_strip_pdf_shape_a_labelled_document(monkeypatch, capsys, env):
    f = write_session(env.projects / "-proj" / "s1.jsonl", pdf_shape_a("t1", "/x/doc.pdf", b64()))
    apply(monkeypatch, capsys, env)
    ph = placeholders(f)
    assert len(ph) == 1
    assert "screenshot" not in ph[0] and "inline document" in ph[0]
    assert lines(f)[1]["toolUseResult"]["file"]["base64"] == strip_images.TINY_PNG


def test_uuids_order_mtime_preserved(monkeypatch, capsys, env, session):
    before = [(r["uuid"], r["type"]) for r in lines(session)]
    mtime = session.stat().st_mtime_ns
    apply(monkeypatch, capsys, env)
    assert [(r["uuid"], r["type"]) for r in lines(session)] == before
    assert session.stat().st_mtime_ns == mtime


def test_placeholder_format_regex(monkeypatch, capsys, env):
    recs = (
        read_image("t1", "/x/a.png", b64())
        + pasted_image(b64())
        + pdf_shape_a("t2", "/x/a.pdf", b64())
        + pdf_shape_b("t3", "/x/b.pdf", b64())
    )
    f = write_session(env.projects / "-proj" / "s1.jsonl", recs)
    apply(monkeypatch, capsys, env)
    ph = placeholders(f)
    assert len(ph) == 4
    assert all(PLACEHOLDER_RE.match(p) for p in ph), ph


def test_placeholder_hash_matches_file_bytes(monkeypatch, capsys, env):
    raw = os.urandom(3001)
    f = write_session(env.projects / "-proj" / "s1.jsonl", read_image("t1", "/x/p.png", b64(raw)))
    apply(monkeypatch, capsys, env)
    (ph,) = placeholders(f)
    assert f", {len(raw)} B, sha256:{hashlib.sha256(raw).hexdigest()[:12]}]" in ph


def test_invalid_base64_does_not_fail_file(monkeypatch, capsys, env):
    f = write_session(env.projects / "-proj" / "s1.jsonl", pasted_image("@" * 2000))
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 0 and "ERROR" not in err
    (ph,) = placeholders(f)
    assert PLACEHOLDER_RE.match(ph) and ", 1500 B, " in ph


def test_already_stripped_is_noop(monkeypatch, capsys, env, session):
    apply(monkeypatch, capsys, env)
    stripped = session.read_bytes()
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 0 and not err
    assert session.read_bytes() == stripped


# ---------------------------------------------------------------- what is not touched


def test_dry_run_writes_nothing(monkeypatch, capsys, env, session):
    before = snapshot(env.root)
    code, out, _ = run(monkeypatch, capsys, "--backup-dir", env.backup)
    assert code == 0 and "dry run" in out
    assert snapshot(env.root) == before
    assert not env.backup.exists()


def test_live_session_skipped(monkeypatch, capsys, env, session):
    (env.cfg / "sessions").mkdir()
    (env.cfg / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "s1"})
    )
    before = session.read_bytes()
    apply(monkeypatch, capsys, env)
    assert session.read_bytes() == before


def test_dead_pid_not_live(monkeypatch, capsys, env, session):
    (env.cfg / "sessions").mkdir()
    (env.cfg / "sessions" / "1.json").write_text(json.dumps({"pid": 999999, "sessionId": "s1"}))

    def dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(strip_images.os, "kill", dead)
    apply(monkeypatch, capsys, env)
    assert len(placeholders(session)) == 1


def test_recent_file_skipped(monkeypatch, capsys, env):
    f = write_session(env.projects / "-proj" / "s1.jsonl", pasted_image(b64()), age_min=1)
    before = f.read_bytes()
    apply(monkeypatch, capsys, env)
    assert f.read_bytes() == before


def test_windows_refused_before_signal(monkeypatch, capsys, env, session):
    (env.cfg / "sessions").mkdir()
    (env.cfg / "sessions" / "1.json").write_text(json.dumps({"pid": 4, "sessionId": "x"}))
    calls = []
    monkeypatch.setattr(strip_images.os, "kill", lambda *a: calls.append(a))
    monkeypatch.setattr(strip_images.sys, "platform", "win32")
    before = snapshot(env.root)
    code, _, _ = apply(monkeypatch, capsys, env)
    assert isinstance(code, str) and "Windows" in code
    assert calls == []
    assert snapshot(env.root) == before


# ---------------------------------------------------------------- failure safety


def is_whole(f, original):
    """The transcript is either the untouched original or a complete stripped version."""
    if f.read_bytes() == original:
        return True
    new = lines(f)
    old = [json.loads(x) for x in original.decode().splitlines()]
    return [r["uuid"] for r in new] == [r["uuid"] for r in old] and len(placeholders(f)) >= 1


def no_tmp(env):
    return list(env.root.rglob("*.strip-tmp")) == []


def test_same_basename_outside_projects_both_backed_up(monkeypatch, capsys, env):
    a = write_session(env.root / "a" / "s.jsonl", pasted_image(b64()))
    b = write_session(env.root / "b" / "s.jsonl", pasted_image(b64()))
    originals = {a.read_bytes(), b.read_bytes()}
    code, _, _ = apply(monkeypatch, capsys, env, a, b)
    assert code == 0
    assert {p.read_bytes() for p in backups(env)} == originals


def test_backup_dir_unwritable_leaves_original_and_no_tmp(monkeypatch, capsys, env, session):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    before = session.read_bytes()
    env.backup.mkdir()
    env.backup.chmod(0o500)
    try:
        code, _, err = apply(monkeypatch, capsys, env)
    finally:
        env.backup.chmod(0o700)
    assert code == 1 and "ERROR" in err
    assert session.read_bytes() == before
    assert no_tmp(env)


def _boom(*a, **k):
    raise KeyboardInterrupt


@pytest.mark.parametrize("point", ["fsync", "backup", "replace"])
def test_interrupt_at_each_step_keeps_file_whole(monkeypatch, capsys, env, session, point):
    before = session.read_bytes()
    if point == "fsync":
        monkeypatch.setattr(strip_images.os, "fsync", _boom)
    elif point == "backup":
        monkeypatch.setattr(strip_images.os, "link", _boom)
    else:
        monkeypatch.setattr(strip_images.os, "replace", _boom)
    apply(monkeypatch, capsys, env)
    assert session.exists() and is_whole(session, before)
    assert no_tmp(env)
    for p in backups(env):
        assert p.read_bytes() == before


def test_existing_backup_never_overwritten(env, session):
    root = env.backup / "run"
    dest = root / "-proj" / "s1.jsonl"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"precious")
    before = session.read_bytes()
    with pytest.raises(FileExistsError):
        strip_images.process(str(session), True, str(root))
    assert dest.read_bytes() == b"precious"
    assert session.read_bytes() == before
    assert no_tmp(env)


def test_cross_device_falls_back_to_copy(monkeypatch, capsys, env, session):
    before = session.read_bytes()

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(strip_images.os, "link", exdev)
    code, _, _ = apply(monkeypatch, capsys, env)
    assert code == 0
    (bk,) = backups(env)
    assert bk.read_bytes() == before and bk.stat().st_nlink == 1
    assert len(placeholders(session)) == 1


@pytest.mark.parametrize(
    "exc", [KeyboardInterrupt, lambda: OSError(errno.ENOSPC, "No space left on device")]
)
def test_copy_fallback_interrupted_leaves_no_backup_or_partial(
    monkeypatch, capsys, env, session, exc
):
    before = session.read_bytes()

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    def half_copy(src, dst, *a, **k):
        dst.write(src.read(100))
        raise exc()

    monkeypatch.setattr(strip_images.os, "link", exdev)
    monkeypatch.setattr(strip_images.shutil, "copyfileobj", half_copy)
    apply(monkeypatch, capsys, env)
    assert session.read_bytes() == before
    assert backups(env) == []
    assert no_tmp(env)


def test_copy_fallback_close_failure_leaves_no_backup(monkeypatch, capsys, env, session):
    """Disk full can surface only when the copy is closed (buffered flush)."""
    before = session.read_bytes()
    real_open = open

    class FailingClose:
        def __init__(self, h):
            self.h = h

        def __getattr__(self, name):
            return getattr(self.h, name)

        def close(self):
            self.h.close()
            raise OSError(errno.ENOSPC, "No space left on device")

    def fake_open(path, mode="r", *a, **k):
        h = real_open(path, mode, *a, **k)
        return FailingClose(h) if mode == "xb" else h

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(strip_images.os, "link", exdev)
    monkeypatch.setattr(strip_images, "open", fake_open, raising=False)
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 1 and "ERROR" in err
    assert session.read_bytes() == before
    assert backups(env) == []
    assert no_tmp(env)


def test_lone_surrogate_record_strips(monkeypatch, capsys, env):
    recs = [rec("user", [{"type": "text", "text": "half \ud83d emoji"}, img_block(b64())])]
    f = write_session(env.projects / "-proj" / "s1.jsonl", recs)
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 0 and "ERROR" not in err
    raw = f.read_bytes()
    raw.decode("utf-8")  # strict: no surrogate written through
    assert "half \ud83d emoji" in texts(lines(f)[0])
    assert len(placeholders(f)) == 1


def test_append_during_processing_skips(monkeypatch, capsys, env, session):
    before = session.read_bytes()
    extra = json.dumps(rec("user", [{"type": "text", "text": "late"}])) + "\n"
    real = strip_images.tool_paths

    def appending(records):
        with open(session, "a") as h:
            h.write(extra)
        return real(records)

    monkeypatch.setattr(strip_images, "tool_paths", appending)
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 0 and "SKIPPED" in err
    assert session.read_bytes() == before + extra.encode()
    assert backups(env) == []
    assert no_tmp(env)


def test_duplicate_path_args_harmless(monkeypatch, capsys, env, session):
    code, _, err = apply(monkeypatch, capsys, env, session, session)
    assert code == 0 and "ERROR" not in err
    assert len(backups(env)) == 1


@pytest.mark.parametrize("when", ["second_file", "right_after_replace"])
def test_ctrl_c_prints_backup_dir_exit_130(monkeypatch, capsys, env, session, when):
    before = session.read_bytes()
    write_session(env.projects / "-proj" / "s2.jsonl", pasted_image(b64()))
    real_replace = os.replace
    real_process = strip_images.process
    calls = []

    if when == "second_file":

        def process(f, *a):
            calls.append(f)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_process(f, *a)

        monkeypatch.setattr(strip_images, "process", process)
    else:

        def replace(src, dst):
            real_replace(src, dst)
            raise KeyboardInterrupt

        monkeypatch.setattr(strip_images.os, "replace", replace)
    code, out, err = apply(monkeypatch, capsys, env)
    assert code == 130
    assert str(env.backup) in out + err
    assert len(placeholders(session)) == 1
    assert [p.read_bytes() for p in backups(env)] == [before]


def test_validation_identity_guard(monkeypatch, capsys, env, session):
    before = session.read_bytes()
    real_walk = strip_images.Stripper.walk

    def walk(self, obj, *a, **k):
        out = real_walk(self, obj, *a, **k)
        if isinstance(out, dict) and "uuid" in out:
            out = dict(out, uuid="changed")
        return out

    monkeypatch.setattr(strip_images.Stripper, "walk", walk)
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 1 and "ERROR" in err
    assert session.read_bytes() == before
    assert backups(env) == [] and no_tmp(env)
