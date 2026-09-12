# このテンプレートを新しいプロジェクトに適用するための対話セットアップ。
# CI/CD をどこまで導入するかを選び、.github/workflows-optional/ から
# .github/workflows/ へ必要なファイルだけコピーする。
# 既存プロジェクトに導入する場合も安全に動作する（既存ファイルの上書き前に確認）。

Set-Location (Split-Path -Parent $PSScriptRoot)

function Ask([string]$Prompt, [string]$Default) {
    $ans = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($ans)) { return $Default }
    return $ans
}

# 既存ファイルがある場合にバックアップを作成する。
# 戻り値: $true=続行, $false=スキップ
function SafeCheck([string]$FilePath, [string]$Label) {
    if (Test-Path $FilePath) {
        $ans = Ask "  ⚠ $FilePath が既に存在します。上書きしますか? (y=上書き/n=スキップ)" "n"
        if ($ans -ne "y") {
            Write-Host "  -> ${Label}: 既存ファイルを維持しました。"
            return $false
        }
        Copy-Item $FilePath "${FilePath}.bak" -Force
        Write-Host "  -> バックアップ: ${FilePath}.bak"
    }
    return $true
}

Write-Host "=== エージェントルール汎用テンプレート セットアップ ===" -ForegroundColor Cyan

# --- .gitignore ---
if (Test-Path ".gitignore") {
    Write-Host ""
    Write-Host "[.gitignore] 既存の .gitignore を検出しました。テンプレートの内容を追記します。"
    $templateLines = @(".claude/settings.local.json", ".claude/worktrees/", "__pycache__/", "*.pyc", ".env")
    $existing = Get-Content ".gitignore" -ErrorAction SilentlyContinue
    $added = 0
    foreach ($line in $templateLines) {
        if ($existing -notcontains $line) {
            Add-Content ".gitignore" $line
            $added++
        }
    }
    if ($added -gt 0) {
        Write-Host "  -> $added 行を追記しました。"
    } else {
        Write-Host "  -> 追記不要（すべて含まれています）。"
    }
} else {
    Write-Host ""
    Write-Host "[.gitignore] 新規作成します。"
}

# --- CI ---
Write-Host ""
$ci = Ask "CI(push/PR で自動テスト実行)を導入しますか? (y/n)" "y"
if ($ci -eq "y") {
    if (Test-Path ".github/workflows/test.yml") {
        Write-Host "  -> .github/workflows/test.yml は既に存在します。既存ファイルを維持します。"
        Write-Host "     テンプレートの test.yml を参考にしたい場合は .github/workflows-optional/ を参照してください。"
    } else {
        Write-Host "  -> .github/workflows/test.yml は導入済みです。テストコマンドを確認してください。"
    }
} else {
    if (Test-Path ".github/workflows/test.yml") {
        $ans = Ask "  ⚠ 既存の .github/workflows/test.yml を削除しますか? (y/n)" "n"
        if ($ans -eq "y") {
            Remove-Item -Force ".github/workflows/test.yml"
            Write-Host "  -> .github/workflows/test.yml を削除しました。"
        } else {
            Write-Host "  -> 既存の test.yml を維持しました。"
        }
    }
}

# --- CD ---
Write-Host ""
$cdChoice = Ask "CD(self-hosted runner での自動デプロイ)を導入しますか? (y/n)" "n"
if ($cdChoice -eq "y") {
    New-Item -ItemType Directory -Force -Path ".github/workflows", "deploy" | Out-Null
    if (SafeCheck ".github/workflows/deploy.yml" "deploy.yml") {
        Copy-Item ".github/workflows-optional/deploy.yml" ".github/workflows/deploy.yml" -Force
        Write-Host "  -> .github/workflows/deploy.yml を作成しました。"
    }
    if (SafeCheck "deploy/auto_deploy.ps1" "auto_deploy.ps1") {
        Copy-Item "deploy/auto_deploy.ps1.example" "deploy/auto_deploy.ps1" -Force
        Write-Host "  -> deploy/auto_deploy.ps1 を作成しました。"
    }
    $wdChoice = Ask "  死活監視(watchdog)も導入しますか? (y/n)" "y"
    if ($wdChoice -eq "y" -and (SafeCheck "deploy/watchdog.ps1" "watchdog.ps1")) {
        Copy-Item "deploy/watchdog.ps1.example" "deploy/watchdog.ps1" -Force
        Write-Host "  -> deploy/watchdog.ps1 を作成しました(タスクスケジューラ/cron へ登録してください)。"
    }
    Write-Host "     deploy/auto_deploy.ps1 のプレースホルダー関数を実プロジェクトに合わせて編集してください。"
    Write-Host "     アプリの /healthz に稼働中コミットの short SHA を出す対応も必要です(deploy/README.md)。"
    Write-Host "     .ps1 は UTF-8 (BOM 付き) で保存してください(BOM 無しは 5.1 でパースエラーになります)。"
    Write-Host "     リポジトリ変数 SELF_HOSTED_DEPLOY を true にするまでは常にスキップされます。"
    Write-Host "     CLAUDE.md の「[オプション] 本番同居チェックアウトの追加ルール」を有効化してください。"
}

