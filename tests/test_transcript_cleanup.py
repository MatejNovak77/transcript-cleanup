"""Tests of transcript_cleanup.py on synthetic transcripts (no real ~/.claude is touched)."""

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
import transcript_cleanup

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
    monkeypatch.setattr(sys, "argv", ["transcript_cleanup.py", *[str(a) for a in args]])
    try:
        transcript_cleanup.main()
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
    assert lines(session)[1]["toolUseResult"]["file"]["base64"] == transcript_cleanup.TINY_PNG


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
    assert lines(f)[1]["toolUseResult"]["file"]["base64"] == transcript_cleanup.TINY_PNG


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

    monkeypatch.setattr(transcript_cleanup.os, "kill", dead)
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
    monkeypatch.setattr(transcript_cleanup.os, "kill", lambda *a: calls.append(a))
    monkeypatch.setattr(transcript_cleanup.sys, "platform", "win32")
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
        monkeypatch.setattr(transcript_cleanup.os, "fsync", _boom)
    elif point == "backup":
        monkeypatch.setattr(transcript_cleanup.os, "link", _boom)
    else:
        monkeypatch.setattr(transcript_cleanup.os, "replace", _boom)
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
        transcript_cleanup.process(str(session), True, str(root))
    assert dest.read_bytes() == b"precious"
    assert session.read_bytes() == before
    assert no_tmp(env)


def test_cross_device_falls_back_to_copy(monkeypatch, capsys, env, session):
    before = session.read_bytes()

    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(transcript_cleanup.os, "link", exdev)
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

    monkeypatch.setattr(transcript_cleanup.os, "link", exdev)
    monkeypatch.setattr(transcript_cleanup.shutil, "copyfileobj", half_copy)
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

    monkeypatch.setattr(transcript_cleanup.os, "link", exdev)
    monkeypatch.setattr(transcript_cleanup, "open", fake_open, raising=False)
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
    real = transcript_cleanup.tool_paths

    def appending(records):
        with open(session, "a") as h:
            h.write(extra)
        return real(records)

    monkeypatch.setattr(transcript_cleanup, "tool_paths", appending)
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
    real_process = transcript_cleanup.process
    calls = []

    if when == "second_file":

        def process(f, *a):
            calls.append(f)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_process(f, *a)

        monkeypatch.setattr(transcript_cleanup, "process", process)
    else:

        def replace(src, dst):
            real_replace(src, dst)
            raise KeyboardInterrupt

        monkeypatch.setattr(transcript_cleanup.os, "replace", replace)
    code, out, err = apply(monkeypatch, capsys, env)
    assert code == 130
    assert str(env.backup) in out + err
    assert len(placeholders(session)) == 1
    assert [p.read_bytes() for p in backups(env)] == [before]


def test_validation_identity_guard(monkeypatch, capsys, env, session):
    before = session.read_bytes()
    real_walk = transcript_cleanup.Stripper.walk

    def walk(self, obj, *a, **k):
        out = real_walk(self, obj, *a, **k)
        if isinstance(out, dict) and "uuid" in out:
            out = dict(out, uuid="changed")
        return out

    monkeypatch.setattr(transcript_cleanup.Stripper, "walk", walk)
    code, _, err = apply(monkeypatch, capsys, env)
    assert code == 1 and "ERROR" in err
    assert session.read_bytes() == before
    assert backups(env) == [] and no_tmp(env)


# ---------------------------------------------------------------- system data
# Record shapes follow Claude Code 2.1.330 (key sets measured on real transcripts; content synthetic).

SKILLS_TEXT = "- alpha-report: Use this skill to format weekly reports. " * 20


