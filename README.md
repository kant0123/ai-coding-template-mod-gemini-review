# エージェントルール汎用テンプレート

AI コーディングエージェント(Claude Code 等)と長期間・複数セッションで並行開発するための
プロジェクト初期設定テンプレート。実運用(本番同居チェックアウト・self-hosted CD・
GitHub Issues 自動連携を含む個人開発プロジェクト)で発生した事故と、その再発防止策を
汎用化して収録している。

このテンプレート自身のリポジトリ: <https://github.com/kant0123/ai-coding-template>

### 対応エージェント

主に **Claude Code** を想定して書かれているが、Git ワークフロー・ナレッジ Wiki・
CI/CD の仕組みはエージェントに依存しない汎用部分。

- **Claude Code** — そのまま使える(`CLAUDE.md` / `.claude/` をそのまま利用)。
  ただし**スキルは `.agent/skills/` から `.claude/skills/` へコピーする**
  (テンプレートはツール中立のため `.agent/` に置いてある。Claude Code が読むのは
  `.claude/skills/<name>/SKILL.md`)。
- **Gemini CLI / Jules** — `CLAUDE.md` → `GEMINI.md`、`.claude/` → `.agents/` に
  読み替えれば同等に機能する。`.agent/skills/agent-config-manager/` の対応表も参照。
- **Cursor / Windsurf / Cline** — Git ワークフロー・CI/CD・`wiki/` はそのまま流用可能。
  エージェント指示ファイルは各ツールの形式(`.cursorrules` 等)に移植する。

## 何が入っているか

| ファイル/ディレクトリ | 役割 | 必須/オプション |
| --- | --- | --- |
| [CLAUDE.md](CLAUDE.md) | エージェントの応答方針・ナレッジ Wiki 規約・Git ワークフロー・完了の定義 | 必須(中身を編集) |
| [wiki/](wiki/index.md) | 蓄積型ナレッジベースの骨組み(`index.md` / `log.md` / `overview.md` + 3 種のページ雛形) | 推奨 |
| [.agent/skills/worktree-start/](.agent/skills/worktree-start/SKILL.md) | 着手手順(Wiki を読む → Issue 確保 → `origin/main` から worktree) | 推奨 |
| [.agent/skills/wiki-ingest/](.agent/skills/wiki-ingest/SKILL.md) | 実装後に変更を `wiki/` へ反映する手順(Wiki を採用する場合) | 推奨 |
| [.agent/skills/pr-finish/](.agent/skills/pr-finish/SKILL.md) | 完了手順(PR → CI → マージ → 後始末 → Issue クローズ → 反映確認)。`--fill` / `Fixes` / `--delete-branch` の罠つき | 推奨 |
| [.agent/skills/agy-review/](.agent/skills/agy-review/SKILL.md) | CI 緑の後に差分を agy (Gemini) でレビューし、差し戻しを評価して修正ループ / 誤検知の起票に振り分ける手順 | オプション |
| [review/](review/README.md) | agy レビューの実体(レビュー実行スクリプト・プロンプト・5 ドメインの不変条件定義) | オプション |
| [.agent/skills/agent-config-manager/](.agent/skills/agent-config-manager/SKILL.md) | 「ルールを追加して」等の要求に対し、Rule/Hook/Skill/Workflow のどれで実装すべきかを判断するスキル | 推奨 |
| [.agent/skills/skill-template/](.agent/skills/skill-template/SKILL.md) | 新しい Skill を作るときのひな形。コピーして使う | オプション |
| [.claude/hooks/](.claude/hooks/check_wiki_updated.sh) | `gh pr create` の直前に `wiki/` の更新有無を確認する hook と、`gh pr merge` の直前に agy レビューの記録を確認する hook | オプション |
| [.claude/settings.example.json](.claude/settings.example.json) | permissions / hooks の設定例 | オプション |
| [docs/development_workflow.md](docs/development_workflow.md) | worktree ベースの Git 運用 + CI/CD の全体像 | 推奨 |
| [docs/wiki_workflow.md](docs/wiki_workflow.md) | Wiki の背景・導入手順・既存プロジェクトからの移行手順 | 推奨 |
| [.github/workflows/test.yml](.github/workflows/test.yml) | CI(push/PR で自動テスト)。テストの仕組みがまだ無いうちは警告だけ出して成功する | 推奨(既定で有効) |
| [.github/workflows/selfcheck.yml](.github/workflows/selfcheck.yml) | **このテンプレート自身**の検査(wiki-lint / シェル・PowerShell・Python の構文 / `.ps1` の BOM / レビュースクリプトの回帰テスト)。コピー先には不要で、セットアップスクリプトが削除する | テンプレート専用 |
| [.github/workflows-optional/](.github/workflows-optional/README.md) | CD(self-hosted デプロイ)・label-hygiene・wiki-check の雛形。`.github/workflows/` に置くまで実行されない | オプション |
| [deploy/](deploy/README.md) | CD スクリプト雛形(CI 再確認・drift 検知・バックアップ・反映確認・失敗時ロールバック・結果の可視化)と watchdog 雛形。runner のサービス化を含む導入手順と落とし穴は `deploy/README.md` | オプション |
| [scripts/worktree-cleanup.ps1](scripts/worktree-cleanup.ps1) | `git worktree remove` が Permission denied で失敗した後の復旧(Windows 専用) | オプション |
| [scripts/setup.ps1](scripts/setup.ps1) / [scripts/setup.sh](scripts/setup.sh) | 対話形式でどのモジュールを使うか選び、不要なファイルを削除・必要なファイルをコピーするセットアップスクリプト | - |

