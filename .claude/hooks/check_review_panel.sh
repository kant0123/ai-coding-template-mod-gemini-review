#!/usr/bin/env bash
# [オプション] 合議制パネル — マージ差し止め hook (Claude Code / PreToolUse / matcher: Bash)
#
# `gh pr merge` を実行しようとしたときに発火し、その PR の差分にパネルを掛ける。
# Critical があれば exit 2 でツール呼び出しをブロックする。PreToolUse はコマンドの
# 実行「前」に走るため、マージは一度も実行されない。
#
# 【CI の panel ジョブがあるのに、なぜローカルにも置くのか】
# CI のジョブが赤くてもマージは止まらない — 止めているのは**ブランチ保護の必須チェック**で、
# ワークフローを置いただけでは何も守られない。保護設定はテンプレートのコピーには
# 引き継がれないため、設定漏れのプロジェクトではゲートが素通しになる。
# この hook はその穴を手元で塞ぐ多層防御であり、ブランチ保護の代わりではない。
# 両方入れること (docs/development_workflow.md 「ブランチ保護」参照)。
#
# 環境変数:
#   REVIEW_PANEL_ROOT   パネル本体を別の場所に置いている場合のルート。
#                       未設定ならリポジトリ内の review/panel_runner.py を使う。
#   REVIEW_PANEL_DOMAIN 監査ドメイン (general/fintech/distributed/healthcare/embedded)。既定 general。
#
# 回避: コマンド先頭に PANEL_SKIP=1 を付けて再実行する。
#       その場合は理由を PR 本文に一行書くこと (CLAUDE.md の規約)。
#
# 採用しない場合はこのファイルを削除し、settings.example.json の該当エントリも消す。

input=$(cat)

# コマンド文字列の取り出しは python に依存する。python が無い環境・JSON が壊れている場合は
# 生の入力をそのまま検査対象にする (フェイルクローズ)。空文字にして素通しさせると、
# 「フックを入れたのに一度も発火しない」状態に黙って落ちるため。
cmd=$(printf '%s' "$input" | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null) || cmd="$input"

# gh pr merge 以外は対象外
[[ "$cmd" == *"gh pr merge"* ]] || exit 0

# 明示的な回避
[[ "$cmd" == *"PANEL_SKIP=1"* ]] && exit 0

# --- パネルの所在を決める ---------------------------------------------------
# 未導入のプロジェクト (review/ ごと削除した場合) は素通しする。パネルの採用は
# オプションであり、未導入を異常として扱うと全マージが壊れるため。
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

runner=""
if [[ -n "$REVIEW_PANEL_ROOT" && -f "$REVIEW_PANEL_ROOT/scripts/panel_runner.py" ]]; then
    runner="$REVIEW_PANEL_ROOT/scripts/panel_runner.py"
elif [[ -f "$repo_root/review/panel_runner.py" ]]; then
    runner="$repo_root/review/panel_runner.py"
fi
[[ -n "$runner" ]] || exit 0

# --- ここから先はフェイルクローズ (黙って素通しさせない) --------------------
fail() {
    echo "合議制パネル: $1" >&2
    echo "パネルを実行できないため、マージを差し止めました。原因を解消して再実行してください。" >&2
    echo "意図的に飛ばす場合は、理由を PR 本文に一行書いた上で PANEL_SKIP=1 を付けて再実行してください。" >&2
    exit 2
}

command -v gh >/dev/null 2>&1 || fail "gh コマンドが見つかりません。"
command -v python >/dev/null 2>&1 || fail "python コマンドが見つかりません。"

# PR 番号。gh pr merge は番号・URL・ブランチ名のいずれも取り、省略も可能なので、
# コマンド文字列から抽出せず gh に引き直す。
pr=$(gh pr view --json number --jq .number 2>/dev/null)
[[ -n "$pr" ]] || fail "PR 番号を特定できませんでした。"

diff_file=$(mktemp) || fail "一時ファイルを作成できませんでした。"
json_file=$(mktemp) || fail "一時ファイルを作成できませんでした。"
trap 'rm -f "$diff_file" "$json_file"' EXIT

# レポートは残す。差し止めのメッセージで場所を案内するため、hook 終了後も読めないと困る。
# review/work/ は .gitignore 済み。
report_file="$repo_root/review/work/pr_${pr}_review.md"
mkdir -p "$repo_root/review/work" 2>/dev/null || report_file=$(mktemp)

# gh pr diff を使う。worktree の未コミット変更に影響されず、マージされる状態と一致するため。
gh pr diff "$pr" > "$diff_file" 2>/dev/null || fail "PR #$pr の差分を取得できませんでした。"
[[ -s "$diff_file" ]] || exit 0  # 差分が空なら検査対象なし

python "$runner" \
    --diff "$diff_file" \
    --domain "${REVIEW_PANEL_DOMAIN:-general}" \
    --task-id "pr_${pr}" \
    --output "$report_file" \
    --json "$json_file" >/dev/null 2>&1 || fail "パネルの実行に失敗しました。"

python - "$json_file" "$report_file" >&2 <<'PY'
import json
import sys

triage_path, report = sys.argv[1], sys.argv[2]
with open(triage_path, encoding="utf-8") as f:
    t = json.load(f)


def render(items):
    for item in items:
        loc = ", ".join(item.get("locations", [])) or "(位置情報なし)"
        print(f"  - [{item.get('category')}] {item.get('issue')}")
        print(f"      該当: {loc}")


if t["critical_count"]:
    print(f"合議制パネルが Critical を {t['critical_count']} 件検出したため、マージを差し止めました。")
    render(t["criticals"])
    if t["warning_count"]:
        print(f"あわせて Warning が {t['warning_count']} 件あります (こちらはマージを止めません)。")
        render(t["warnings"])
    print(f"詳細レポート: {report}")
    print("")
    print("対応方針:")
    print("  1. worktree で修正し、push して CI を通してから再度マージしてください。")
    print("  2. 指摘が的外れだと判断した場合は、理由を PR 本文に一行書いた上で、")
    print("     コマンド先頭に PANEL_SKIP=1 を付けて再実行してください。")
    print("     あわせて偽陽性としてパネル側に起票してください:")
    print("       gh issue create --repo kant0123/gemini-review --label false-positive \\")
    print("         --title \"<誤検知の一文>\" --body \"<指摘カテゴリ / 該当コード / なぜ的外れか>\"")
    print("     起票しないと同じ誤検知が全プロジェクトで再発します。")
    sys.exit(2)

if t["warning_count"]:
    print(f"合議制パネル: Warning {t['warning_count']} 件 (マージは止めません)。")
    render(t["warnings"])
    print(f"詳細レポート: {report}")
    print("今の PR で直すか、起票して先に進むか、見送るかを判断してください。")
    print("見送る場合は理由を PR 本文に一行書いてください。")

sys.exit(0)
PY
exit $?
