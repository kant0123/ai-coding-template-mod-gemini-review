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
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

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
# プロンプトと agy の出力
# ---------------------------------------------------------------------------
def build_prompt(diff, domain):
    invariants = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    if domain not in invariants:
        raise ReviewError(f"未知のドメインです: {domain} (選択肢: {', '.join(invariants)})")
    info = invariants[domain]
    rules = "\n".join(f"- {r}" for r in info["critical_rules"])
    return "\n".join([
        PROMPT_FILE.read_text(encoding="utf-8"),
        "",
        f"## ドメイン不変条件: {info['name']}",
        "",
        rules,
        "",
        "## レビュー対象の差分",
        "",
        diff,
    ])


def parse_output(stdout):
    """agy の stream-json 出力から findings を取り出す。

    **出力が空・解釈不能のときは絶対に「指摘 0 件」として扱わない。**
    それを APPROVE として記録すると、レビューしていないのにレビュー済みの記録だけが残る。
    """
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result = event.get("result") or {}
    if result is None:
        raise ReviewError("agy の出力に result イベントがありません (出力が空か途中で切れています)。")
    if result.get("status") != "SUCCESS":
        raise ReviewError(f"agy が失敗を報告しました: {result.get('error') or result.get('status')}")

    payload = result.get("structured_output")
    if not isinstance(payload, dict):
        text = (result.get("response") or "").strip()
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


def call_agy(prompt, model, timeout_min):
    WORK_DIR.mkdir(exist_ok=True)
    schema = WORK_DIR / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    # 本文は -p の引数ではなく stdin で渡す。引数だと Windows のコマンドライン長 (約 32KB) に当たり、
    # ファイルパスを渡して読ませる方式はヘッドレスでは読み取り許可が自動拒否されて空振りする。
    message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    cmd = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
           "--json-schema", str(schema), "--print-timeout", f"{timeout_min}m", "--model", model]
    # 作業ディレクトリをリポジトリの外にして、agy がリポジトリを触る余地を無くす。
    with tempfile.TemporaryDirectory() as cwd:
        proc = run(cmd, stdin=message, timeout=timeout_min * 60 + 60, cwd=cwd)
    if proc.returncode != 0:
        tail = " / ".join(proc.stderr.strip().splitlines()[-3:])
        raise ReviewError(f"agy が異常終了しました (exit={proc.returncode}): {tail}")
    try:
        return parse_output(proc.stdout)
    except ReviewError:
        dump = WORK_DIR / "agy_raw_output.txt"
        dump.write_text(proc.stdout or "(出力なし)", encoding="utf-8")
        raise ReviewError(f"{sys.exc_info()[1]} 生出力: {dump}")


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------
def verdict_of(findings):
    return "CHANGES_REQUESTED" if any(f["severity"] in BLOCKING for f in findings) else "APPROVE"


def fence(code):
    ticks = max([len(m) for m in re.findall(r"`+", code)] + [2]) + 1
    return f"{'`' * ticks}\n{code.rstrip()}\n{'`' * ticks}"


def render(findings, sha, model, domain):
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

    print(f"agy ({args.model}) でレビュー中... 差分 {len(diff)} 文字")
    findings = call_agy(build_prompt(diff, args.domain), args.model, args.timeout)

    # レビュー中に push されていたら、古い差分の結果を新しい head に紐付けない。
    if pr_info(pr)["headRefOid"] != sha:
        raise ReviewError("レビュー中に PR の head が変わりました。再実行してください。")

    report = render(findings, sha, args.model, args.domain)
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
