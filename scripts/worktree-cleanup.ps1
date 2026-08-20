<#
.SYNOPSIS
  worktree を安全に削除し、過去に失敗して残った孤立エントリを掃除する（Windows 専用）。

.DESCRIPTION
  ディレクトリに ReadOnly 属性が付いていると、Windows の RemoveDirectory は ACCESS_DENIED を返し、
  `git worktree remove` が Permission denied で失敗する。失敗すると git は管理情報の切り離しにだけ
  成功するので、.git/worktrees/<id> と作業ディレクトリがディスク上に取り残される。しかも残った
  メタデータにも ReadOnly が付いていると、以後 `git worktree prune` も同じ場所で失敗し続ける。

  ReadOnly を付けてくる典型はクラウドストレージのミラー同期（Google ドライブ / OneDrive 等）や
  バックアップツールで、リポジトリの親ディレクトリを同期対象にしていると起きる。同期を外せば
  通常は素の `git worktree remove` で足りるが、同じ状態に陥ったときの復旧手段として用意してある。

  -Remove を付けると、ReadOnly を外してから `git worktree remove` を実行する（= 失敗させない）。
  引数なしで実行すると、既に取り残されている孤立エントリの掃除だけを行う。

  削除そのものは常に git（`git worktree remove` / `git worktree prune`）に任せ、本スクリプトは
  属性を外すだけに留める。稼働中の worktree を判定ミスで消さないため。

  このファイルは UTF-8 (BOM 付き) で保存すること。BOM が無いと Windows PowerShell 5.1 が
  CP932 として読み、日本語が化けてパースエラーで 1 行も実行されない。

.PARAMETER Remove
  削除する worktree のパス。ReadOnly を外してから `git worktree remove` を実行する。

.PARAMETER RemoveDirs
  git が把握していない作業ディレクトリの残骸も削除する。未コミットの変更ごと消えるので、
  中身を確認してから付けること。

.PARAMETER DryRun
  対象を表示するだけで、実際には何も変更しない。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\worktree-cleanup.ps1 -Remove ..\<repo>-<作業名>

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\worktree-cleanup.ps1 -RemoveDirs
#>

param(
  [string]$Remove,
  [switch]$RemoveDirs,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ReadOnly を再帰的に外す。これが付いている限り git 側の rmdir は必ず EACCES になる。
function Clear-ReadOnlyRecurse {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $items = @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse)
  foreach ($item in $items) {
    if ($item.Attributes -band [IO.FileAttributes]::ReadOnly) {
      $item.Attributes = $item.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly)
    }
  }
}

# --- リポジトリの位置を掴む -------------------------------------------------
# worktree の中から実行されてもメインの .git を指すよう --git-common-dir を使う。
$commonDir = & git rev-parse --path-format=absolute --git-common-dir
if ($LASTEXITCODE -ne 0) {
  Write-Error 'git リポジトリの中で実行してください。'
  exit 1
}
$commonDir = ($commonDir | Select-Object -First 1).Trim()
$worktreesDir = Join-Path $commonDir 'worktrees'

# 稼働中の worktree（メインツリーを含む）。孤立判定はここに載っていないことが前提になる。
$live = @()
$mainRoot = $null
foreach ($line in (& git worktree list --porcelain)) {
  if ($line -match '^worktree (.+)$') {
    $path = (Resolve-Path -LiteralPath $Matches[1]).Path
    if (-not $mainRoot) { $mainRoot = $path }
    $live += $path
  }
}

$failed = $false

