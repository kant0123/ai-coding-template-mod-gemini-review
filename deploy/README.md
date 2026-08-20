# deploy/

[オプション] 自前サーバーへ self-hosted runner でデプロイする構成
（`docs/development_workflow.md` の「CD 方式 B」）で使うテンプレート。
ホスティング先の自動デプロイに任せる場合（方式 A）はこのディレクトリごと削除してよい。

| ファイル | 内容 |
| --- | --- |
| `auto_deploy.ps1.example` | CD 本体。変更検知・drift 検知・CI 再確認・バックアップ・再起動・反映確認・ロールバック・通知・履歴 |
| `watchdog.ps1.example` | プロセス死活監視。落ちていたら再起動、応答はするが異常なら通知のみ |

## 導入手順

0. **self-hosted runner を登録し、サービスとして常駐させる**（下記「runner のサービス化」）。
1. `.example` を外してコピーし、`Invoke-DependencyInstall` / `Invoke-PreDeployBackup` /
   `Invoke-Migration` / `Restart-App` を実プロジェクトのコマンドに書き換える。
2. **アプリの `/healthz` に稼働中コミットの short SHA を `commit` として出す**（下記）。
3. `.github/workflows-optional/deploy.yml` を `.github/workflows/` へコピーする。
4. runner に環境変数を設定する（`APP_REPO_PATH` / `APP_HEALTH_URL` / `APP_RESTART_CMD` /
   `DISCORD_WEBHOOK_URL`）。**runner は起動時の環境変数を引き継ぐので、設定後に再起動する。**
5. watchdog をタスクスケジューラ（または cron）へ登録する。
6. 最後にリポジトリ変数 `SELF_HOSTED_DEPLOY` を `true` にして CD を有効化する。

## runner のサービス化

`run.cmd` / `./run.sh` で対話起動した runner は**その端末を閉じた時点で死ぬ**。
サインアウト・再起動のたびに CD が無言で止まり、「push したのに deploy が起動しない」
（＝ runner オフライン）になるため、**登録したら必ずサービスとして常駐させる**。

### Windows

```powershell
# 1. 展開ディレクトリで登録（実行アカウントは必ず明示する。下記「実行アカウントを決める」）
cd C:\actions-runner-<project>
.\config.cmd --url https://github.com/<owner>/<repo> --unattended `
  --token <REGISTRATION_TOKEN> --name <project>-runner --labels self-hosted,windows `
  --runasservice --windowslogonaccount ".\<user>" --windowslogonpassword "<password>"

# 2. サービスの状態確認（Status=Running / StartType=Automatic）
Get-Service actions.runner.* | Select-Object Name, Status, StartType
```

**現行の Windows runner に `svc.cmd` は無い**（Linux の `svc.sh` に相当するものが同梱されない）。
サービス化は `config.cmd --runasservice` で行い、既に登録済みのサービスの**アカウントだけ**を
変えるなら、再登録せず `services.msc` の「ログオン」タブ、または
`sc.exe config <サービス名> obj= ".\<user>" password= "<password>"` で切り替えて再起動する。

### Linux / macOS

```bash
cd ~/actions-runner-<project>
sudo ./svc.sh install "$USER"   # 実行アカウントを明示する
sudo ./svc.sh start
sudo ./svc.sh status
```

### 実行アカウントを決める（ここが一番踏む）

アカウントを指定せずにサービス化すると、既定の `NT AUTHORITY\NETWORK SERVICE` で動く。
**サービス実行アカウントによって「見える環境変数」と「使える権限」が変わる**ので、
デプロイ対象のパス・認証情報にアクセスできるアカウントを選ぶこと。

**方式 B（開発機と本番機が同一マシン）では、実ユーザーを選ぶのがほぼ唯一の正解。**
CD は本番チェックアウトに対して `git merge` / 依存インストール / プロセス再起動 /
ログ書き込みを行うため、実ユーザーの権限一式（対象ドライブの ACL・サービス/タスクの操作権限・
git 資格情報・パッケージキャッシュ）が要る。サービスアカウントで動かすと、下の表の順に
症状が出て潰しても潰しても次が出る（実例: `pwsh: command not found` →
`running scripts is disabled on this system` → `Access to the path ... is denied.`）。
ACL を個別に開けていくのは、本番ディレクトリをサービスアカウントに開放することと同じ。

| 実行アカウント | 見える環境変数 | 注意 |
| --- | --- | --- |
| `NETWORK SERVICE`（既定） | システム環境変数のみ | ユーザー環境変数が**見えない**。ユーザープロファイル配下・別ドライブのパスにも届かず、実行ポリシーも `Restricted` のまま。**方式 B では使えない** |
| `LocalSystem` | システム環境変数のみ | 権限は強いがユーザーの資格情報（git credential manager 等）を持たない |
| 実ユーザー（`.\<user>`、要パスワード） | そのユーザーの環境変数 | 開発時と同じ環境が再現できる。**方式 B ではこれを選ぶ** |

実行アカウントは `Get-CimInstance Win32_Service -Filter "Name like 'actions.runner%'" |
Select-Object Name, StartName, PathName` で確認できる。
環境変数を**システム環境変数に置く**か、runner ディレクトリの `.env`
（Windows は `.env` ファイル、Linux は `svc.sh` の environment ファイル）に書けば
アカウントに依存せず読ませられる。

### 変更を反映するには再起動する

