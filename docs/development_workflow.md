# 開発ワークフロー(Git / CI / CD)

このドキュメントは、AI エージェント(Claude Code 等)が複数セッションで並行作業することを
前提にした、日常の開発運用を 1 枚にまとめたテンプレートである。

- **エージェント向けの実行ルール**は リポジトリ直下の [CLAUDE.md](../CLAUDE.md) にある。
  本書はその背景・全体像を人間が読むためのもので、内容が食い違った場合は
  `CLAUDE.md` を正とする。
- 知識の蓄積(`wiki/`)については [wiki_workflow.md](wiki_workflow.md) を参照。
  仕様は Issue に、実装後の現状は `wiki/` に置き、**Wiki 更新は機能 PR に同梱する**。
- CI/CD の導入有無・方式はプロジェクトごとに選ぶ(下記「CI/CD の選択」参照)。
  未導入の場合は該当節を削除してよい。

## 全体像(CI/CD を両方導入した場合の例)

**PR のマージが先、deploy は最後**(逆ではない)。エージェントが行うのは Issue 起票から
「CI 緑を確認して PR をマージする」までで、それ以降(main への push → CI → deploy)は
CI/CD が自動で連鎖する。マージが deploy の引き金であり、deploy の後に別のマージが
続くわけではない。

```mermaid
flowchart TD
    issue["Issue 起票<br/>(仕様・受け入れ基準はここに書く)"]
    worktree["worktree で実装<br/>(着手前に wiki/index.md を読む = Query)"]
    wikiUpdate["wiki/ 更新を同じブランチにコミット (Ingest)"]
    push["git push -u origin &lt;branch&gt;"]
    pr["PR 作成"]
    prTests["tests (CI) が PR に対して走る"]
    wikiCheck["wiki-check (オプション) が PR に対して走る"]
    gate{"CI 緑?"}
    review["agy レビュー (オプション)<br/>python review/agy_review.py"]
    verdict{"APPROVE?"}
    triage{"指摘を評価<br/>妥当な指摘がある?"}
    fp["誤検知を gemini-review に起票<br/>--triage で評価を記録"]
    merge["gh pr merge &lt;PR番号&gt; --merge<br/>(エージェントの仕事はここまで)"]
    mainCommit["main に merge commit"]
    mainPush["main への push"]
    mainTests["tests (CI) が main に対して走る"]
    deploySuccess{"success?"}
    deploy["deploy (CD, オプション)"]

    issue --> worktree --> wikiUpdate --> push --> pr
    pr --> prTests
    pr -.オプション.-> wikiCheck
    prTests --> gate
    wikiCheck -.-> gate
    gate -->|No| worktree
    gate -->|Yes| review --> verdict
    verdict -->|Yes| merge
    verdict -->|差し戻し| triage
    triage -->|Yes: 修正して push| worktree
    triage -->|No: すべて誤検知| fp --> merge
    merge --> mainCommit --> mainPush --> mainTests --> deploySuccess
    deploySuccess -->|success| deploy
```

## 作業の進め方

エージェント向けの**逐次手順はスキルに分けて置いてある**
(`.agent/skills/<name>/SKILL.md`。Claude Code なら `.claude/skills/` に置く)。
本節は人間が全体像を掴むための要約で、細かい罠は各スキルに書いてある。

| 局面 | スキル | 主な内容 |
| --- | --- | --- |
| 着手 | `worktree-start` | Wiki を読む → Issue 確保 → `origin/main` から worktree |
| 実装後 | `wiki-ingest` | 影響ページの更新 → `log.md` 追記 → wiki-lint |
| 完了 | `pr-finish` | push → PR → CI → (agy レビュー)→ マージ → 後始末 → Issue クローズ → 反映確認 |
| CI 緑の後 | `agy-review`(オプション) | agy でレビュー → 差し戻しを評価 → 修正ループ / 誤検知を起票してマージ |

### 1. 開始 — worktree を作る

```bash
git fetch origin
git worktree add ../<repo>-<名前> -b <branch> origin/main
```

- ブランチ名は `fix/xxx` / `feature/xxx` / `chore/xxx` / `docs/xxx`。
- **ローカル `main` ではなく `origin/main` から切る**。
- worktree はリポジトリの外の兄弟ディレクトリに置く。

複数セッションが同時に作業するため、1 つの作業ツリーを共有すると HEAD を奪い合い、
自分のコミットが他人のブランチ上に載る事故が起きる。worktree はその防止も兼ねる。

