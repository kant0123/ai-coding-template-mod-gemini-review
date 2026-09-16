#!/usr/bin/env python3
"""PR の差分を agy (Antigravity CLI / Gemini) にレビューさせ、結果を PR コメントに残す。

使い方 (手順全体は agy-review スキル):

  python review/agy_review.py [--pr N]                 CI 緑を確認してレビューし、PR にコメントする
  python review/agy_review.py --triage FILE [--pr N]   差し戻しに対する評価を PR にコメントする
  python review/agy_review.py --check [--pr N]         マージしてよいかを判定する (merge hook 用)

終了コード:
  0  APPROVE / 評価を投稿した / マージしてよい
  1  実行時エラー (agy・gh の失敗、出力を解釈できない)。レビュー結果は投稿しない
  2  前提を満たさない (CI が緑でない / レビュー未実施 / 差し戻しが未評価)
  3  CHANGES_REQUESTED (CRITICAL か WARNING がある)

判定の記録は PR コメントの先頭に置く HTML コメントのマーカーだけで行う。
**head SHA ごと**に記録するので、修正を push すれば自動的に「未レビュー」に戻る。

レビューでは差分と関連 Wiki ページをプロンプトに埋め込み、head 時点のリポジトリの
スナップショットを agy に読み取り用として渡す (`--add-dir`)。スナップショットは実行後に消す。
"""

import argparse
import codecs
import contextlib
import fnmatch
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

REVIEW_DIR = Path(__file__).resolve().parent
PROMPT_FILE = REVIEW_DIR / "prompt.md"
INVARIANTS_FILE = REVIEW_DIR / "domain_invariants.json"
WORK_DIR = REVIEW_DIR / "work"

# 実装は Claude 系で行う前提なので、レビューは別系統のモデルに担当させる
# (自分が書いたコードを自分で採点させない)。速度優先なら gemini-3.8-flash-high。
DEFAULT_MODEL = os.environ.get("REVIEW_MODEL", "gemini-3.1-pro-high")
DEFAULT_DOMAIN = os.environ.get("REVIEW_DOMAIN", "general")
DEFAULT_TIMEOUT_MIN = 15
# これを超える差分は 1 回のレビューで精度が落ちる。PR を分けるのが本筋。
DEFAULT_MAX_CHARS = 300_000
# 埋め込む Wiki ページの合計文字数。ソースは埋め込まずスナップショットから読ませるので、
# ここは設計判断を伝える分だけあればよい。
DEFAULT_WIKI_CHARS = 60_000

# スナップショットの一時ディレクトリ。プロセスごと強制終了されると finally が走らず残るので、
# 次回の起動時にこの接頭辞で古いものを掃除する。
SNAPSHOT_PREFIX = "agy-review-snapshot-"
STALE_SNAPSHOT_HOURS = 24
# 差分に載っていない秘匿情報を、スナップショット経由で agy (= Google) に送らないための除外。
# コミットされていなければ tarball に入らないが、誤ってコミットされた場合の保険。
SECRET_NAME_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ecdsa*", "id_ed25519*")
SECRET_NAME_ALLOW = ("*.example", "*.sample", "*.template")
# Wiki の中で、関連ページ選びの対象にしないもの (カタログ・ログ・雛形)。
WIKI_SKIP = ("index.md", "log.md", "_template.md")
# wiki-lint.js の CODE_REF と同じ記法 (`path/to/file.py:123 記号名`)。行番号なしのパスも拾う。
CODE_REF_RE = re.compile(r"^([\w./-]+\.[a-zA-Z0-9]{1,5})(?::\d+)?(?:\s+.+)?$")

SEVERITIES = ("CRITICAL", "WARNING", "NITPICK")
BLOCKING = ("CRITICAL", "WARNING")

REVIEW_MARKER_RE = re.compile(r"<!-- agy-review sha=([0-9a-f]{40}) verdict=(APPROVE|CHANGES_REQUESTED) -->")
TRIAGE_MARKER_RE = re.compile(r"<!-- agy-review-triage sha=([0-9a-f]{40}) -->")

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string"},
                    "location": {"type": "string"},
                    "issue": {"type": "string"},
                    "impact": {"type": "string"},
                    "fix_code": {"type": "string"},
                },
                "required": ["severity", "category", "location", "issue", "impact"],
            },
        },
    },
    "required": ["findings"],
}