**runner プロセスはサービス起動時の環境変数を引き継ぐ。** `APP_HEALTH_URL` などを
設定・変更したら、必ずサービスを再起動する。

```powershell
Restart-Service actions.runner.<owner>-<repo>.<runner-name>
```

```bash
sudo ./svc.sh stop && sudo ./svc.sh start
```

### 複数プロジェクトを同じマシンで動かす場合

runner の展開ディレクトリを**リポジトリごとに分ける**（`C:\actions-runner-<project>`）。
1 ディレクトリを使い回すと後から登録した方で設定が上書きされる。サービス名は
`actions.runner.<owner>-<repo>.<runner-name>` になるので、`--name` を分かる名前にしておくと
`Get-Service actions.runner.*` の一覧で見分けられる。

## 落とし穴（先に読む）

- **`.ps1` は UTF-8 (BOM 付き) で保存する。** BOM が無いと Windows PowerShell 5.1 が
  CP932 として読み、日本語のコメント・文字列が化けて**スクリプトが 1 行も実行されず
  パースエラーで落ちる**。ワークフロー側も `shell: pwsh` にしておくと二重に守れる。
  `Format-Hex -Count 4` の先頭が `EF BB BF` であることを確認すること。
- **初回だけは人間がメインツリーを前進させる必要がある。** `deploy.yml` はツリー上の
  `auto_deploy.ps1` を絶対パスで実行するが、そのツリーを前進させるのはスクリプト自身。
  導入直後は「スクリプトが無い → ジョブが失敗 → ツリーが前進しない」の循環に入るため、
  一度だけ手動で `git merge --ff-only origin/main` する。
- **メインツリーに未コミットの変更があると、デプロイが止まりその変更も消える。**
  `git merge --ff-only` が `Your local changes ... would be overwritten by merge` で失敗し、
  失敗を受けたロールバック（`git reset --hard`）が**その未コミット変更ごと巻き戻す**。
  開発機と本番機が同じディレクトリなので、人間がエディタで直しただけでこの状態になる。
  しかもエラーは「ローカルが分岐している可能性」としか出ず、原因の切り分けを誤らせる。
  `auto_deploy.ps1.example` は merge の前に `git status --porcelain` を見て、
  変更があれば**ロールバックを走らせずに `blocked` で中止**する。
- **デプロイスクリプト自身の変更は 1 デプロイ遅れて効く。** 実行されるのは merge 前の
  （＝ひとつ前の）スクリプト。逆に**ワークフロー YAML は main から読まれるので即座に効く** —
  スクリプトが壊れて CD が止まったら、まずワークフロー側で回避できないか考える。
- **ヘルスチェック URL のポートを変えたら環境変数も直す。** 合っていないとデプロイは毎回
  ヘルスチェック失敗と判定され、**正常なコミットが自動ロールバックされる**。
- **watchdog を SYSTEM で登録すると、ユーザー環境変数の `DISCORD_WEBHOOK_URL` は見えない。**
  runner をサービス化した場合も同じ問題が起きる（上表「実行アカウント」）。両方から通知したい
  なら**システム環境変数**に置く。
- **`pwsh`（PowerShell 7）が入っている前提でワークフローを書かない。** Store 版 pwsh は
  ユーザー単位の App Execution Alias でパス解決されるため、サービス実行アカウントからは
  見えない（`pwsh: command not found`）。`shell: powershell`（5.1 は OS 標準で必ず存在）で
  起動し、pwsh があればそちらへ委譲する形が安全。
- **`shell: powershell` は `-ExecutionPolicy` を付けてくれない。** GitHub の既定は
  `powershell -command ". '<一時ファイル>'"` なので、実行ポリシーが `Restricted` のアカウントでは
  全ステップが `running scripts is disabled on this system` で落ちる。シェル行を
  `powershell -NoProfile -ExecutionPolicy Bypass -Command ". '{0}'"` と自前指定して回避する
  （マシン全体のポリシーを緩めるより影響範囲が狭い）。
- **`Add-Content -Encoding utf8` は 5.1 だと BOM を付ける。** `$GITHUB_OUTPUT` の 1 行目が
  `<BOM>key=value` になると step output が読めず、jsonl のログも壊れる。
  `[System.IO.File]::AppendAllText($path, $line, (New-Object System.Text.UTF8Encoding($false)))`
  を使う。pwsh 7 では BOM 無しなので、5.1 で実行して初めて表面化する。
- **runner を対話起動（`run.cmd`）のままにしない。** 端末を閉じた・サインアウトした瞬間に
  CD が止まり、push しても deploy ジョブが起動しない状態が無言で続く。サービス化して
  `StartType=Automatic` にしておくこと。

## `/healthz` に出すもの

反映確認とdrift検知に**稼働中コミットが要る**。ツリーの HEAD ではなく、
**プロセスが実際に読み込んだコミット**であることが重要（古いプロセスが生き残っていても
ヘルスチェックは 200 を返すため、200 だけでは反映を確認できない）。

- 起動時に一度だけ HEAD を解決する（`git rev-parse HEAD` を優先し、失敗したら `.git` を
  直接読む。worktree では `.git` がファイルで `gitdir: <path>` を指す点に注意）
- 取得できなければ `null` を返し、**例外でアプリを落とさない**
- レスポンス例: `{"ok": true, "commit": "a837a0d", ...}`