### 2. コミット

- コミットメッセージは `feat:` / `fix:` / `docs:` / `chore:` + 概要。
- **`git add` はファイルを明示指定する。** 他セッションの未コミット変更が
  作業ツリーに混ざっていることがあるため、`git diff --cached` で
  ステージ内容を確認してからコミットする。
- **`wiki/` の更新を同じブランチに含める。** コミットは分けてよい(`docs: wiki を更新`)が、
  PR は分けない。実装と知識の反映がずれると Wiki は腐る。

### 3. 完了 — PR を作って CI を通してマージ

```bash
git push -u origin <branch>
gh pr create --title "<一文>" --body "<対応 Issue / 変更内容 / Wiki の 3 節>"
gh pr checks <PR番号> --watch             # CI を導入している場合、成功を確認
gh pr merge <PR番号> --merge
git push origin --delete <branch>
cd <メインツリー>                          # worktree の中からは削除できない
git worktree remove ../<repo>-<名前>
```

- **main へ直接 push・直接マージしない。** 必ず PR を経由する。
- CI が失敗したらマージせず原因を調査する。修正が困難なら PR は開いたまま報告する
  (勝手に close しない)。
- **`gh pr create --fill` を使わない。** 本文をコミットメッセージから生成するため、
  規約が要求する「対応 Issue」「Wiki」の節を満たせない。`--body` で明示する。
- **PR 本文・コミットメッセージに `Fixes #` / `Closes #` を書かない。** 自動クローズされると
  後段の `gh issue close --comment` が `already closed` で失敗し、実装内容の要約が
  Issue に残らないまま手順が中断する。素の `#<番号>` 参照に留める。
- **`gh pr merge` に `--delete-branch` を付けない。** worktree 内で実行すると `gh` が
  マージ後に `main` へ checkout しようとして
  `fatal: 'main' is already used by worktree at ...` で失敗する。
  **マージ自体は成功している**ので、エラーを見てマージ失敗と誤読しないこと
  (リモートブランチ削除に到達しないだけ。`git push origin --delete` で消す)。

## CI/CD の選択

このテンプレートは 3 段階で導入できる。`scripts/setup.ps1` / `scripts/setup.sh` を
実行すると対話形式で選べる(詳細は [README.md](../README.md))。

| 段階 | 内容 | 必要なもの |
| --- | --- | --- |
| なし | ローカルでテストを回すのみ | - |
| CI のみ(推奨) | push/PR で自動テスト実行 | GitHub Actions(hosted runner で足りる) |
| CI + CD | main への push を CI 成功後に自動デプロイ | デプロイ先環境、下記いずれかの CD 方式 |

### ブランチ保護 — これを入れないと CI はゲートにならない

**ワークフローを置いただけでは、マージは一度も止まらない。** GitHub Actions のジョブが赤くても
`gh pr merge` はそのまま通る。マージを止めているのは常に**ブランチ保護の必須チェック**であり、
ワークフローはその判定材料を作っているだけである。

**この設定はリポジトリ側にあり、テンプレートのコピーには引き継がれない。** 新しく作った
プロジェクトでは毎回手で有効化すること。飛ばすと、CI が緑でも赤でもマージできる状態のまま
「CI 緑を確認してマージ」という手順だけが回り続ける。

```bash
gh api --method PUT repos/<owner>/<repo>/branches/main/protection \
  --input .github/branch-protection.json
```

同梱の [.github/branch-protection.json](../.github/branch-protection.json) は
`test` を必須チェックにする。採用していないワークフローの名前は `contexts` から
外すこと — **一度も実行されないチェックを必須にすると、PR が永久に pending のまま
マージできなくなる。**

| 設定 | 値 | 理由 |
| --- | --- | --- |
| `required_status_checks.contexts` | 採用したジョブ名 | `jobs:` 直下のキーであって、ワークフロー名 (`name:`) ではない |
| `required_status_checks.strict` | `false` | `true` にすると main が進むたび全 PR で再 push が要る |
| `required_pull_request_reviews` | `null` | **1 人開発では設定してはいけない。** 自分の PR は自分で承認できず、誰もマージできなくなる |
| `enforce_admins` | `false` | 障害時に管理者が手で復旧する逃げ道を残す |

`wiki:skip` ラベルで外したジョブは **skipped = 成功**として扱われるため、
必須チェックにしても逃げ道は塞がらない。