def compact(path, records, age_min=120):
    """Write records the way Claude Code does: compact JSON, raw UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            for r in records
        )
    )
    t = time.time() - age_min * 60
    os.utime(path, (t, t))
    return path


def attachment(att, rendered=None, **extra):
    r = {
        "parentUuid": str(uuid.uuid4()),
        "isSidechain": False,
        "attachment": att,
        "type": "attachment",
        "uuid": str(uuid.uuid4()),
        "timestamp": "2026-01-01T00:00:00.000Z",
    }
    if rendered is not None:
        r["rendered"] = rendered
    r.update(extra)
    return r


def skill_listing(content=SKILLS_TEXT, rendered=True):
    rend = [{"content": "The following skills are available:\n\n" + content}] if rendered else None
    att = {"type": "skill_listing", "content": content, "skillCount": 1, "isInitial": True}
    return attachment(att, rend)


def prompt_snapshot():
    att = {
        "type": "prompt_snapshot",
        "systemPrompt": ["You are a helpful agent. " * 40],
        "tools": [{"name": "Read", "description": "Reads a file. " * 40}],
    }
    return attachment(att)


def instructions():
    text = "# Project notes\n" + "Keep functions small. " * 40
    att = {
        "type": "instructions",
        "files": [{"path": "/p/CLAUDE.md", "type": "Project", "content": text}],
    }
    return attachment(att, [{"content": "Contents of /p/CLAUDE.md:\n" + text}])


def api_request():
    return {
        "type": "api-request",
        "id": "req_" + uuid.uuid4().hex,
        "shapeHash": "s" * 16,
        "tail": ["h" * 16],
        "params": {"betas": ["x"] * 50, "safeguards": "y" * 2000},
        "sessionId": "s1",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "version": "2.1.330",
    }


def api_blob():
    return {
        "type": "api-request-blob",
        "hash": "h" * 16,
        "message": {"role": "user", "content": [{"type": "text", "text": "z" * 3000}]},
        "sessionId": "s1",
    }


def api_shape():
    return {
        "type": "api-request-shape",
        "shapeHash": "s" * 16,
        "shape": {"tools": [{"name": "Read", "description": "d" * 3000}], "system": ["sys"]},
        "sessionId": "s1",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "version": "2.1.330",
    }


def conversation():
    return [
        rec("user", [{"type": "text", "text": "Remember 4711, café ✓"}]),
        rec(
            "assistant",
            [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "ls"}}],
            requestRef="req_x",
        ),
        rec("user", [{"type": "tool_result", "tool_use_id": "t9", "content": "a\nb"}]),
        rec("assistant", [{"type": "text", "text": "Done: 4711."}]),
    ]


def system_session(f, **kw):
    conv = conversation()
    recs = [skill_listing(), prompt_snapshot(), instructions(), conv[0], api_shape(), api_blob()]
    recs += [api_request(), conv[1], conv[2], api_request(), api_blob(), conv[3]]
    return compact(f, recs, **kw)


def raw_lines(path):
    return path.read_bytes().splitlines(keepends=True)


def kind(line):
    o = json.loads(line)
    a = o.get("attachment")
    return o["type"] + (":" + a["type"] if isinstance(a, dict) else "")


def test_system_only_transcript_cleaned(monkeypatch, capsys, env):
    f = system_session(env.projects / "-proj" / "s1.jsonl")
    before, st = f.read_bytes(), f.stat()
    code, out, err = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 0 and "ERROR" not in err
    old, new = before.splitlines(keepends=True), raw_lines(f)
    kept_old = [x for x in old if not kind(x).startswith("api-request")]
    assert len(new) == len(kept_old) == len(old) - 5
    for a, b in zip(kept_old, new):  # every other line byte-identical and in order
        if kind(a) == "attachment:skill_listing":
            oa, nb = json.loads(a), json.loads(b)
            assert nb["attachment"]["content"] == "" and nb["rendered"] == oa["rendered"]
            assert dict(nb, attachment=None) == dict(oa, attachment=None)
        else:
            assert a == b
    assert [json.loads(x).get("uuid") for x in new] == [
        json.loads(x).get("uuid") for x in old if "uuid" in json.loads(x)
    ]
    (bk,) = backups(env)
    assert bk.read_bytes() == before
    assert f.stat().st_mtime_ns == st.st_mtime_ns
    assert "originals kept in" in out


def test_system_rerun_is_noop(monkeypatch, capsys, env):
    f = system_session(env.projects / "-proj" / "s1.jsonl")
    apply(monkeypatch, capsys, env, "--clean", "system-data")
    after, ino = f.read_bytes(), f.stat().st_ino
    n_backups = len(backups(env))
    code, out, _ = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 0 and f.read_bytes() == after and f.stat().st_ino == ino
    assert len(backups(env)) == n_backups == 1
    assert "originals kept in" not in out


def test_system_record_with_uuid_never_dropped(monkeypatch, capsys, env):
    chained = dict(api_request(), uuid=str(uuid.uuid4()))  # in the uuid chain: must stay
    f = compact(env.projects / "-proj" / "s1.jsonl", [chained, api_blob()])
    apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert [json.loads(x)["type"] for x in raw_lines(f)] == ["api-request"]


@pytest.mark.parametrize(
    "case", ["no_rendered", "not_contained", "rendered_not_list", "rendered_list_of_str", "empty"]
)
def test_skill_listing_needs_rendered_copy(monkeypatch, capsys, env, case):
    r = {
        "no_rendered": skill_listing(rendered=False),
        "not_contained": skill_listing(),
        "rendered_not_list": skill_listing(),
        "rendered_list_of_str": skill_listing(),
        "empty": skill_listing(content=""),
    }[case]
    if case == "not_contained":
        r["rendered"] = [{"content": "a different listing"}]
    if case == "rendered_not_list":
        r["rendered"] = SKILLS_TEXT
    if case == "rendered_list_of_str":  # unknown shape: leave alone
        r["rendered"] = [SKILLS_TEXT]
    f = compact(env.projects / "-proj" / "s1.jsonl", [r])
    before = f.read_bytes()
    code, _, _ = apply(monkeypatch, capsys, env, "--clean", "all")
    assert code == 0 and f.read_bytes() == before and backups(env) == []


def test_kept_kinds_untouched(monkeypatch, capsys, env):
    other = [
        prompt_snapshot(),  # a resume rebuilds the system prompt from it
        instructions(),  # a resume compares it with CLAUDE.md on disk
        attachment({"type": "deferred_tools_delta", "added": ["x" * 3000]}, [{"content": "x"}]),
        rec("progress", "p" * 3000),
        {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {"a": "b" * 3000}}},
        dict(api_request(), type="api-request-foo"),
    ]
    f = compact(env.projects / "-proj" / "s1.jsonl", other)
    before = f.read_bytes()
    code, out, _ = run(monkeypatch, capsys)
    assert code == 0 and "| **total** | 0 | 0 | 0 | 0 | 0 | 0 |" in out
    apply(monkeypatch, capsys, env, "--clean", "all")
    assert f.read_bytes() == before


def test_system_marker_quoted_in_user_text_untouched(monkeypatch, capsys, env):
    quoted = rec(
        "user",
        [
            {
                "type": "text",
                "text": 'see {"type":"api-request","id":"x"} and "type":"skill_listing"',
            }
        ],
    )
    f = compact(env.projects / "-proj" / "s1.jsonl", [quoted, api_blob()])
    apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert raw_lines(f) == [compact(env.root / "q.jsonl", [quoted]).read_bytes()]


def test_system_lone_surrogate_record(monkeypatch, capsys, env):
    r = skill_listing(content="half an emoji \ud83d " + SKILLS_TEXT)
    line = json.dumps(r, separators=(",", ":")).encode() + b"\n"  # \ud83d stays escaped
    f = env.projects / "-proj" / "s1.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes(line + compact(env.root / "b.jsonl", [api_blob()]).read_bytes())
    t = time.time() - 7200
    os.utime(f, (t, t))
    code, _, err = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 0 and "ERROR" not in err
    data = f.read_bytes()
    data.decode("utf-8")  # strictly valid UTF-8
    (o,) = [json.loads(x) for x in data.splitlines()]
    assert o["attachment"]["content"] == "" and "\ud83d" in o["rendered"][0]["content"]


def _corrupt(case):
    """A system_stage replacement that makes one specific mistake."""
    real = transcript_cleanup.system_stage

    def stage(lines):
        out = real(lines)
        recs = [json.loads(x) for x in out]
        i = next(n for n, r in enumerate(recs) if r.get("type") in ("user", "attachment"))
        if case == "user_text_changed":
            j = next(n for n, r in enumerate(recs) if r["type"] == "user")
            recs[j]["message"]["content"][0]["text"] = "Remember 4712"
        elif case == "uuid_line_dropped":
            j = next(n for n, r in enumerate(recs) if r["type"] == "user")
            del recs[j]
        elif case == "other_attachment_field_changed":
            j = next(
                n
                for n, r in enumerate(recs)
                if r.get("attachment", {}).get("type") == "skill_listing"
            )
            recs[j]["attachment"]["skillCount"] = 9
        elif case == "kept_kind_shrunk":
            j = next(
                n
                for n, r in enumerate(recs)
                if r.get("attachment", {}).get("type") == "prompt_snapshot"
            )
            recs[j]["attachment"]["systemPrompt"] = []
        elif case == "rendered_changed":
            j = next(
                n
                for n, r in enumerate(recs)
                if r.get("attachment", {}).get("type") == "skill_listing"
            )
            recs[j]["rendered"] = [{"content": ""}]
        elif case == "extra_line":
            recs.append(recs[i])
        return [transcript_cleanup.encode(r) + b"\n" for r in recs]

    return stage


@pytest.mark.parametrize(
    "case",
    [
        "user_text_changed",
        "uuid_line_dropped",
        "other_attachment_field_changed",
        "kept_kind_shrunk",
        "rendered_changed",
        "extra_line",
    ],
)
def test_check_system_rejects(monkeypatch, capsys, env, case):
    f = system_session(env.projects / "-proj" / "s1.jsonl")
    before = f.read_bytes()
    monkeypatch.setattr(transcript_cleanup, "system_stage", _corrupt(case))
    code, _, err = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 1 and "ERROR" in err and "system-data check" in err
    assert f.read_bytes() == before and backups(env) == [] and no_tmp(env)


@pytest.mark.parametrize("case", ["no_rendered", "not_contained"])
def test_check_system_rejects_listing_without_copy(monkeypatch, capsys, env, case):
    r = skill_listing(rendered=case != "no_rendered")
    if case == "not_contained":
        r["rendered"] = [{"content": "another listing"}]
    f = compact(env.projects / "-proj" / "s1.jsonl", [r])
    before = f.read_bytes()

    def stage(lines):  # empties the only copy
        o = json.loads(lines[0])
        o["attachment"]["content"] = ""
        return [transcript_cleanup.encode(o) + b"\n"]

    monkeypatch.setattr(transcript_cleanup, "system_stage", stage)
    code, _, err = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 1 and "no copy in rendered" in err
    assert f.read_bytes() == before and backups(env) == []


def test_subagent_file_cleaned(monkeypatch, capsys, env):
    f = system_session(env.projects / "-proj" / "s1" / "subagents" / "agent-a1.jsonl")
    apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert not any(kind(x).startswith("api-request") for x in raw_lines(f))


# ---------------------------------------------------------------- --clean and the report


def mixed_session(env):
    recs = read_image("t1", "/x/pic.png", b64()) + [
        json.loads(x) for x in raw_lines(system_session(env.root / "tmp" / "sys.jsonl"))
    ]
    return compact(env.projects / "-proj" / "s1.jsonl", recs)


def test_default_clean_is_images_only(monkeypatch, capsys, env):
    f = mixed_session(env)
    before = raw_lines(f)
    code, out, _ = apply(monkeypatch, capsys, env)
    after = raw_lines(f)
    assert code == 0 and len(placeholders(f)) == 1 and len(after) == len(before)
    for a, b in zip(before, after):  # only the image-carrying line changes
        assert a == b or b'"base64"' in a
    assert sum(a != b for a, b in zip(before, after)) == 1
    row = next(x for x in out.splitlines() if x.startswith("| **total**")).split("|")
    assert row[5].strip() != "-" and row[6].strip() == "-"  # system data column not selected


def test_clean_system_data_keeps_images(monkeypatch, capsys, env):
    f = mixed_session(env)
    code, _, _ = apply(monkeypatch, capsys, env, "--clean", "system-data")
    assert code == 0 and placeholders(f) == []
    assert sum(b'"base64"' in x for x in raw_lines(f)) == 1
    assert not any(kind(x).startswith("api-request") for x in raw_lines(f))


def test_clean_all(monkeypatch, capsys, env):
    f = mixed_session(env)
    code, _, _ = apply(monkeypatch, capsys, env, "--clean", "all")
    assert code == 0 and len(placeholders(f)) == 1
    assert not any(kind(x).startswith("api-request") for x in raw_lines(f))
    assert len(backups(env)) == 1


def test_process_reports_exact_bytes_per_kind(env):
    f = mixed_session(env)
    size0 = f.stat().st_size
    b, n, blocks, img, sysd = transcript_cleanup.process(str(f), False, str(env.backup))
    assert (b, blocks) == (size0, 1) and img > 0 and sysd > 0 and n == b - img - sysd
    transcript_cleanup.process(str(f), True, str(env.backup / "1"), "images")
    assert f.stat().st_size == size0 - img
    transcript_cleanup.process(str(f), True, str(env.backup / "2"), "system-data")
    assert f.stat().st_size == size0 - img - sysd


def test_report_has_both_columns_in_dry_run(monkeypatch, capsys, env):
    mixed_session(env)
    code, out, _ = run(monkeypatch, capsys)
    assert code == 0
    assert re.search(r"\| images savable MB \| system data savable MB \|", out)
    row = next(x for x in out.splitlines() if x.startswith("| **total**")).split("|")
    assert row[4].strip() == "1" and "-" not in row[5] and "-" not in row[6]
    assert "--clean system-data" in out


def test_apply_unselected_column_dash(monkeypatch, capsys, env):
    mixed_session(env)
    _, out, _ = apply(monkeypatch, capsys, env, "--clean", "system-data")
    row = next(x for x in out.splitlines() if x.startswith("| **total**")).split("|")
    assert row[5].strip() == "-" and row[6].strip() != "-"
    assert row[3].strip() == row[4].strip() == "-"  # images were not scanned
    assert "images freed MB" in out


def test_system_only_frees_image_copies_in_blobs(env):
    """Documented attribution: a blob's image counts as images; system data alone frees it too."""
    blob = dict(api_blob(), message={"role": "user", "content": [img_block(b64(n=30000))]})
    f = compact(env.projects / "-proj" / "s1.jsonl", [blob])
    size0 = f.stat().st_size
    _, _, blocks, img, sysd = transcript_cleanup.process(str(f), False, str(env.backup))
    assert blocks == 1 and img > sysd > 0
    _, _, blocks2, img2, sysd2 = transcript_cleanup.process(
        str(f), True, str(env.backup), "system-data"
    )
    assert (blocks2, img2) == (0, 0) and sysd2 == size0 == img + sysd
    assert f.stat().st_size == 0


