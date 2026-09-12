#!/usr/bin/env bash
# このテンプレートを新しいプロジェクトに適用するための対話セットアップ。
# CI/CD をどこまで導入するかを選び、.github/workflows-optional/ から
# .github/workflows/ へ必要なファイルだけコピーする。
# 既存プロジェクトに導入する場合も安全に動作する（既存ファイルの上書き前に確認）。
set -euo pipefail
cd "$(dirname "$0")/.."

ask() {
    local prompt="$1" default="$2" ans
    read -r -p "$prompt [$default] " ans || true
    echo "${ans:-$default}"
}

# 既存ファイルがある場合にバックアップを作成する。
# 戻り値: 0=続行, 1=スキップ
safe_check() {
    local filepath="$1" label="$2"
    if [[ -f "$filepath" ]]; then
        local ans
        ans=$(ask "  ⚠ $filepath が既に存在します。上書きしますか? (y=上書き/n=スキップ)" "n")
        if [[ "$ans" != "y" ]]; then
            echo "  -> $label: 既存ファイルを維持しました。"
            return 1
        fi
        cp "$filepath" "${filepath}.bak"
        echo "  -> バックアップ: ${filepath}.bak"
    fi
    return 0
}

echo "=== エージェントルール汎用テンプレート セットアップ ==="

# --- .gitignore ---
if [[ -f ".gitignore" ]]; then
    echo ""
    echo "[.gitignore] 既存の .gitignore を検出しました。テンプレートの内容を追記します。"
    # テンプレートの各行を既存ファイルに追記（重複行はスキップ）
    template_lines=(".claude/settings.local.json" ".claude/worktrees/" "__pycache__/" "*.pyc" ".env")
    added=0
    for line in "${template_lines[@]}"; do
        if ! grep -qxF "$line" .gitignore; then
            echo "$line" >> .gitignore
            ((added++))
        fi
    done
    if [[ $added -gt 0 ]]; then
        echo "  -> $added 行を追記しました。"
    else
        echo "  -> 追記不要（すべて含まれています）。"
    fi
else
    echo ""
    echo "[.gitignore] 新規作成します。"
fi

# --- CI ---
echo ""
ci=$(ask "CI(push/PR で自動テスト実行)を導入しますか? (y/n)" "y")
if [[ "$ci" == "y" ]]; then
    if [[ -f ".github/workflows/test.yml" ]]; then
        echo "  -> .github/workflows/test.yml は既に存在します。既存ファイルを維持します。"
        echo "     テンプレートの test.yml を参考にしたい場合は .github/workflows-optional/ を参照してください。"
    else
        echo "  -> .github/workflows/test.yml は導入済みです。テストコマンドを確認してください。"
    fi
else
    if [[ -f ".github/workflows/test.yml" ]]; then
        ans=$(ask "  ⚠ 既存の .github/workflows/test.yml を削除しますか? (y/n)" "n")
        if [[ "$ans" == "y" ]]; then
            rm -f .github/workflows/test.yml
            echo "  -> .github/workflows/test.yml を削除しました。"
        else
            echo "  -> 既存の test.yml を維持しました。"
        fi
    fi
fi

# --- CD ---
echo ""
cd_choice=$(ask "CD(self-hosted runner での自動デプロイ)を導入しますか? (y/n)" "n")
if [[ "$cd_choice" == "y" ]]; then
    mkdir -p .github/workflows deploy
    if safe_check ".github/workflows/deploy.yml" "deploy.yml"; then
        cp .github/workflows-optional/deploy.yml .github/workflows/deploy.yml
        echo "  -> .github/workflows/deploy.yml を作成しました。"
    fi
    if safe_check "deploy/auto_deploy.ps1" "auto_deploy.ps1"; then
        cp deploy/auto_deploy.ps1.example deploy/auto_deploy.ps1
        echo "  -> deploy/auto_deploy.ps1 を作成しました。"
    fi
    wd_choice=$(ask "  死活監視(watchdog)も導入しますか? (y/n)" "y")
    if [[ "$wd_choice" == "y" ]] && safe_check "deploy/watchdog.ps1" "watchdog.ps1"; then
        cp deploy/watchdog.ps1.example deploy/watchdog.ps1
        echo "  -> deploy/watchdog.ps1 を作成しました(タスクスケジューラ/cron へ登録してください)。"
    fi
    echo "     deploy/auto_deploy.ps1 のプレースホルダー関数を実プロジェクトに合わせて編集してください。"
    echo "     アプリの /healthz に稼働中コミットの short SHA を出す対応も必要です(deploy/README.md)。"
    echo "     .ps1 は UTF-8 (BOM 付き) で保存してください(BOM 無しは 5.1 でパースエラーになります)。"
    echo "     リポジトリ変数 SELF_HOSTED_DEPLOY を true にするまでは常にスキップされます。"
    echo "     CLAUDE.md の「[オプション] 本番同居チェックアウトの追加ルール」を有効化してください。"