agy レビューはローカルで agy を動かすため CI のチェックにはならず、ブランチ保護では強制できない。
代わりに [.claude/hooks/check_agy_review.sh](../.claude/hooks/check_agy_review.sh) が
`gh pr merge` の直前に発火し、head SHA に対するレビュー記録が PR に無ければマージを差し止める
(詳細は下記「agy レビュー」)。hook は Claude Code を使っているときしか動かず、
GitHub の画面から押されたマージは素通しになる。

### CI — `tests` ワークフロー

[.github/workflows/test.yml](../.github/workflows/test.yml)

| 項目 | 内容 |
| --- | --- |
| 契機 | `push`(全ブランチ)と `pull_request` |
| 内容 | 依存インストール + テストコマンド実行(既定は Python/pytest。スタックに合わせて書き換える) |

### CD(オプション) — `deploy` ワークフロー

[.github/workflows-optional/deploy.yml](../.github/workflows-optional/deploy.yml)

有効化するには `.github/workflows/` にコピーし、下記いずれかの方式を選ぶ。

**A. ホスティング先の自動デプロイに任せる**(Vercel / Render / Railway など)
このテンプレートの deploy.yml は不要。ホスティング側の「push で自動デプロイ」を使う。
エージェント側のルールは CI を通すところまでで完結する。

**B. 自前サーバーへ self-hosted runner でデプロイ**
開発機と本番機が同一マシンという構成(小規模な自宅サーバー運用などで有効)。

- **runner はサービスとして常駐させること。** `run.cmd` / `./run.sh` での対話起動は端末を
  閉じた時点で死に、以後 push しても deploy ジョブが起動しない状態が無言で続く。
  登録・サービス化・実行アカウント(見える環境変数が変わる)の選び方は
  [deploy/README.md](../deploy/README.md) の「runner のサービス化」を参照。
- 発火条件: `tests` が **main への push** で **success** し、
  リポジトリ変数 `SELF_HOSTED_DEPLOY` が `true` のときだけ実行(既定は無効=常にスキップ)。
- 実処理は [deploy/auto_deploy.ps1.example](../deploy/auto_deploy.ps1.example) に委譲する。
  変更検知・drift 検知・CI 再確認(フェイルクローズ)・デプロイ前バックアップ・
  反映確認・失敗時ロールバック・通知・履歴記録を行う。
  導入手順と落とし穴は [deploy/README.md](../deploy/README.md) にまとめてある。
- **この方式を採る場合、`CLAUDE.md` の「[オプション] 本番同居チェックアウトの追加ルール」を
  有効化すること。** メインツリーの HEAD は CD だけが前進させる(エージェントが
  `git checkout` / `git merge` / `git pull` で先回りすると CD が no-op になり、
  本番が古いコードのまま取り残される)。
- **アプリの `/healthz` に稼働中コミットの short SHA を出すこと。** ツリーの HEAD ではなく
  プロセスが実際に読み込んだコミットが要る。ヘルスチェックが 200 を返すだけでは
  「古いプロセスが生き残っている」状態を検知できない。
- **`.ps1` は UTF-8 (BOM 付き) で保存すること。** BOM が無いと Windows PowerShell 5.1 が
  CP932 として読み、日本語が化けてパースエラーになる(スクリプトが 1 行も動かない)。
- 死活監視が要るなら [deploy/watchdog.ps1.example](../deploy/watchdog.ps1.example) を
  タスクスケジューラ / cron へ登録する。CD は「更新があるとき」しか走らないため、
  プロセスが落ちたままの状態は CD では埋められない。

#### デプロイ結果の読み方

job summary、または実行ログの `::notice::deploy result:` 行に `status` が出る。

| status | 意味 | 終了コード |
| --- | --- | --- |
| `deployed` | 実際に前進して反映した | 0 |
| `no-op` | 更新なし。稼働中コミットも HEAD と一致(正常) | 0 |
| `skipped` | 更新はあったが CI が success でないため見送った | 0 |
| `drift` | 更新なしだが**稼働中コミットが HEAD と違う**(本番が古いまま。要再起動) | 1 |
| `blocked` | メインツリーに未コミットの変更があり、前進していない(退避してから再実行) | 1 |
| `down` | 更新なしで、かつヘルスチェックへ接続できない(プロセス停止) | 1 |
| `rolled_back` | デプロイに失敗し、直前コミットへ戻して復旧した | 1 |
| `failed` | デプロイに失敗し、ロールバックも失敗した(手動対応) | 1 |
| `error` | 想定外の例外 | 1 |