class ReviewError(RuntimeError):
    pass


class ToolDeniedError(ReviewError):
    """ヘッドレスで許可されないツールを agy が呼び、応答が空のまま終わった。再試行で通ることがある。"""

    def __init__(self, message, denied=(), conversation=None, calls=()):
        super().__init__(message)
        self.denied = list(denied)
        # 拒否された会話の ID。再試行はこの会話の続きとして行う。
        self.conversation = conversation
        # 拒否された呼び出しそのもの (`run_command: python -c ...`)。再試行の注意に名指しで載せる。
        self.calls = list(calls)
        self.raw = ""


# ---------------------------------------------------------------------------
# 外部コマンド
# ---------------------------------------------------------------------------
def run(cmd, stdin=None, timeout=120, cwd=None):
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise ReviewError(f"コマンドが見つかりません: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise ReviewError(f"タイムアウトしました ({timeout} 秒): {' '.join(cmd[:3])}")
    return proc


def gh(*args, timeout=120):
    proc = run(["gh", *args], timeout=timeout)
    if proc.returncode != 0:
        raise ReviewError(f"gh {' '.join(args[:3])} が失敗しました: {proc.stderr.strip()}")
    return proc.stdout


def pr_info(pr):
    args = ["pr", "view"] + ([str(pr)] if pr else []) + ["--json", "number,headRefOid,state"]
    return json.loads(gh(*args))


def ci_state(pr):
    """(緑か, 説明) を返す。skip されたチェックは成功扱い。

    **チェックが 1 件も無い状態を緑にしない。** push 直後はチェックがまだ登録されておらず、
    それを緑と読むと CI を待たずにレビューへ進んでしまう。gh の失敗 (出力が空) も同様。
    """
    proc = run(["gh", "pr", "checks", str(pr), "--json", "name,bucket"])
    if not proc.stdout.strip():
        if "no checks reported" in proc.stderr:
            return False, "チェックがまだ登録されていません"
        raise ReviewError(f"CI の状態を取得できませんでした (exit={proc.returncode}): {proc.stderr.strip()}")
    try:
        checks = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ReviewError(f"gh pr checks の出力を解釈できませんでした: {proc.stdout[:200]}")
    if not checks:
        return False, "チェックがまだ登録されていません"
    bad = [f"{c['name']}={c['bucket']}" for c in checks if c["bucket"] not in ("pass", "skipping")]
    if bad:
        return False, ", ".join(bad)
    return True, f"{len(checks)} 件すべて成功"


def pr_comments(pr):
    out = gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{pr}/comments", "--jq", ".[].body")
    return out


# ---------------------------------------------------------------------------
# スナップショット
# ---------------------------------------------------------------------------
def fetch_tarball(sha, timeout=300):
    """head 時点の tarball を bytes で返す。テキストで受けると壊れるので run() は使わない。

    認証はスクリプト側の gh が持つので private リポジトリでも取れる。agy には gh を触らせない。
    """
    cmd = ["gh", "api", f"repos/{{owner}}/{{repo}}/tarball/{sha}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ReviewError("コマンドが見つかりません: gh")
    except subprocess.TimeoutExpired:
        raise ReviewError(f"tarball の取得がタイムアウトしました ({timeout} 秒)")
    if proc.returncode != 0 or not proc.stdout:
        raise ReviewError(f"tarball を取得できませんでした: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def is_secret_name(name):
    if any(fnmatch.fnmatch(name, p) for p in SECRET_NAME_ALLOW):
        return False
    return any(fnmatch.fnmatch(name, p) for p in SECRET_NAME_PATTERNS)


def safe_relpath(member_name):
    """tarball の要素名から、先頭の `<owner>-<repo>-<sha>/` を外した安全な相対パスを返す。

    絶対パス・`..`・ドライブ指定を含むものは None (展開しない)。tarfile.extract は使わない —
    Python 3.9 には展開先の外に書かせない filter が無いため、自前で検査して書き出す。
    """
    parts = PurePosixPath(member_name.replace("\\", "/")).parts
    if len(parts) < 2 or member_name.startswith(("/", "\\")):
        return None
    rel = parts[1:]
    if any(p in ("", ".", "..") or ":" in p for p in rel):
        return None
    return PurePosixPath(*rel)


def extract_snapshot(data, dest):
    """通常ファイルだけを dest に書き出す。(書き出した数, 除外したパスの一覧) を返す。

    リンク・デバイスは展開しない (展開先の外を指せるため)。秘匿情報らしい名前も除外する。
    """
    written, skipped = 0, []
    dest = Path(dest)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar:
            if member.isdir():
                continue
            rel = safe_relpath(member.name)
            if rel is None:
                skipped.append(member.name)
                continue
            if not member.isfile() or is_secret_name(rel.name):
                skipped.append(str(rel))
                continue
            target = dest.joinpath(*rel.parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            except OSError:
                # Windows で使えないファイル名など。レビューを止めるほどではない。
                skipped.append(str(rel))
                continue
            written += 1
    if written == 0:
        raise ReviewError("tarball から展開できたファイルが 0 件です。")
    return written, skipped


def remove_tree(path):
    """読み取り専用属性を外しながら消す。消せたら True。"""
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)
    return not Path(path).exists()


def sweep_stale_snapshots(tmp_root=None, now=None, hours=STALE_SNAPSHOT_HOURS):
    """強制終了で残ったスナップショットのうち、古いものだけを消す。消したパスを返す。

    実行中の別のレビューのスナップショットを消さないよう、時間で区切る。
    """
    root = Path(tmp_root or tempfile.gettempdir())
    now = now if now is not None else time.time()
    removed = []
    for entry in root.glob(f"{SNAPSHOT_PREFIX}*"):
        try:
            if not entry.is_dir() or now - entry.stat().st_mtime < hours * 3600:
                continue
        except OSError:
            continue
        if remove_tree(entry):
            removed.append(entry)
    return removed


@contextlib.contextmanager
def snapshot_dir(tmp_root=None):
    """スナップショット用の一時ディレクトリ。例外・Ctrl+C でも抜けるときに消す。"""
    sweep_stale_snapshots(tmp_root)
    path = Path(tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX, dir=tmp_root))
    try:
        yield path
    finally:
        if not remove_tree(path):
            print(f"[WARN] スナップショットを削除できませんでした: {path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------
def _unquote_git_path(path):
    """git が引用符で囲んだパス (`"b/\\346\\227\\245.md"`) を戻す。非 ASCII や特殊文字を含むと囲まれる。"""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    raw = codecs.escape_decode(path[1:-1].encode("utf-8"))[0]
    return raw.decode("utf-8", "replace")


def changed_paths(diff):
    """差分に載っているファイルのパス (変更後の名前) を順序つきで返す。"""
    paths = []
    for m in re.finditer(r'^diff --git (?:"a/(?:[^"\\]|\\.)*"|a/.+?) ("b/(?:[^"\\]|\\.)*"|b/.+)$', diff, re.MULTILINE):
        path = _unquote_git_path(m.group(1))
        path = path[2:] if path.startswith("b/") else path
        if path not in paths:
            paths.append(path)
    return paths


def code_refs(text):
    """ページ本文のコードスパンから参照しているパスを集める (フェンスの中は記法の例なので除く)。"""
    body = re.sub(r"```[\s\S]*?```", "", text)
    refs = set()
    for span in re.findall(r"`([^`\n]+)`", body):
        m = CODE_REF_RE.match(span.strip())
        if m:
            path = m.group(1)
            refs.add(path[2:] if path.startswith("./") else path)
    return refs


def select_wiki_pages(root, changed, budget):
    """埋め込む Wiki ページを選ぶ。(採用 [(相対パス, 本文)], 予算で外した相対パス) を返す。

    候補は overview.md・差分で変更されたページ・変更ファイルを参照するページ。
    overview → 変更されたページ → 参照している変更ファイルの多い順に、予算に収まるものを採る。
    """
    wiki = Path(root) / "wiki"
    if not wiki.is_dir():
        return [], []
    changed = set(changed)
    candidates = []
    for page in sorted(wiki.rglob("*.md")):
        rel = page.relative_to(root).as_posix()
        if page.name in WIKI_SKIP:
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        if rel == "wiki/overview.md":
            rank = (0, 0)
        elif rel in changed:
            rank = (1, 0)
        else:
            hits = len(code_refs(text) & changed)
            if not hits:
                continue
            rank = (2, -hits)
        candidates.append((rank, rel, text))

    included, omitted, used = [], [], 0
    for _, rel, text in sorted(candidates):
        if used + len(text) > budget:
            omitted.append(rel)
            continue
        included.append((rel, text))
        used += len(text)
    return included, omitted


# ---------------------------------------------------------------------------
# プロンプトと agy の出力
# ---------------------------------------------------------------------------
ENVIRONMENT_NOTE = """## この環境の制約(agy CLI・ヘッドレス)

- **コマンド実行・URL 取得・ファイルの書き込みは許可されていません。** 呼んだ時点で拒否され、
  レビューが応答なしで終了します。確かめたいことは `view_file` / `grep_search` で読んで推論し、
  実行していない点は該当する指摘の issue にその旨を書いてください。
- **agy 自身の会話ログを読もうとしないでください** (`transcript.jsonl` / `transcript_full.jsonl`、
  `.gemini/antigravity-cli/brain/` 配下)。表示上このメッセージが `<truncated NNNN bytes>` と
  切り詰められて見えても、**本文は全文があなたに渡っています**(実測で確認済み。約 4KB を超える
  メッセージは保存・表示側だけが切り詰められます)。読み直す必要はなく、試みると拒否されて
  レビューが失敗します。
"""


def build_prompt(diff, domain, snapshot=None, wiki_pages=(), changed=()):
    """参考資料 → 差分 → 指示の順に並べる。長い入力では末尾の指示の方が守られやすいため。"""
    invariants = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    if domain not in invariants:
        raise ReviewError(f"未知のドメインです: {domain} (選択肢: {', '.join(invariants)})")
    info = invariants[domain]
    rules = "\n".join(f"- {r}" for r in info["critical_rules"])

    parts = ["以下の参考資料と差分を読んだ上で、末尾の「Role」以降の指示に従って差分をレビューしてください。", ""]
    if snapshot:
        parts += [
            "# 参考資料 1: リポジトリのスナップショット",
            "",
            f"PR の head 時点のリポジトリ全体を `{snapshot}` に置いてあります(読み取り専用のコピー)。",
            "差分に出てこない前提は、ここを開いて確認してください。変更されたファイル:",
            "",
            *[f"- `{snapshot / p}`" for p in changed],
            "",
        ]
    if wiki_pages:
        parts += ["# 参考資料 2: 関連する Wiki ページ", ""]
        for rel, text in wiki_pages:
            parts += [f"## {rel}", "", fence(text), ""]
    parts += [
        "# レビュー対象の差分",
        "",
        diff,
        "",
        PROMPT_FILE.read_text(encoding="utf-8"),
        "",
        f"## ドメイン不変条件: {info['name']}",
        "",
        rules,
        "",
        # agy CLI 固有の事情なので prompt.md (上流と共有) ではなくここに置く。末尾なのは、
        # 長い入力では末尾の指示の方が守られやすいため。
        ENVIRONMENT_NOTE,
    ]
    return "\n".join(parts)


def _events(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def parse_output(stdout):
    """agy の stream-json 出力から findings を取り出す。

    **出力が空・解釈不能のときは絶対に「指摘 0 件」として扱わない。**
    それを APPROVE として記録すると、レビューしていないのにレビュー済みの記録だけが残る。
    """
    result = None
    for event in _events(stdout):
        if event.get("event") == "result":
            result = event.get("result") or {}
    if result is None:
        raise ReviewError("agy の出力に result イベントがありません (出力が空か途中で切れています)。")
    if result.get("status") != "SUCCESS":
        raise ReviewError(f"agy が失敗を報告しました: {result.get('error') or result.get('status')}")

    payload = result.get("structured_output")
    if not isinstance(payload, dict):
        text = (result.get("response") or "").strip()
        denied = [d.get("action") for d in result.get("denied_actions") or [] if isinstance(d, dict)]
        if not text and denied:
            # ヘッドレスでは許可を求められないツールを呼ぶと、その場で空の応答のまま終わる。
            raise ToolDeniedError(f"agy のツール呼び出しが拒否され、レビューが途中で終わりました: {', '.join(denied)}",
                                  denied, result.get("conversation_id"), denied_calls(stdout))
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text[:text.rfind("```")] if "```" in text else text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise ReviewError("agy の出力から JSON を取り出せませんでした。")
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        raise ReviewError("agy の出力に findings 配列がありません。")

    normalized = []
    for f in findings:
        if not isinstance(f, dict):
            raise ReviewError(f"findings の要素が不正です: {f!r}")
        severity = str(f.get("severity", "")).upper()
        if severity not in SEVERITIES:
            raise ReviewError(f"未知の severity です: {f.get('severity')!r}")
        normalized.append({**f, "severity": severity})
    return normalized


def denied_calls(stdout):
    """許可が無くて失敗したツール呼び出しを `ツール名: 引数` の形で順序つきで返す。"""
    calls = []
    for event in _events(stdout):
        step = event.get("step_update") if event.get("event") == "step_update" else None
        if not isinstance(step, dict) or step.get("state") != "ERROR":
            continue
        info = step.get("tool_info") or {}
        if "denied" not in str((info.get("error") or {}).get("message", "")):
            continue
        params = info.get("parameters") or {}
        arg = params.get("CommandLine") or params.get("Url") or json.dumps(params, ensure_ascii=False)
        call = f"{step.get('tool_name')}: {arg}"
        if call not in calls:
            calls.append(call)
    return calls


def files_read(stdout, root):
    """agy が view_file で開いたファイルを、スナップショットからの相対パスで順序つきで返す。"""
    root = Path(root).resolve()
    seen = []
    for event in _events(stdout):
        step = event.get("step_update") if event.get("event") == "step_update" else None
        if not isinstance(step, dict) or step.get("tool_name") != "view_file" or step.get("state") != "DONE":
            continue
        raw = ((step.get("tool_info") or {}).get("parameters") or {}).get("AbsolutePath")
        if not raw:
            continue
        try:
            shown = Path(raw).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            # スナップショットの外、または OS が扱えないパス。表示のためだけなので落とさない。
            shown = raw
        if shown not in seen:
            seen.append(shown)
    return seen


def kill_tree(proc):
    """agy の子プロセスごと止める。残るとスナップショットのファイルを掴んだままになり消せない。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def call_agy(prompt, model, timeout_min, add_dir, conversation=None):
    """(findings, agy の生出力) を返す。conversation を渡すとその会話の続きとして prompt を送る。"""
    WORK_DIR.mkdir(exist_ok=True)
    schema = WORK_DIR / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    # 本文は -p の引数ではなく stdin で渡す。引数だと Windows のコマンドライン長 (約 32KB) に当たる。
    message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    # --add-dir で加えたディレクトリはヘッドレスでも読める (書き込みも通るので、捨てるコピーを渡す)。
    # コマンド実行と URL 取得は許可ルールを置かない限り拒否される。
    cmd = ["agy", "--add-dir", str(add_dir), "--input-format", "stream-json", "--output-format", "stream-json",
           "--json-schema", str(schema), "--print-timeout", f"{timeout_min}m", "--model", model]
    if conversation:
        cmd += ["--conversation", conversation]
    popen_extra = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(add_dir), **popen_extra)
    except FileNotFoundError:
        raise ReviewError("コマンドが見つかりません: agy")
    try:
        out, err = proc.communicate(message.encode("utf-8"), timeout=timeout_min * 60 + 60)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        raise ReviewError(f"agy がタイムアウトしました ({timeout_min} 分)")
    except BaseException:
        kill_tree(proc)
        raise
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = " / ".join(stderr.strip().splitlines()[-3:])
        raise ReviewError(f"agy が異常終了しました (exit={proc.returncode}): {tail}")
    try:
        return parse_output(stdout), stdout
    except ReviewError as e:
        if isinstance(e, ToolDeniedError):
            e.raw = stdout
        dump = WORK_DIR / "agy_raw_output.txt"
        text = (stdout or "(出力なし)") + ("\n--- stderr ---\n" + stderr if stderr.strip() else "")
        # 会話の続きのときは追記する。上書きすると、拒否に至った前半の出力が消えて原因を追えない。
        with open(dump, "a" if conversation else "w", encoding="utf-8") as f:
            f.write(f"\n=== 会話の続き ({conversation}) ===\n{text}" if conversation else text)
        e.args = (f"{e} 生出力: {dump}",)
        raise


TOOL_DENIED_RETRIES = 3
TOOL_DENIED_NOTE = (
    "## 注意: ツール呼び出しが拒否されました\n\n"
    "次の呼び出しは許可されておらず、この環境では**何度呼んでも実行されません**"
    "(呼んだ時点でレビューが失敗します)。\n\n"
    "{calls}\n\n"
    "`run_command` / `read_url_content` / 書き込み系のツールは二度と呼ばないでください。"
    "会話ログ (`transcript.jsonl` / `transcript_full.jsonl`) を読み直す必要もありません"
    "(切り詰めて表示されているだけで、本文は全文渡っています)。"
    "結果が知りたかった点は `view_file` / `grep_search` でコードを読んで推論し、"
    "推論で済ませた点は該当する指摘の issue に「実行して確かめていない」と書いてください。"
    "そのうえでレビューを最後まで行い、指定の JSON で結果を返してください。"
)


def call_agy_with_retry(prompt, model, timeout_min, add_dir, call=None, retries=TOOL_DENIED_RETRIES):
    """ツール拒否で途中終了したときだけ再試行する。それ以外の失敗は即座に上げる。(findings, 全試行の生出力) を返す。

    プロンプトで読み取り系ツールに限っても、agy は検証のために `python -c` などを呼ぶことがある (実測)。
    再試行は**拒否された会話の続き**として、拒否された呼び出しを名指しした注意だけを送る。
    最初からやり直してプロンプト末尾に一般的な注意を足すだけでは、同じコマンドを呼び直して落ちた (#22)。
    会話 ID が取れなかったときだけ、最初のプロンプトに注意を足して新しい会話でやり直す。
    """
    call = call or call_agy
    message, conversation, raws, calls = prompt, None, [], []
    for attempt in range(retries + 1):
        try:
            findings, raw = call(message, model, timeout_min, add_dir, conversation)
            return findings, "\n".join(raws + [raw])
        except ToolDeniedError as e:
            raws.append(e.raw)
            if attempt == retries:
                raise
            print(f"[WARN] {e} — 再試行します ({attempt + 1}/{retries})", file=sys.stderr)
            calls += [c for c in e.calls or [f"{d}: (内容不明)" for d in e.denied] if c not in calls]
            note = TOOL_DENIED_NOTE.format(calls="\n".join(f"- `{c}`" for c in calls))
            if e.conversation:
                message, conversation = note, e.conversation
            else:
                message, conversation = f"{prompt}\n\n{note}", None


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------
def verdict_of(findings):
    return "CHANGES_REQUESTED" if any(f["severity"] in BLOCKING for f in findings) else "APPROVE"


def fence(code):
    ticks = max([len(m) for m in re.findall(r"`+", code)] + [2]) + 1
    return f"{'`' * ticks}\n{code.rstrip()}\n{'`' * ticks}"


def render_context(context):
    """何を見せ、agy が何を読んだか。省いたことに気づかないと「全部見た上での APPROVE」と読み違えるため。"""
    read = context.get("files_read", [])
    unread = [p for p in context.get("changed_existing", []) if p not in read]
    lines = ["<details><summary>参照した資料</summary>", ""]
    lines.append("- 埋め込んだ Wiki: " + (", ".join(f"`{p}`" for p in context.get("wiki_included", [])) or "なし"))
    if context.get("wiki_omitted"):
        lines.append("- 予算超過で外した Wiki: " + ", ".join(f"`{p}`" for p in context["wiki_omitted"]))
    lines.append(f"- agy が開いたファイル ({len(read)} 件): " + (", ".join(f"`{p}`" for p in read) or "なし"))
    if unread:
        lines.append("- **開かれなかった変更ファイル:** " + ", ".join(f"`{p}`" for p in unread))
    if context.get("snapshot_skipped"):
        lines.append(f"- スナップショットから除外したファイル: {len(context['snapshot_skipped'])} 件"
                     "(リンク・秘匿情報らしい名前・書き出せない名前)")
    lines += ["", "</details>", ""]
    return lines


def render(findings, sha, model, domain, context=None):
    verdict = verdict_of(findings)
    icon = "✅" if verdict == "APPROVE" else "🔁"
    counts = " / ".join(f"{s} {sum(f['severity'] == s for f in findings)}" for s in SEVERITIES)
    lines = [
        f"<!-- agy-review sha={sha} verdict={verdict} -->",
        f"## {icon} agy レビュー: {verdict}",
        "",
        f"`{sha[:7]}` / `{model}` / ドメイン `{domain}` / {counts}",
        "",
    ]
    if context is not None:
        lines += render_context(context)
    if not findings:
        lines.append("指摘はありません。")
    for i, f in enumerate(sorted(findings, key=lambda f: SEVERITIES.index(f["severity"])), 1):
        lines += [
            f"### {i}. [{f['severity']}] {f.get('category', '')}",
            "",
            f"**場所:** `{f.get('location', '')}`",
            "",
            f.get("issue", ""),
            "",
            f"**影響:** {f.get('impact', '')}",
            "",
        ]
        if f.get("fix_code"):
            lines += [fence(f["fix_code"]), ""]
    if verdict == "CHANGES_REQUESTED":
        lines += [
            "---",
            "依頼元は CRITICAL / WARNING を 1 件ずつ評価し、`python review/agy_review.py --triage <file>` で",
            "評価を記録してください(修正する場合は push 後に再レビュー)。",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------
def cmd_review(args):
    info = pr_info(args.pr)
    pr, sha = info["number"], info["headRefOid"]
    if info["state"] != "OPEN":
        raise ReviewError(f"PR #{pr} は {info['state']} です。")

    green, detail = ci_state(pr)
    if not green:
        print(f"PR #{pr} の CI が緑ではありません ({detail})。CI が通ってからレビューしてください。")
        return 2
    print(f"PR #{pr} `{sha[:7]}`: CI {detail}")

    diff = gh("pr", "diff", str(pr))
    if not diff.strip():
        raise ReviewError(f"PR #{pr} の差分が空です。")
    if len(diff) > args.max_chars:
        raise ReviewError(f"差分が {len(diff)} 文字あり、上限 {args.max_chars} を超えています。"
                          "PR を分けるか、--max-chars で上限を上げてください。")
    changed = changed_paths(diff)

    with snapshot_dir() as root:
        written, skipped = extract_snapshot(fetch_tarball(sha), root)
        wiki_pages, wiki_omitted = select_wiki_pages(root, changed, args.wiki_chars)
        changed_existing = [p for p in changed if (root / p).is_file()]
        print(f"スナップショット {written} ファイル / Wiki {len(wiki_pages)} ページ"
              f"{f' (予算超過で {len(wiki_omitted)} ページ除外)' if wiki_omitted else ''}")
        print(f"agy ({args.model}) でレビュー中... 差分 {len(diff)} 文字")
        prompt = build_prompt(diff, args.domain, root, wiki_pages, changed_existing)
        findings, raw = call_agy_with_retry(prompt, args.model, args.timeout, root)
        context = {
            "wiki_included": [rel for rel, _ in wiki_pages],
            "wiki_omitted": wiki_omitted,
            "files_read": files_read(raw, root),
            "changed_existing": changed_existing,
            "snapshot_skipped": skipped,
        }

    # レビュー中に push されていたら、古い差分の結果を新しい head に紐付けない。
    if pr_info(pr)["headRefOid"] != sha:
        raise ReviewError("レビュー中に PR の head が変わりました。再実行してください。")

    report = render(findings, sha, args.model, args.domain, context)
    WORK_DIR.mkdir(exist_ok=True)
    out = WORK_DIR / f"pr-{pr}-{sha[:7]}.md"
    out.write_text(report, encoding="utf-8")
    if not args.no_post:
        gh("pr", "comment", str(pr), "--body-file", str(out))
    print(report)
    print(f"(レポート: {out}{'' if args.no_post else ' / PR にコメントしました'})")
    return 0 if verdict_of(findings) == "APPROVE" else 3


def cmd_triage(args):
    info = pr_info(args.pr)
    pr, sha = info["number"], info["headRefOid"]
    reviews = {s: v for s, v in REVIEW_MARKER_RE.findall(pr_comments(pr))}
    if reviews.get(sha) != "CHANGES_REQUESTED":
        print(f"`{sha[:7]}` に対する差し戻しのレビューがありません。評価は差し戻しに対してだけ記録します。")
        return 2
    body = Path(args.triage).read_text(encoding="utf-8").strip()
    if not body:
        raise ReviewError("評価の本文が空です。")
    comment = f"<!-- agy-review-triage sha={sha} -->\n## agy レビューの評価 (`{sha[:7]}`)\n\n{body}\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(comment)
    try:
        gh("pr", "comment", str(pr), "--body-file", f.name)
    finally:
        os.unlink(f.name)
    print(f"PR #{pr} に評価を記録しました。")
    return 0


def check_merge(comments, sha):
    """(マージしてよいか, 理由)。merge hook から使う。"""
    reviews = {s: v for s, v in REVIEW_MARKER_RE.findall(comments)}
    triaged = set(TRIAGE_MARKER_RE.findall(comments))
    verdict = reviews.get(sha)
    if verdict is None:
        return False, (f"head `{sha[:7]}` に対する agy レビューがありません。"
                       "CI が緑になってから `python review/agy_review.py` を実行してください。")
    if verdict == "CHANGES_REQUESTED" and sha not in triaged:
        return False, (f"head `{sha[:7]}` のレビューは差し戻しで、評価が記録されていません。"
                       "指摘を評価し、的外れなら上流に起票した上で `python review/agy_review.py --triage <file>` を実行してください。"
                       "妥当な指摘があるなら修正して push してください。")
    return True, f"head `{sha[:7]}` は agy レビュー済みです ({verdict})。"


def cmd_check(args):
    info = pr_info(args.pr)
    ok, reason = check_merge(pr_comments(info["number"]), info["headRefOid"])
    print(f"PR #{info['number']}: {reason}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 2


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pr", type=int, help="PR 番号。省略時は現在のブランチの PR")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--triage", metavar="FILE", help="差し戻しに対する評価 (Markdown) を PR に記録する")
    mode.add_argument("--check", action="store_true", help="マージしてよいかを判定する (merge hook 用)")
    p.add_argument("--domain", default=DEFAULT_DOMAIN, help=f"ドメイン不変条件 (既定: {DEFAULT_DOMAIN})")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"agy のモデル (既定: {DEFAULT_MODEL})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MIN, help="agy のタイムアウト (分)")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="差分の上限文字数")
    p.add_argument("--wiki-chars", type=int, default=DEFAULT_WIKI_CHARS, help="埋め込む Wiki ページの合計文字数")
    p.add_argument("--no-post", action="store_true", help="PR にコメントせずローカルにだけ出力する")
    args = p.parse_args()
    try:
        if args.triage:
            return cmd_triage(args)
        if args.check:
            return cmd_check(args)
        return cmd_review(args)
    except ReviewError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