def test_system_dry_run_writes_nothing(monkeypatch, capsys, env):
    system_session(env.projects / "-proj" / "s1.jsonl")
    before = snapshot(env.root)
    for clean in ("images", "system-data", "all"):
        code, out, _ = run(monkeypatch, capsys, "--clean", clean, "--backup-dir", env.backup)
        assert code == 0 and "dry run" in out
    assert snapshot(env.root) == before and not env.backup.exists()


def test_system_live_and_recent_skipped(monkeypatch, capsys, env):
    live = system_session(env.projects / "-proj" / "s1.jsonl")
    recent = system_session(env.projects / "-proj" / "s2.jsonl", age_min=1)
    (env.cfg / "sessions").mkdir()
    (env.cfg / "sessions" / "1.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "s1"})
    )
    before = (live.read_bytes(), recent.read_bytes())
    apply(monkeypatch, capsys, env, "--clean", "all")
    assert (live.read_bytes(), recent.read_bytes()) == before and backups(env) == []


# ---------------------------------------------------------------- age limits

DAY = 86400


def age(path, days):
    t = time.time() - days * DAY
    os.utime(path, (t, t))
    return path


def big(f, days):
    """Append a 2.5 MB API request blob (so the MB columns are non-zero), then set the age."""
    blob = dict(
        api_blob(), message={"role": "user", "content": [{"type": "text", "text": "z" * 2_500_000}]}
    )
    with open(f, "ab") as h:
        h.write(json.dumps(blob, separators=(",", ":")).encode() + b"\n")
    return age(f, days)


