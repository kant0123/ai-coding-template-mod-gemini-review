# Git ワークフロー & 完了の定義

## 常に守る基本規約

1. **main へ直接 push・直接マージしない**:
   - 必ず PR を経由し、CI の全件パス（`gh pr checks`）を確認してからマージする。
2. **実装作業はメインツリーで行わない**:
   - 複数セッションによる HEAD 衝突や作業の混ざりを防ぐため、必ず `git worktree add ../<repo>-<作業名> -b <branch> origin/main` で作成したリポジトリ外の兄弟ディレクトリで作業する。
   - ブランチ名は `fix/`, `feature/`, `chore/`, `docs/` を前置し、必ず `origin/main` から切る。
3. **`git add` はファイルを明示指定する**:
   - `git add -A` や `git add .` は禁止。他セッションの未コミット変更を巻き込まないよう、`git diff --cached` で確認してコミットする。
4. **Wiki の更新を同一 PR に同梱する**:
   - 実装とドキュメントの乖離（腐敗）を防ぐため、`wiki/` の更新と `wiki/log.md` は同一ブランチ・同一 PR に含める。
5. **worktree の安全な後始末**:
   - 作業完了後は必ずメインツリーに `cd` してから `git worktree remove` する。
   - 削除失敗時に手動で `rm -rf` / `Remove-Item` を行わない（git 管理情報が破損するため。復旧には `scripts/worktree-cleanup.ps1` を使用する）。
6. **PR / Issue での罠回避**:
   - `gh pr create --fill` は使わない（必須節が満たせないため `--body` で明示指定）。
   - `Fixes #` / `Closes #` などの自動クローズ構文を使わない（マージ時に自動クローズされると、後続の `gh issue close --comment` による要約記録が中断するため）。
   - worktree 内で `gh pr merge --delete-branch` を使わない（ローカル main への checkout 失敗がマージ失敗と誤読されるため。ブランチ削除は `git push origin --delete` で行う）。

## 完了の定義 (Definition of Done)

- 関連するローカルテストを実行し、全件パスしている。
- CI（`gh pr checks`）が全件パスしている。
- スキーマ変更がある場合、マイグレーションが含まれている。
- フロントエンド変更がある場合、ブラウザ等で動作検証を行っている。
- `wiki/` の該当ページと `wiki/log.md` を同一 PR で更新している（更新不要時は PR 本文に理由を明記）。
- Wiki 更新後、コミット状態で `node scripts/wiki-lint.js` がパスしている。
- 作業中に発見したスコープ外の問題をすべて Issue 化している。
- 変更内容の要約と手動確認結果を報告している。