# --- label-hygiene ---
Write-Host ""
$labelChoice = Ask "GitHub Issues/Projects 連携の label-hygiene ワークフローを導入しますか? (y/n)" "n"
if ($labelChoice -eq "y") {
    New-Item -ItemType Directory -Force -Path ".github/workflows" | Out-Null
    if (SafeCheck ".github/workflows/label-hygiene.yml" "label-hygiene.yml") {
        Copy-Item ".github/workflows-optional/label-hygiene.yml" ".github/workflows/label-hygiene.yml" -Force
        Write-Host "  -> .github/workflows/label-hygiene.yml を作成しました。ラベル名を確認してください。"
    }
}

# --- wiki-check ---
Write-Host ""
$wikiCheckChoice = Ask "PR に wiki/ 更新が含まれるかを確認する wiki-check ワークフローを導入しますか? (y/n)" "n"
if ($wikiCheckChoice -eq "y") {
    New-Item -ItemType Directory -Force -Path ".github/workflows" | Out-Null
    if (SafeCheck ".github/workflows/wiki-check.yml" "wiki-check.yml") {
        Copy-Item ".github/workflows-optional/wiki-check.yml" ".github/workflows/wiki-check.yml" -Force
        Write-Host "  -> .github/workflows/wiki-check.yml を作成しました。既定は警告のみで CI は落ちません。"
    }
}

# --- wiki-lint ---
Write-Host ""
$wikiLintChoice = Ask "wiki/ の整合性を機械チェックする wiki-lint ワークフローを導入しますか?(Wiki を使うなら推奨) (y/n)" "n"
if ($wikiLintChoice -eq "y") {
    New-Item -ItemType Directory -Force -Path ".github/workflows" | Out-Null
    if (SafeCheck ".github/workflows/wiki-lint.yml" "wiki-lint.yml") {
        Copy-Item ".github/workflows-optional/wiki-lint.yml" ".github/workflows/wiki-lint.yml" -Force
        Write-Host "  -> .github/workflows/wiki-lint.yml を作成しました。scripts/wiki-lint.js が必要です(Node.js)。"
        Write-Host "     テストのワークフローには相乗りさせないこと(CD が止まります)。"
    }
}

# --- agy レビュー ---
Write-Host ""
$reviewChoice = Ask "agy レビュー(CI 緑の後に差分を Gemini でレビュー。agy が必要)を使いますか? (y/n)" "y"
if ($reviewChoice -eq "y") {
    Write-Host "  -> review/ ・agy-review スキル・merge hook を維持します。"
    Write-Host "     agy をインストール・ログインし、PATH に通してください。"
    Write-Host "     ドメインは環境変数 REVIEW_DOMAIN で指定します"
    Write-Host "     (general / fintech / distributed / healthcare / embedded。未設定なら general)。"
} else {
    foreach ($p in @("review", ".agent/skills/agy-review", ".claude/skills/agy-review", ".claude/hooks/check_agy_review.sh")) {
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
    Write-Host "  -> review/ ・スキル・hook を削除しました。"
    Write-Host "     CLAUDE.md の「[オプション] agy レビュー」節、pr-finish スキルの"
    Write-Host "     「4. agy レビュー」手順、settings.example.json の該当 hook も削除してください。"
}

# --- ナレッジ Wiki ---
Write-Host ""
$wikiChoice = Ask "ナレッジ Wiki(wiki/)を使いますか? (y/n)" "y"
if ($wikiChoice -ne "y") {
    if (Test-Path "wiki") {
        $ans = Ask "  ⚠ wiki/ を削除しますか? (y/n)" "n"
        if ($ans -eq "y") {
            Remove-Item -Recurse -Force "wiki"
            Write-Host "  -> wiki/ を削除しました。CLAUDE.md の「ナレッジ Wiki」節と docs/wiki_workflow.md も削除してください。"
        } else {
            Write-Host "  -> wiki/ を維持しました。"
        }
    }
} else {
    Write-Host "  -> wiki/ を使います。まず wiki/overview.md を書いてください。"
    Write-Host "     既存プロジェクトへの導入手順は docs/wiki_workflow.md を参照。"
}

# --- テンプレート自身の検査ワークフロー(コピー先には不要) ---
if (Test-Path ".github/workflows/selfcheck.yml") {
    Remove-Item -Force ".github/workflows/selfcheck.yml"
    Write-Host ""
    Write-Host "  -> .github/workflows/selfcheck.yml を削除しました(テンプレート自身の検査用のため)。"
}

Write-Host ""
Write-Host "セットアップ完了。次のファイルのプレースホルダーを埋めてください:" -ForegroundColor Cyan
Write-Host "  - CLAUDE.md"
Write-Host "  - docs/development_workflow.md"
if ($wikiChoice -eq "y") { Write-Host "  - wiki/overview.md" }