# --- 0. -Remove: ReadOnly を外してから git worktree remove ------------------
if ($Remove) {
  Write-Host "== worktree の削除: $Remove =="
  if (-not (Test-Path -LiteralPath $Remove)) {
    Write-Error "$Remove が見つかりません。"
    exit 1
  }
  $target = (Resolve-Path -LiteralPath $Remove).Path
  if ($live -notcontains $target) {
    Write-Error "$target は稼働中の worktree ではありません（git worktree list に無い）。孤立の掃除は引数なしで実行する。"
    exit 1
  }
  if ($target -eq $mainRoot) {
    Write-Error 'メインツリーは削除できません。'
    exit 1
  }
  # .git は `gitdir: <メタデータのパス>` の 1 行。メタデータ側にも ReadOnly が付く。
  $metaDir = $null
  $dotGit = (Get-Content -LiteralPath (Join-Path $target '.git') -Raw).Trim()
  if ($dotGit -match '^gitdir:\s*(.+)$') { $metaDir = $Matches[1].Trim() }

  if ($DryRun) {
    Write-Host "  (DryRun) ReadOnly を外して git worktree remove する: $target"
  } else {
    Clear-ReadOnlyRecurse -Path $target
    if ($metaDir) { Clear-ReadOnlyRecurse -Path $metaDir }
    & git worktree remove $target
    if ($LASTEXITCODE -ne 0) {
      Write-Warning '削除に失敗した。残骸は下の掃除で拾う。'
      $failed = $true
    } else {
      Write-Host "  削除した: $target"
      $live = @($live | Where-Object { $_ -ne $target })
    }
  }
  Write-Host ''
}

# --- 1. .git/worktrees/ の孤立エントリ --------------------------------------
Write-Host '== .git/worktrees/ の孤立エントリ =='
$orphans = @()
if (Test-Path -LiteralPath $worktreesDir) {
  foreach ($entry in Get-ChildItem -LiteralPath $worktreesDir -Force -Directory) {
    $gitdirFile = Join-Path $entry.FullName 'gitdir'
    $target = ''
    if (Test-Path -LiteralPath $gitdirFile) { $target = (Get-Content -LiteralPath $gitdirFile -Raw).Trim() }
    # gitdir が無い / 指す先が無い = git prune が消したがるエントリ。判定条件は git 本体と同じ。
    if (-not $target -or -not (Test-Path -LiteralPath $target)) { $orphans += $entry }
  }
}

if ($orphans.Count -eq 0) {
  Write-Host '  なし'
} else {
  foreach ($entry in $orphans) { Write-Host "  $($entry.Name)" }
  if ($DryRun) {
    Write-Host '  (DryRun) ReadOnly の解除と prune は行わない'
  } else {
    foreach ($entry in $orphans) { Clear-ReadOnlyRecurse -Path $entry.FullName }
    & git worktree prune -v
    $left = @()
    if (Test-Path -LiteralPath $worktreesDir) {
      $left = @(Get-ChildItem -LiteralPath $worktreesDir -Force -Directory |
        Where-Object { $orphans.Name -contains $_.Name })
    }
    if ($left.Count -gt 0) {
      Write-Warning "prune 後も残ったエントリ: $($left.Name -join ', ')"
      $failed = $true
    } else {
      Write-Host "  $($orphans.Count) 件を削除した"
    }
  }
}

# --- 2. 作業ディレクトリ側の残骸 --------------------------------------------
Write-Host ''
Write-Host '== git が把握していない作業ディレクトリの残骸 =='
$prefix = (Split-Path -Leaf $mainRoot) + '-'
$stale = @()
foreach ($dir in Get-ChildItem -LiteralPath (Split-Path -Parent $mainRoot) -Force -Directory) {
  if (-not $dir.Name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
  if ($live -contains $dir.FullName) { continue }
  # worktree のチェックアウトは .git が「ファイル」。通常のクローンや無関係な兄弟を巻き込まない。
  if (-not (Test-Path -LiteralPath (Join-Path $dir.FullName '.git') -PathType Leaf)) { continue }
  $stale += $dir
}

if ($stale.Count -eq 0) {
  Write-Host '  なし'
} elseif (-not $RemoveDirs) {
  foreach ($dir in $stale) { Write-Host "  $($dir.FullName)" }
  Write-Host '  未コミットの変更が残っている可能性があるため既定では削除しない。'
  Write-Host '  中身を確認して問題なければ -RemoveDirs を付けて再実行する。'
} else {
  foreach ($dir in $stale) {
    if ($DryRun) {
      Write-Host "  (DryRun) 削除する: $($dir.FullName)"
      continue
    }
    Clear-ReadOnlyRecurse -Path $dir.FullName
    Remove-Item -LiteralPath $dir.FullName -Recurse -Force
    Write-Host "  削除した: $($dir.FullName)"
  }
}

if ($failed) { exit 1 }