## 前提条件

- **Git** 2.20 以降(worktree 機能を使うため)
- **GitHub CLI (`gh`)** — PR 作成・マージ・Issue 操作に必要
- (オプション) GitHub Actions が有効なリポジトリ(CI/CD を使う場合)
- (オプション) **Python 3.9 以降** と **agy (Antigravity CLI)** — agy レビューを使う場合
  (`agy` はログイン済みで PATH に通っていること)

## 使い方

> **最短パス**: とにかく動かすには **ステップ 1 → 2** だけで OK。
> セットアップスクリプトが不要ファイルを削除し、プレースホルダーも案内してくれる。
> ステップ 3・4 は後から必要になったときにやればよい。

### 1. テンプレートを新しいプロジェクトへコピーする

このディレクトリの中身を新規/既存リポジトリのルートにコピーする
(GitHub の "Template repository" 機能を使ってもよい)。

> **既存プロジェクトに導入する場合の注意:**
>
> - `.gitignore` — 既存ファイルがあれば上書きせず、テンプレートの行を **追記** する
>   (重複行はスキップ)。
> - `.github/workflows/test.yml` 等 — 既にある場合はセットアップスクリプトが検知し、
>   上書きするかスキップするかを対話で選べる。上書き時は `.bak` バックアップを作成する。
> - `CLAUDE.md` — 手動でコピーするファイルなので、既存の指示ファイルがあれば
>   内容をマージすること。

### 2. セットアップスクリプトで導入モジュールを選ぶ

```bash
bash scripts/setup.sh
```

```powershell
.\scripts\setup.ps1
```

対話で以下を選べる。

- **CI**(push/PR で自動テスト)— 既定で有効。ホスト型 runner で完結するため基本的に常時導入を推奨。
- **CD**(self-hosted runner での自動デプロイ)— 既定で無効。開発機と本番機が同一マシンの
  ような構成のときだけ有効化する。有効にすると `.github/workflows/deploy.yml` と
  `deploy/auto_deploy.ps1` が作成される。