fi

# --- label-hygiene ---
echo ""
label_choice=$(ask "GitHub Issues/Projects 連携の label-hygiene ワークフローを導入しますか? (y/n)" "n")
if [[ "$label_choice" == "y" ]]; then
    mkdir -p .github/workflows
    if safe_check ".github/workflows/label-hygiene.yml" "label-hygiene.yml"; then
        cp .github/workflows-optional/label-hygiene.yml .github/workflows/label-hygiene.yml
        echo "  -> .github/workflows/label-hygiene.yml を作成しました。ラベル名を確認してください。"
    fi
fi

# --- wiki-check ---
echo ""
wikicheck_choice=$(ask "PR に wiki/ 更新が含まれるかを確認する wiki-check ワークフローを導入しますか? (y/n)" "n")
if [[ "$wikicheck_choice" == "y" ]]; then
    mkdir -p .github/workflows
    if safe_check ".github/workflows/wiki-check.yml" "wiki-check.yml"; then
        cp .github/workflows-optional/wiki-check.yml .github/workflows/wiki-check.yml
        echo "  -> .github/workflows/wiki-check.yml を作成しました。既定は警告のみで CI は落ちません。"
    fi
fi

# --- wiki-lint ---
echo ""
wikilint_choice=$(ask "wiki/ の整合性を機械チェックする wiki-lint ワークフローを導入しますか?(Wiki を使うなら推奨) (y/n)" "n")
if [[ "$wikilint_choice" == "y" ]]; then
    mkdir -p .github/workflows
    if safe_check ".github/workflows/wiki-lint.yml" "wiki-lint.yml"; then
        cp .github/workflows-optional/wiki-lint.yml .github/workflows/wiki-lint.yml
        echo "  -> .github/workflows/wiki-lint.yml を作成しました。scripts/wiki-lint.js が必要です(Node.js)。"
        echo "     テストのワークフローには相乗りさせないこと(CD が止まります)。"
    fi
fi

# --- agy レビュー ---
echo ""
review_choice=$(ask "agy レビュー(CI 緑の後に差分を Gemini でレビュー。agy が必要)を使いますか? (y/n)" "y")
if [[ "$review_choice" == "y" ]]; then
    echo "  -> review/ ・agy-review スキル・merge hook を維持します。"
    echo "     agy をインストール・ログインし、PATH に通してください。"
    echo "     ドメインは環境変数 REVIEW_DOMAIN で指定します"
    echo "     (general / fintech / distributed / healthcare / embedded。未設定なら general)。"
else
    rm -rf review .agent/skills/agy-review .claude/skills/agy-review
    rm -f .claude/hooks/check_agy_review.sh
    echo "  -> review/ ・スキル・hook を削除しました。"
    echo "     CLAUDE.md の「[オプション] agy レビュー」節、pr-finish スキルの"
    echo "     「4. agy レビュー」手順、settings.example.json の該当 hook も削除してください。"
fi

# --- ナレッジ Wiki ---
echo ""
wiki_choice=$(ask "ナレッジ Wiki(wiki/)を使いますか? (y/n)" "y")
if [[ "$wiki_choice" != "y" ]]; then
    if [[ -d "wiki" ]]; then
        ans=$(ask "  ⚠ wiki/ を削除しますか? (y/n)" "n")
        if [[ "$ans" == "y" ]]; then
            rm -rf wiki
            echo "  -> wiki/ を削除しました。CLAUDE.md の「ナレッジ Wiki」節と docs/wiki_workflow.md も削除してください。"
        else
            echo "  -> wiki/ を維持しました。"
        fi
    fi
else
    echo "  -> wiki/ を使います。まず wiki/overview.md を書いてください。"
    echo "     既存プロジェクトへの導入手順は docs/wiki_workflow.md を参照。"
fi

# --- テンプレート自身の検査ワークフロー(コピー先には不要) ---
if [[ -f ".github/workflows/selfcheck.yml" ]]; then
    rm -f .github/workflows/selfcheck.yml
    echo ""
    echo "  -> .github/workflows/selfcheck.yml を削除しました(テンプレート自身の検査用のため)。"
fi

echo ""
echo "セットアップ完了。次のファイルのプレースホルダーを埋めてください:"
echo "  - CLAUDE.md"
echo "  - docs/development_workflow.md"
[[ "$wiki_choice" == "y" ]] && echo "  - wiki/overview.md"