def total_row(out):
    return [
        c.strip()
        for c in next(x for x in out.splitlines() if x.startswith("| **total**")).split("|")
    ]


@pytest.mark.parametrize("days, cleaned", [(89.9, False), (90.1, True)])
def test_age_limit_boundary(monkeypatch, capsys, env, days, cleaned):
    f = age(mixed_session(env), days)
    before = f.read_bytes()
    code, _, _ = apply(monkeypatch, capsys, env, "--clean", "all", "--older-than", "90")
    assert code == 0 and (f.read_bytes() != before) == cleaned


def test_age_limit_per_kind(monkeypatch, capsys, env):
    f = big(mixed_session(env), 30)  # images: any age; system data: only after 90 days
    code, out, _ = apply(monkeypatch, capsys, env, "--clean", "all", "--system-older-than", "90")
    assert code == 0 and len(placeholders(f)) == 1
    assert sum(kind(x).startswith("api-request") for x in raw_lines(f)) == 6
    assert "age limits (last activity): images no limit; system data older than 90 days" in out
    row = total_row(out)
    assert row[8] != "0"  # younger than limit MB: the file kept its system data


def test_per_kind_option_overrides_older_than(monkeypatch, capsys, env):
    f = age(mixed_session(env), 30)
    apply(
        monkeypatch, capsys, env, "--clean", "all", "--older-than", "90", "--images-older-than", "7"
    )
    assert len(placeholders(f)) == 1  # images: 30 > 7 days
    assert sum(kind(x).startswith("api-request") for x in raw_lines(f)) == 5  # system: 30 < 90