履歴は `logs/deploy-history.jsonl`(git 管理外)にも 1 行ずつ残る。Actions の実行履歴は
保持期間で消え、手動実行分は残らないため。

### `wiki-check`(オプション)

[.github/workflows-optional/wiki-check.yml](../.github/workflows-optional/wiki-check.yml)

PR に `wiki/` の更新が含まれているかを確認する。既定は**警告のみで success** するので
マージは止まらない。必須ゲートにしたい場合はワークフロー末尾の `exit 0` を `exit 1` に変える。
Wiki 更新が不要な PR には `wiki:skip` ラベルを付ける。

ローカル側の同等チェックとして
[.claude/hooks/check_wiki_updated.sh](../.claude/hooks/check_wiki_updated.sh) がある
(`gh pr create` の直前に発火し、Wiki 未更新なら PR 作成をブロックする)。

### `wiki-lint`(オプション)

[.github/workflows-optional/wiki-lint.yml](../.github/workflows-optional/wiki-lint.yml)

`wiki/` の**中身が整合しているか**を機械チェックする(壊れた `[[リンク]]`・孤立ページ・
`index.md` の掲載漏れ・frontmatter の不備・`updated` と最終コミット日のズレ・
`related` の片方向参照)。違反があれば落ちる。`wiki-check` が「**Ingest したか**」を見るのに対し
こちらは「**中身が正しいか**」を見るので、役割が違い併用できる。

- **Wiki を運用するなら実質的に必須。** エージェントは 1 ページ内で完結する規約(frontmatter を
  書く、テンプレートに従う)はよく守るが、Wiki 全体にまたがる大域的な一貫性は自然には保てない。
  壊れるのは常に後者なので、機械に見張らせる。
- **`tests` に相乗りさせない。** CD(方式 B)は `workflow_run` で CI ワークフロー全体の
  conclusion を待つため、テストに wiki-lint を足すと**Wiki の不整合 1 件で本番デプロイが止まる**。
  デプロイの可否と無関係な検査は独立したワークフローに分ける。
- `fetch-depth: 0` が要る。`updated` を各ファイルの最終コミット日と突き合わせるため、既定の
  浅いクローンでは履歴が足りず、**スクリプトは日付の検査を黙って飛ばす**(通っているのに
  何も見ていない状態になる)。
- ローカルでは `node scripts/wiki-lint.js`。**コミットしてから**走らせる(未コミットだと
  `updated` が古いと言われる)。

### agy レビュー(オプション・既定で有効)

CI のワークフローではなく、**CI が緑になった後にエージェントがローカルで実行する**
([review/agy_review.py](../review/agy_review.py))。差分を agy 経由で Gemini にレビューさせ、
結果を PR コメントに残す。手順は `agy-review` スキル、設計判断は [review/README.md](../review/README.md)。

- **CI 緑が前提。** スクリプトは `gh pr checks` が緑でなければ実行を拒否する。
- **記録は head SHA ごと。** push すると未レビューに戻る。
- 差し戻し(CRITICAL / WARNING)があれば、エージェントが指摘を評価する。妥当なら修正して
  push → CI → 再レビュー。すべて誤検知なら `kant0123/gemini-review` に起票し、
  `--triage` で評価を PR に記録してからマージする。
- `gh pr merge` の hook がレビュー記録(差し戻しなら評価の記録も)を確認する。
- 前提: `agy` がインストール・ログイン済みで、PATH に通っていること。
- **導入直後は強制が効かない。** hook もスクリプトもメインツリーから読まれるため、導入 PR が
  メインツリーに反映されるまで(方式 B ならデプロイ完了まで)はレビューを素通しする。
  導入手順と注意点は [review/README.md](../review/README.md) の「導入するときの注意」。
- ドメイン不変条件は `--domain` か環境変数 `REVIEW_DOMAIN` で切り替える
  (`general` / `fintech` / `distributed` / `healthcare` / `embedded`)。未設定なら `general`。

### `label-hygiene`(オプション)

[.github/workflows-optional/label-hygiene.yml](../.github/workflows-optional/label-hygiene.yml)

GitHub Issues / Projects 連携を使う場合の補助ワークフロー。Issue クローズ時に
`status:blocked` ラベルが残っていれば自動で外す。Issues 連携を使わないなら不要。

## やってはいけないこと

