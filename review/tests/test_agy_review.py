"""agy_review.py のうち、壊れると「レビューしていないのに通る」部分の回帰テスト。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agy_review as ar  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40


def result_line(**result):
    return json.dumps({"event": "result", "result": result}, ensure_ascii=False)


# --- parse_output: 空・失敗を APPROVE にしない ---------------------------------
@pytest.mark.parametrize("stdout", ["", "\n", '{"event":"step_update"}'])
def test_output_without_result_is_error(stdout):
    with pytest.raises(ar.ReviewError):
        ar.parse_output(stdout)


def test_failed_status_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="ERROR", error="quota"))


def test_missing_findings_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="SUCCESS", response="レビューしました"))


def test_structured_output_is_used_and_severity_normalized():
    out = "\n".join([
        '{"event":"step_update"}',
        result_line(status="SUCCESS", structured_output={"findings": [
            {"severity": "critical", "category": "x", "location": "a.py:1", "issue": "i", "impact": "m"}]}),
    ])
    findings = ar.parse_output(out)
    assert findings[0]["severity"] == "CRITICAL"


def test_fenced_response_is_parsed():
    body = '```json\n{"findings": [{"severity": "NITPICK", "fix_code": "```py\\nx\\n```"}]}\n```'
    assert ar.parse_output(result_line(status="SUCCESS", response=body))[0]["severity"] == "NITPICK"


def test_unknown_severity_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="SUCCESS", structured_output={"findings": [{"severity": "INFO"}]}))


# --- verdict / render ----------------------------------------------------------
def test_verdict():
    assert ar.verdict_of([]) == "APPROVE"
    assert ar.verdict_of([{"severity": "NITPICK"}]) == "APPROVE"
    assert ar.verdict_of([{"severity": "WARNING"}]) == "CHANGES_REQUESTED"


def test_render_marker_is_recognized_by_check():
    report = ar.render([{"severity": "WARNING", "fix_code": "```\nx\n```"}], SHA, "m", "general")
    assert ar.REVIEW_MARKER_RE.search(report).groups() == (SHA, "CHANGES_REQUESTED")
    # fix_code 内のフェンスで Markdown が壊れない
    assert "````" in report


# --- ci_state: CI 緑の判定 -----------------------------------------------------
class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def checks(*buckets):
    return json.dumps([{"name": f"c{i}", "bucket": b} for i, b in enumerate(buckets)])


@pytest.mark.parametrize("proc, green", [
    (Proc(0, checks("pass", "skipping")), True),
    (Proc(1, checks("pass", "fail")), False),
    (Proc(8, checks("pass", "pending")), False),
    (Proc(0, "[]"), False),
    # push 直後でチェックが未登録
    (Proc(1, "", "no checks reported on the 'x' branch"), False),
])
def test_ci_state(monkeypatch, proc, green):
    monkeypatch.setattr(ar, "run", lambda *a, **k: proc)
    assert ar.ci_state(1)[0] is green


@pytest.mark.parametrize("proc", [
    Proc(1, "", "HTTP 502"),
    Proc(0, "not json"),
])
def test_ci_state_error_is_not_green(monkeypatch, proc):
    monkeypatch.setattr(ar, "run", lambda *a, **k: proc)
    with pytest.raises(ar.ReviewError):
        ar.ci_state(1)


# --- check_merge: マージ可否 ---------------------------------------------------
def review(sha, verdict):
    return f"<!-- agy-review sha={sha} verdict={verdict} -->\n"


def triage(sha):
    return f"<!-- agy-review-triage sha={sha} -->\n"


def test_no_review_blocks():
    assert not ar.check_merge("", SHA)[0]


def test_review_of_old_head_blocks():
    assert not ar.check_merge(review(OTHER, "APPROVE"), SHA)[0]


def test_approve_passes():
    assert ar.check_merge(review(SHA, "APPROVE"), SHA)[0]


def test_changes_requested_without_triage_blocks():
    comments = review(SHA, "CHANGES_REQUESTED") + triage(OTHER)
    assert not ar.check_merge(comments, SHA)[0]


def test_changes_requested_with_triage_passes():
    assert ar.check_merge(review(SHA, "CHANGES_REQUESTED") + triage(SHA), SHA)[0]


# --- ツール拒否: 途中で終わったレビューを APPROVE にしない ----------------------
def test_denied_tool_with_empty_response_is_error():
    out = result_line(status="SUCCESS", response="", denied_actions=[{"action": "command"}])
    with pytest.raises(ar.ReviewError, match="command"):
        ar.parse_output(out)


# --- スナップショットの展開 -----------------------------------------------------
def make_tar(entries):
    """entries: [(name, bytes | None, type)]。type は tarfile の型定数。"""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "../../outside"
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def test_extract_snapshot_writes_regular_files_only(tmp_path):
    import tarfile
    data = make_tar([
        ("owner-repo-abc/", None, tarfile.DIRTYPE),
        ("owner-repo-abc/src/app.py", b"print(1)\n", tarfile.REGTYPE),
        ("owner-repo-abc/.env", b"TOKEN=x\n", tarfile.REGTYPE),
        ("owner-repo-abc/.env.example", b"TOKEN=\n", tarfile.REGTYPE),
        ("owner-repo-abc/keys/server.pem", b"key\n", tarfile.REGTYPE),
        ("owner-repo-abc/link", None, tarfile.SYMTYPE),
        ("owner-repo-abc/../escape.txt", b"x", tarfile.REGTYPE),
    ])
    dest = tmp_path / "snap"
    dest.mkdir()
    written, skipped = ar.extract_snapshot(data, dest)
    assert written == 2
    assert (dest / "src" / "app.py").read_bytes() == b"print(1)\n"
    assert (dest / ".env.example").exists()
    assert not (dest / ".env").exists()
    assert not (dest / "keys" / "server.pem").exists()
    assert not (dest / "link").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert len(skipped) == 4


@pytest.mark.parametrize("name, secret", [
    (".env", True), (".env.local", True), ("id_rsa", True), ("server.key", True),
    (".env.example", False), ("app.py", False), ("keyboard.py", False),
])
def test_is_secret_name(name, secret):
    assert ar.is_secret_name(name) is secret


@pytest.mark.parametrize("returncode, stdout", [(1, b""), (0, b"")])
def test_fetch_tarball_failure_is_error(monkeypatch, returncode, stdout):
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: Proc(returncode, stdout, b"HTTP 404"))
    with pytest.raises(ar.ReviewError):
        ar.fetch_tarball(SHA)


def test_extract_snapshot_with_no_files_is_error(tmp_path):
    import tarfile
    with pytest.raises(ar.ReviewError):
        ar.extract_snapshot(make_tar([("owner-repo-abc/", None, tarfile.DIRTYPE)]), tmp_path)


# --- スナップショットの後片付け -------------------------------------------------
def test_snapshot_is_removed_even_on_error(tmp_path):
    with pytest.raises(RuntimeError):
        with ar.snapshot_dir(tmp_path) as root:
            readonly = root / "readonly.txt"
            readonly.write_text("x")
            readonly.chmod(0o444)
            raise RuntimeError("boom")
    assert not root.exists()


class RunningProc:
    """communicate が指定の例外を投げる agy のプロセス。"""
    pid = 999
    returncode = None

    def __init__(self, exc):
        self.exc = exc

    def communicate(self, *args, timeout=None):
        if timeout is not None:
            raise self.exc
        return b"", b""

    def poll(self):
        return None

    def kill(self):
        pass


@pytest.mark.parametrize("exc, expected", [
    (ar.subprocess.TimeoutExpired("agy", 60), ar.ReviewError),
    (KeyboardInterrupt(), KeyboardInterrupt),
])
def test_call_agy_kills_process_tree_when_interrupted(monkeypatch, tmp_path, exc, expected):
    # 子プロセスが残るとスナップショットのファイルを掴んだままになり、後片付けで消せない。
    monkeypatch.setattr(ar, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(ar.subprocess, "Popen", lambda *a, **k: RunningProc(exc))
    killed = []
    monkeypatch.setattr(ar, "kill_tree", lambda proc: killed.append(proc.pid))
    with pytest.raises(expected):
        ar.call_agy("prompt", "model", 1, tmp_path)
    assert killed == [999]


def test_kill_tree_uses_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(ar.os, "name", "nt")
    called = []
    monkeypatch.setattr(ar.subprocess, "run", lambda cmd, **k: called.append(cmd))
    ar.kill_tree(RunningProc(None))
    assert called == [["taskkill", "/PID", "999", "/T", "/F"]]


def test_kill_tree_kills_process_group_on_posix(monkeypatch):
    monkeypatch.setattr(ar.os, "name", "posix")
    monkeypatch.setattr(ar.signal, "SIGKILL", 9, raising=False)
    called = []
    monkeypatch.setattr(ar.os, "killpg", lambda pid, sig: called.append((pid, sig)), raising=False)
    ar.kill_tree(RunningProc(None))
    assert called == [(999, 9)]


def test_sweep_removes_only_stale_snapshots(tmp_path):
    import os
    old = tmp_path / f"{ar.SNAPSHOT_PREFIX}old"
    new = tmp_path / f"{ar.SNAPSHOT_PREFIX}new"
    other = tmp_path / "unrelated-old"
    for d in (old, new, other):
        d.mkdir()
    now = 10 * 86400
    os.utime(old, (now - 25 * 3600, now - 25 * 3600))
    os.utime(new, (now - 1 * 3600, now - 1 * 3600))
    os.utime(other, (now - 25 * 3600, now - 25 * 3600))
    removed = ar.sweep_stale_snapshots(tmp_path, now=now)
    assert removed == [old]
    assert new.exists() and other.exists()


# --- Wiki ページの選択 ----------------------------------------------------------
DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-a
+b
diff --git a/wiki/components/app.md b/wiki/components/app.md
"""