- **label-hygiene**(Issue ラベルの自動整理)— GitHub Issues/Projects 連携を使う場合のみ。
- **wiki-check**(PR に `wiki/` 更新が含まれるかの確認)— 既定で無効。警告のみで CI は落とさない。
- **ナレッジ Wiki**(`wiki/`)— 既定で有効。使わないなら削除。

スクリプトを使わず手動で選ぶ場合は、`.github/workflows-optional/` の中から必要なものだけ
`.github/workflows/` にコピーすればよい(GitHub Actions は `.github/workflows/` 直下しか
実行しないため、コピーしない限り何も起きない)。

### 3. プレースホルダーを埋める

`<...>` になっている箇所をプロジェクトの実際の値に置き換える。特に:

- `CLAUDE.md` — プロジェクト概要、Issue/Projects の実リポジトリ名・ラベル
- `.agent/skills/*/SKILL.md` — リポジトリ名・テストコマンド・ヘルスチェック URL、
  および採用しない仕組み(Wiki / Issue 連携 / 本番同居 CD)の節の削除
- `docs/development_workflow.md` — 採用した CI/CD 方式
- `wiki/overview.md` — システム全体像(Wiki を採用する場合、最初に書くページ)

既存プロジェクトに Wiki を導入する場合(設計書がすでにたまっている場合を含む)は、
[docs/wiki_workflow.md](docs/wiki_workflow.md) の「既存プロジェクトへの導入手順」に従う。

### 4. 不要なオプション節を削除する

CLAUDE.md 内の「[オプション]」と付いた節は、対応する仕組みを使わないなら
節ごと削除してよい。

## 設計思想

- **知識は設計書ではなく Wiki に蓄積する。** 実装前に書く設計書は、書いた瞬間から現実と
  乖離し、たまるほど「今どうなっているか」が読めなくなる。代わりに、これから作るものの仕様は
  GitHub Issue に置き、実装後の現状は `wiki/` のページとして**毎 PR で更新する**。
  ページはコードの複製ではなく、コードを読んでも分からないこと(なぜこの設計か・過去の事故・
  踏むと壊れる前提)を保存する場所にする。詳細は [docs/wiki_workflow.md](docs/wiki_workflow.md)。
- **エージェントが安全に自律行動できる範囲を明文化する。** 「都度確認不要」と書く範囲を
  明確にすることで、些末な確認のたびに人間を待たせず、かつ危険な操作(本番直 push、
  メインツリーでの HEAD 前進など)には踏み込ませない。
- **CI は常時、CD は選択制。** テストの自動実行はほぼ全プロジェクトで有効だが、
  デプロイの自動化はインフラ構成に強く依存する(ホスティング側のオートデプロイで足りる
  こともあれば、self-hosted runner が必要なこともある)。無理に CD まで含めず、
  `.github/workflows-optional/` に隔離して選べるようにしている。
- **手順はスキルへ、不変条件は CLAUDE.md へ。** 逐次手順まで `CLAUDE.md` に書くと、
  毎セッション全文がコンテキストに載るうえ、「絶対に守ること」と「今回の段取り」が混ざって
  どちらも守られなくなる。`CLAUDE.md` には破ると事故になる不変条件だけを置き、
  コマンドの並びと罠は `worktree-start` / `wiki-ingest` / `pr-finish` の 3 スキルに降ろしてある。
  スキルが発火しなかった場合でも `CLAUDE.md` だけで安全側に倒れるよう、両者は意図的に
  一部重複させている。
- **事故から学んだ禁止事項を明文化する。** 「メインツリーで git checkout/merge/pull しない」
  「main へ直接 push しない」「git add -A を使わない」「gh pr create --fill を使わない」
  「PR 本文に Fixes # を書かない」「gh pr merge --delete-branch を付けない」は、
  いずれも実運用で実際に起きた事故(複数セッションの HEAD 奪い合い、CD の変更検知の無効化、
  Issue の自動クローズで実装内容の要約が失われる、worktree 内での checkout 失敗を
  マージ失敗と誤読する)への再発防止策。
