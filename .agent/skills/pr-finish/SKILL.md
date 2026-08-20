---
name: pr-finish
description: >
  実装が終わってから PR を作り、CI を通し、マージして後始末するまでの完了手順。
  テスト実行、push、gh pr create の本文テンプレート、CI 確認、マージ、
  リモートブランチと worktree の削除、Issue クローズ、デプロイの反映確認までを一続きで行う。
  「PR を作って」「マージして」「終わらせて」と言われたときや、
  worktree での実装と wiki 更新が済んだときに使う。
  --fill / Fixes / --delete-branch を使わない理由など、素直にやると失敗する罠がまとまっている。
---

# 完了 — PR から反映確認まで

> **このファイルはテンプレートです。** `<...>` を実プロジェクトの値に置き換え、
> 採用していない仕組み(Wiki / Issue 連携 / 本番同居 CD)の節は削除してから使う。
> Claude Code で使う場合は `.claude/skills/pr-finish/SKILL.md` に置く。

`gh` の既定の使い方(`--fill`、`Fixes #`、`--delete-branch`)は**worktree ベースの運用では
順に壊れる**。理由は各手順に書いた。

## 前提の確認

始める前に、worktree の中にいて、実装と Wiki 更新が済んでいること。

```bash
<テストコマンド>
git status
```

- `git status` は**他セッションの変更が混ざっていないか**を見るため。混ざっていたら
  自分の変更だけをファイル明示で `git add` する(`-A` / `.` は使わない)。
- スキーマ変更があるなら、マイグレーションが含まれていて既存 DB がそのまま起動できることを確認する。
- フロントを触ったなら実際に起動してブラウザで動作確認する。

## 1. push

```bash
git push -u origin <branch>
```

## 2. PR を作る

```bash
gh pr create --title "<一文>" --body "<下記テンプレート>"
```

**`--fill` は使わない。** 本文をコミットメッセージから生成するため、規約が要求する
「対応 Issue」「Wiki」の節を満たせない。`--body` で明示する。

```markdown
## 対応 Issue

#<番号>

## 変更内容

<要約>

## Wiki

<更新したページ、または更新不要と判断した理由を一行>
```

- **`Fixes #<番号>` / `Closes #<番号>` を書かない。** クロージングキーワードがあると
  マージ時に GitHub が Issue を自動クローズし、後段の `gh issue close --comment` が
  `already closed` で失敗する。コマンド全体が中断するので `--comment` も投稿されず、
  **実装内容の要約が Issue に残らない**。素の `#<番号>` 参照に留める。
- デプロイスクリプトを変更したなら、差分の説明も本文に含める(本番プロセスの
  再起動・ロールバック挙動に直結するため)。
- `wiki/` に差分が無いと PreToolUse hook がブロックする。意図的なら理由を本文に書いた上で
  `WIKI_SKIP=1 gh pr create ...` で再実行する。

PR 番号が決まったら、`wiki/log.md` のエントリに `PR #<番号>` を埋めて追いコミット・push する。

## 3. CI を待つ

```bash
gh pr checks <PR番号> --watch
```

失敗したらマージせず原因を調べる。修正が困難なら PR は開いたまま状況を報告する
(勝手に close しない)。

## 4. マージ

```bash
gh pr merge <PR番号> --merge
```

**`--delete-branch` を付けない。** worktree 内で実行すると `gh` がマージ後に `main` へ
checkout しようとして失敗する(メインツリーが `main` を持っているため):

```
failed to run git: fatal: 'main' is already used by worktree at '...'
```

**このエラーが出ていてもマージ自体は成功している。** エラー終了のためリモートブランチ削除に
到達しないだけ。エラーメッセージを見てマージ失敗と誤読しないこと。確認は:

```bash
gh pr view <PR番号> --json state,mergedAt
```

## 5. 後始末

```bash
git push origin --delete <branch>
cd <メインツリーのパス>
git worktree remove ../<repo>-<作業名>
```

**`git worktree remove` はメインツリーに `cd` してから実行する。** シェルの作業ディレクトリが
削除対象の中にあると、Windows はプロセスのカレントディレクトリを削除できず必ず失敗する。
`../<repo>-<作業名>` は **worktree の中からでも自分自身に解決してしまう**ので、
パスを見ただけでは気づけない。

失敗したら**手で `rm -rf` / `Remove-Item` しない。** 一度失敗すると git はその worktree を
working tree と認識しなくなり、`--force` でも `fatal: ... is not a working tree` で回復しない。
掃除スクリプトを使う(Windows):

```bash
powershell -ExecutionPolicy Bypass -File scripts/worktree-cleanup.ps1
```

`-RemoveDirs` は**未コミットの変更ごと消える**ので、中身を確認してから付ける。
ディレクトリに ReadOnly 属性が付く環境(クラウドストレージのミラー同期・バックアップ
ツールが親ディレクトリを掴んでいる場合など)では、素の `git worktree remove` は
Permission denied で失敗し、以後 `git worktree prune` も同じ場所で失敗し続ける。

## 6. Issue をクローズする

```bash
gh issue close <番号> --comment "<実装内容の要約>"
```

既にクローズ済みだった場合は `gh issue comment <番号> --body "<要約>"` で要約だけ残す。
**クローズの成否にかかわらず、要約は必ず Issue に残す。**

## 7. デプロイの反映を確認する(本番同居 CD を採用している場合)

**`git pull` で確認しない。** メインツリーの HEAD を前進させるのは CD の役目で、
先に進めると CD が「更新なし」と判断して本番プロセスが古いコードのまま取り残される。
ローカル main が `origin/main` より後ろにいるのは、デプロイ待ちの正常な状態。

次のいずれかで見る。

```bash
curl <ヘルスチェック URL>/healthz
```

`commit`(稼働中コミットの short SHA)がマージ後のものになっていれば反映済み。

`deploy` ワークフローの job summary / `::notice::deploy result:` 行に出る `status` でもよい
(`deployed` / `no-op` / `drift` 等。意味は `docs/development_workflow.md`)。
`logs/deploy-history.jsonl`(git 管理外)にも 1 行ずつ残る。

**デプロイスクリプト自身の変更は 1 デプロイ遅れて効く。** ワークフローが実行するのは
メインツリー上の(= まだ更新前の)スクリプトのため。変更した PR のデプロイ結果を
新ロジックのものと読み違えないこと。逆に**ワークフロー YAML は main から読まれるので
マージした瞬間に効く**。

## 事前承認の範囲

push・PR 作成・マージ・ブランチ削除・worktree 削除は `CLAUDE.md` が事前承認しており、
都度の確認は不要。CI 成功を確認したら、マージしてよいかを改めて聞かない。

ただし**タスクが失敗・中断した場合は勝手にマージ / 削除せず、状況を報告する。**
worktree も残す(作業内容を失わないため)。

## 完了報告に含めるもの

- 変更内容の要約と、手動確認の結果
- 作業中に見つけてこの PR で直さないと決めた問題を Issue 化したなら、その番号
  (1 件も無ければ、確認済みであること自体を述べる)