| 禁止 | 理由 |
| --- | --- |
| メインツリーで `git checkout` / `git merge` / `git pull`(方式 B を採用している場合) | 複数セッションの HEAD 奪い合い、または CD の変更検知の無効化 |
| main へ直接 push・直接マージ | CI がゲートとして機能しない(マージ後に走るだけになる) |
| `git add -A` / `git add .` | 他セッションの未コミット変更を巻き込む |
| `gh pr create --fill` | PR 本文が規約(対応 Issue / Wiki)を満たさなくなる |
| PR 本文の `Fixes #` / `Closes #` | 自動クローズで `gh issue close --comment` が失敗し、要約が残らない |
| `gh pr merge --delete-branch`(worktree 内) | `main` への checkout に失敗する。マージ済みなのに失敗と誤読される |
| worktree 削除の失敗後に手で `rm -rf` / `Remove-Item` | git が working tree と認識しなくなり `--force` でも回復しない |

## トラブルシュート(worktree)

| 症状 | 確認すること |
| --- | --- |
| `git worktree remove` が必ず失敗する | シェルの作業ディレクトリが削除対象の中にある。`../<repo>-<名前>` は **worktree の中からでも自分自身に解決する**ので、パスを見ただけでは気づけない。メインツリーに `cd` してから実行する |
| `Permission denied` で削除できない | ディレクトリに ReadOnly 属性が付いている(クラウドストレージのミラー同期・バックアップツールが親を掴んでいる場合など)。`scripts/worktree-cleanup.ps1` で属性を外してから git に削除させる |
| `fatal: ... is not a working tree` で `--force` も効かない | 一度削除に失敗した後の状態。手で消さず `scripts/worktree-cleanup.ps1`(引数なし)で孤立エントリを掃除する |
| `git worktree list` に出ないディレクトリが残っている | 削除失敗の残骸。`-RemoveDirs` で消せるが**未コミットの変更ごと消える**ので中身を確認してから |

## トラブルシュート(CD 方式 B を採用している場合)

| 症状 | 確認すること |
| --- | --- |
| デプロイしたのに反映されない | ヘルスチェックの `commit` と `origin/main` を比較。不一致なら CD が no-op になっている(→ メインツリーの HEAD を誰かが先に進めていないか)。この状態は `status: drift` として自動検知される |
| `deploy` が success なのに何も起きていない | `status` を見る。`no-op`(更新なし)か `skipped`(CI が緑でない)。どちらも success で終わる |
| `deploy` が起動しない | `tests` が main の push で success したか / `SELF_HOSTED_DEPLOY` が `true` か / runner がオンラインか(`Get-Service actions.runner.*` が `Running`。対話起動のままだと端末を閉じた時点で落ちている → サービス化する) |
| runner はオンラインなのに環境変数が空 / 認証に失敗する | サービスの実行アカウントを確認する(`Get-CimInstance Win32_Service -Filter "Name like 'actions.runner%'"` の `StartName`)。既定の `NETWORK SERVICE` からはユーザー環境変数もユーザープロファイル配下のパスも見えない |
| 環境変数を直したのに反映されない | runner は起動時の環境変数を引き継ぐ。`Restart-Service actions.runner.*` / `svc.sh stop && start` で再起動する |
| `git merge --ff-only` に失敗 | **メッセージは「分岐している可能性」と出るが、原因は未コミット変更のことが多い。** `git status` を先に見る。分岐しているなら手動で調査が必要 |
| `status: blocked` で止まる | メインツリーに未コミットの変更が残っている。commit / stash して退避してから deploy を再実行する。この中止は**意図的** — そのまま進めると merge が失敗し、ロールバックの `git reset --hard` がその変更を消してしまう |
| ジョブがパースエラーで即座に落ちる(`Unexpected token '繝…'` のような文字化け) | `.ps1` の BOM 欠落。UTF-8 (BOM 付き) で保存し直す。**同時にワークフローを `shell: pwsh` に変える** — ツリー上のスクリプトは前進しないと直らないが、ワークフロー YAML は main から読まれるので即座に効く |
| 導入直後、`auto_deploy.ps1` が見つからずジョブが失敗する | 初回だけ人間がメインツリーを手動で fast-forward する(deploy/README.md「落とし穴」) |
| スクリプトを直したのに挙動が変わらない | デプロイスクリプト自身の変更は 1 デプロイ遅れて効く(実行されるのは merge 前の版) |
| 正常なコミットが毎回ロールバックされる | ヘルスチェック URL(ポート)がアプリの設定とずれている。`APP_HEALTH_URL` を直す |
