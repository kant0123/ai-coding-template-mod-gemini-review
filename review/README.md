# [オプション] 合議制専門家パネル (Consensus Multi-Expert Review)

PR をマージする前に、**5 名の専門家ロール**でコード差分を監査し、Lead Reviewer が
合議・トリアージして 1 枚のレポートにまとめる仕組み。出自は独立リポジトリ
「Gemini 合議制レビュー」で、このテンプレートに移植するにあたり
**絶対パス依存を外し、PR ワークフローに接続した**。

採用しない場合、このディレクトリと以下をまるごと削除してよい。

- `.agent/skills/multi-expert-review/`
- `.github/workflows/multi-expert-review.yml`
- `CLAUDE.md` の「[オプション] 合議制レビュー」節
- `.agent/skills/pr-finish/SKILL.md` の「レビューパネルを通す」手順

## 2 つの実行系 — 役割が違う

| | 静的プレスキャナ | エージェント側パネル |
| --- | --- | --- |
| 実体 | `review/panel_runner.py` | `review/prompts/` の 5 プロンプト + `multi-expert-review` スキル |
| 動かすもの | 正規表現・部分文字列マッチ | LLM(エージェント自身) |
| 実行タイミング | CI が毎 PR に自動実行 | `pr-finish` の中でエージェントが実行 |
| 速さ / コスト | 数秒・無料・API キー不要 | 遅い・トークンを食う |
| 見えるもの | 定型パターン(float 金銭計算、`@pytest.mark.skip`、無防備な NOT NULL など) | 文脈依存の欠陥(認可の抜け、ドメイン不変条件の破綻、並行競合) |
| 通ったときの意味 | **「問題なし」ではない。** 足切りを抜けただけ | 監査済み |

**プレスキャナが APPROVE を出しても、それは見落としが無いことを意味しない。**
CI に置いているのは「機械が確実に判定できる範囲を、人間とエージェントが忘れても必ず見る」ため。
本命は常にエージェント側パネル。

## 構成

```
review/
  panel_runner.py                       静的プレスキャナ (CI が実行)
  configs/domain_invariants.json        5 ドメインの不変条件定義
  prompts/
    01_security_auth.md                 セキュリティ・認可
    02_architecture_concurrency.md      アーキテクチャ・並行制御
    03_domain_adversarial.md            ドメイン不変条件・敵対的 PoC
    04_qa_breaking_guard.md             破壊的変更・テスト隠蔽
    05_lead_consensus_triage.md         Lead Reviewer 合議・トリアージ
  work/                                 中間成果物 (.gitignore 対象)
```

## ドメイン

`--domain` で不変条件セットを切り替える。既定は `general`。

| 値 | 対象 |
| --- | --- |
| `general` | 一般的な Web / バックエンド (OWASP Top 10, Clean Architecture) |
| `fintech` | 金融・暗号資産 (IEEE 754 丸め誤差、TOCTOU 二重出金) |
| `distributed` | 分散・イベント駆動 (Dual-Write、分散ロック早期解放) |
| `healthcare` | 医療・臨床安全 (単位混同、投与量上限バイパス) |
| `embedded` | 組込み・車載 RTOS (CAN エンディアン、ISR 内ブロッキング) |

**プロジェクトの既定ドメインはリポジトリ変数 `REVIEW_DOMAIN` で指定する**
(GitHub → Settings → Secrets and variables → Actions → Variables)。未設定なら `general`。

## 手で走らせる

```bash
git diff origin/main...HEAD > review/work/diff.patch
python review/panel_runner.py --diff review/work/diff.patch --domain general
```

ファイル・ディレクトリを直接見せることもできる。

```bash
python review/panel_runner.py --target src/payments --domain fintech
```

主なオプション。

| オプション | 意味 |
| --- | --- |
| `--target <path>` | 監査対象のファイル / ディレクトリ |
| `--diff <path>` | git diff のパッチファイル(PR レビュー用) |
| `--domain <name>` | 上表のドメイン。既定 `general` |
| `--task-id <id>` | タスク識別子。既定 `task_YYYYMMDD_HHMMSS` |
| `--work-dir <path>` | 中間成果物の出力先。既定 `review/work/<task_id>` |
| `--output <path>` | 最終レポートの出力先 |
| `--fail-on-critical` | Critical が 1 件でもあれば exit 1(CI 用。既定では落とさない) |

`--fail-on-critical` を既定にしていないのは、静的パターン検査が誤検知しうるため。
落とすかどうかは CI 側で明示的に選ばせる。

## 出力 — コンテキスト隔離

各専門家の生レポートは `review/work/<task_id>/` に分離保存され、
呼出元は最終成果物 `final_consensus_review.md` だけを読めばよい。

```
review/work/<task_id>/
  01_security_report.json
  02_architecture_report.json
  03_domain_adversarial_report.json
  04_qa_report.json
  05_consensus_triage.json
  final_consensus_review.md   ← これだけ読む
```

## 指摘の書き方 — 3 原則

エージェント側パネルを回すときは以下を守る(出自リポジトリの `AGENTS.md` から移設)。

1. **偽陽性を出さない。** 実害のないチェックリスト項目や、文脈を無視した教科書的警告
   (インメモリ辞書に TLS を要求する類)を出さない。指摘には必ず
   「なぜ本番障害・インシデント・データ不整合に直結するのか」の機序を書く。
2. **心理的安全性。** 「なぜこんな実装をしたのか」「初歩的なミス」といった非難表現を使わない。
   「〜のリスクを防ぐため、このように改善することを提案します」と書く。
3. **外科手術的な修正コード。** 概念論で止めず、そのまま適用できるスニペットを添える。

## CI

`.github/workflows/multi-expert-review.yml` が PR ごとにプレスキャナを回し、
レポートを PR コメントとして投稿する。Critical があればジョブが失敗してマージをブロックする。

- 個別に外したい PR には `review:skip` ラベルを付ける。
- **テストのワークフローに相乗りさせないこと。** `deploy.yml` を採用している場合、
  CD は `workflow_run` で CI ワークフローの conclusion を待つため、レビュー指摘 1 件で
  本番デプロイまで止まる(`wiki-lint` を分けているのと同じ理由)。