def test_changed_paths():
    assert ar.changed_paths(DIFF) == ["src/app.py", "wiki/components/app.md"]


def test_changed_paths_with_quoted_and_spaced_names():
    # 非 ASCII・特殊文字を含むパスは git が引用符で囲み、UTF-8 のバイトを 8 進でエスケープする。
    diff = "\n".join([
        'diff --git "a/docs/\\346\\227\\245\\346\\234\\254.md" "b/docs/\\346\\227\\245\\346\\234\\254.md"',
        "diff --git a/my dir/file.txt b/my dir/file.txt",
        'diff --git "a/q\\"uote.txt" "b/q\\"uote.txt"',
    ])
    assert ar.changed_paths(diff) == ["docs/日本.md", "my dir/file.txt", 'q"uote.txt']


def test_files_read_survives_unresolvable_path(monkeypatch, tmp_path):
    # agy が OS の扱えないパスで view_file を呼んでも、レビュー結果ごと失わない。
    real_resolve = ar.Path.resolve

    def resolve(self, *a, **k):
        if "bad" in str(self):
            raise OSError("invalid name")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(ar.Path, "resolve", resolve)
    out = step("view_file", "DONE", AbsolutePath="bad<name>.py")
    assert ar.files_read(out, tmp_path) == ["bad<name>.py"]