def test_no_limit_report_unchanged(monkeypatch, capsys, env):
    big(mixed_session(env), 1)
    _, out, _ = run(monkeypatch, capsys)
    assert "age limits" not in out and "younger than limit" not in out
    header = next(x for x in out.splitlines() if x.startswith("| project"))
    assert header.count("|") == 8
    assert total_row(out)[4] == "1" and total_row(out)[6] != "0"  # both kinds counted


def test_subagent_age_follows_session(monkeypatch, capsys, env):
    main = age(system_session(env.projects / "-proj" / "s1.jsonl"), 5)  # recent activity
    sub = age(system_session(env.projects / "-proj" / "s1" / "subagents" / "agent-a1.jsonl"), 200)
    old = age(system_session(env.projects / "-proj" / "s2.jsonl"), 200)
    snap = {p: p.read_bytes() for p in (main, sub)}
    apply(monkeypatch, capsys, env, "--clean", "system-data", "--older-than", "90")
    assert {p: p.read_bytes() for p in (main, sub)} == snap  # session s1 was active 5 days ago
    assert not any(kind(x).startswith("api-request") for x in raw_lines(old))


def test_live_counted_as_live_not_age(monkeypatch, capsys, env):
    big(system_session(env.projects / "-proj" / "s1.jsonl"), 1 / 1440)  # one minute old
    _, out, _ = run(monkeypatch, capsys, "--older-than", "90")
    row = total_row(out)
    assert row[7] != "0" and row[8] == "0"  # skipped (live/recent) vs younger than limit


def test_apply_keeps_age(monkeypatch, capsys, env):
    f = big(mixed_session(env), 100)
    mtime = f.stat().st_mtime_ns
    apply(monkeypatch, capsys, env, "--clean", "images", "--older-than", "90")
    assert f.stat().st_mtime_ns == mtime and len(placeholders(f)) == 1
    _, out, _ = run(monkeypatch, capsys, "--older-than", "90")
    assert total_row(out)[8] == "0" and total_row(out)[6] != "0"  # still old: system data savable


def test_apply_age_column_counts_selected_kind_only(monkeypatch, capsys, env):
    big(mixed_session(env), 30)
    _, out, _ = apply(monkeypatch, capsys, env, "--clean", "images", "--system-older-than", "90")
    assert total_row(out)[8] == "0"  # system data not selected: its limit leaves nothing alone