def write_page(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_select_wiki_pages_ranks_and_budgets(tmp_path):
    write_page(tmp_path, "wiki/overview.md", "overview")
    write_page(tmp_path, "wiki/index.md", "`src/app.py:1`")
    write_page(tmp_path, "wiki/components/_template.md", "`src/app.py:1`")
    write_page(tmp_path, "wiki/components/app.md", "changed page")
    write_page(tmp_path, "wiki/concepts/uses-app.md", "入口は `src/app.py:10 main()` にある")
    write_page(tmp_path, "wiki/concepts/example-only.md", "```\n`src/app.py:1`\n```")
    write_page(tmp_path, "wiki/concepts/unrelated.md", "`src/other.py:1`")
    write_page(tmp_path, "wiki/operations/huge.md", "`./src/app.py` " + "x" * 1000)

    included, omitted = ar.select_wiki_pages(tmp_path, ar.changed_paths(DIFF), budget=200)
    assert [rel for rel, _ in included] == ["wiki/overview.md", "wiki/components/app.md", "wiki/concepts/uses-app.md"]
    assert omitted == ["wiki/operations/huge.md"]


def test_select_wiki_pages_without_wiki(tmp_path):
    assert ar.select_wiki_pages(tmp_path, ["src/app.py"], budget=1000) == ([], [])


def test_code_refs_keeps_dotted_directories():
    assert ar.code_refs("`.github/workflows/test.yml:3`") == {".github/workflows/test.yml"}


# --- agy が読んだファイル -------------------------------------------------------
def step(tool, state, **params):
    return json.dumps({"event": "step_update", "step_update": {
        "tool_name": tool, "state": state, "tool_info": {"parameters": params}}})


def test_files_read(tmp_path):
    target = tmp_path / "src" / "app.py"
    out = "\n".join([
        step("view_file", "ACTIVE", AbsolutePath=str(target)),
        step("view_file", "DONE", AbsolutePath=str(target)),
        step("view_file", "DONE", AbsolutePath=str(target)),
        step("list_dir", "DONE", DirectoryPath=str(tmp_path)),
        step("view_file", "ERROR", AbsolutePath=str(tmp_path / "denied.py")),
    ])
    assert ar.files_read(out, tmp_path) == ["src/app.py"]


def test_render_lists_unread_changed_files():
    context = {"wiki_included": [], "wiki_omitted": ["wiki/x.md"], "files_read": ["a.py"],
               "changed_existing": ["a.py", "b.py"], "snapshot_skipped": []}
    report = ar.render([], SHA, "m", "general", context)
    assert "`b.py`" in report.split("開かれなかった変更ファイル")[1]
    assert "`wiki/x.md`" in report
    assert ar.REVIEW_MARKER_RE.search(report).groups() == (SHA, "APPROVE")
