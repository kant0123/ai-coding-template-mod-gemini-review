---
name: agent-config-manager
description: >
  エージェントの振る舞いを追加・変更・自動化したいときに使う。
  「〜を自動でやって」「〜をルールに追加して」「〜をコマンドにして」
  「エージェントに覚えさせて」「毎回〜してほしい」「〜したらフックして」
  「定期的に〜して」「外部ツールと繋いで」「プロジェクトの背景を教えて」
  「サブエージェントに分担させて」などのフレーズで発動。
  どのファイルタイプが適切かも判断して作成・更新する。
---

# Skill: Agent Config Manager

このスキルは特定の AI コーディングツールに依存しない汎用の分類軸を示す。
下表の右列は Claude Code での置き場所の対応。他ツール(Gemini CLI 系など)を
使う場合は左列の `.agents/` 配下の記法をそのまま使う。

## 全タイプ一覧

| タイプ | ファイル/場所(汎用) | Claude Code での対応 | カテゴリ |
|--------|-------------|----------------------|---------|
| GEMINI.md / AGENTS.md | プロジェクトルート or `~/.gemini/` | `CLAUDE.md`(プロジェクト直下 or `~/.claude/CLAUDE.md`) | コンテキスト |
| Rule | `.agents/rules/*.md` | `CLAUDE.md` 内の節、または `.claude/rules/*.md` | 指示・制約 |
| Workflow | `.agents/workflows/*.md` | `.claude/commands/<name>.md`(スラッシュコマンド) | 指示・制約 |
| Skill | `.agents/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` | 指示・制約 |
| agents.md | `.agents/agents.md` | `.claude/agents/<name>.md`(サブエージェント定義) | 指示・制約 |
| Hook | `.agents/hooks.json` | `.claude/settings.json` の `hooks` フィールド | システム制御 |
| MCP設定 | `~/.gemini/config/mcp_config.json` | `.mcp.json` またはユーザー設定 | システム制御 |
| Scheduled Task | UI or `.agents/scheduled/` | UI のスケジュール機能 / `schedule` スキル | 自動実行 |
| Dynamic Subagent | agents.md内 or 実行時定義 | `Agent` ツールでの動的起動 | 自動実行 |

---

## Step 1: タイプ判断フロー

ユーザーの要求を受けたら、以下を順番に評価する。
最初にYESになった選択肢を採用する。複数該当する場合は組み合わせる。

```
Q1. プロジェクトの背景・技術スタック・アーキテクチャの説明か？
    （「このプロジェクトはJavaで…」「DBはMySQLで…」的な知識の共有）
    YES → GEMINI.md / CLAUDE.md（プロジェクト固有）または ~/.claude/CLAUDE.md（グローバル）
          ※ 複数ツール共有なら AGENTS.md を優先

Q2. 毎回・常に従わせたいルール・制約・義務か？
    （条件なしで全タスクに適用）
    YES → Rule (.agents/rules/*.md or CLAUDE.md 内の節)

Q3. ツール呼び出し前後・ファイル編集後・セッション開始など
    ライフサイクルイベントで自動インターセプトしたいか？
    YES → Hook (.agents/hooks.json or .claude/settings.json の hooks)

Q4. /コマンドで手動トリガーしたい繰り返し手順か？
    YES → Workflow (.agents/workflows/*.md or .claude/commands/*.md)

Q5. 特定タスクの複雑な手順・専門知識で
    文脈に応じてオンデマンドで読み込みたいか？
    YES → Skill (.agents/skills/<name>/SKILL.md)

Q6. エージェントの役割・ペルソナ・チーム構成の定義か？
    または大きなタスクを複数エージェントに分担させたいか？
    YES → agents.md / .claude/agents/*.md
          ※ 実行時に動的生成するなら Dynamic Subagent として記述

Q7. 外部サービス（DB・クラウド・API）をツールとして使わせたいか？
    YES → MCP設定
          ※ GUIから追加できるツールもある

Q8. 人間の介入なしで定期的に自動実行させたいか？
    （毎朝・毎晩・週次など cron スタイル）
    YES → Scheduled Task

どれも該当しない → プロンプトで直接指示でOK（ファイル不要）
```

---

## Step 2: 各タイプの特性チートシート

### テキスト指示系

| タイプ | 発動 | 向いている用途 | 向いていない用途 |
|--------|------|--------------|----------------|
| GEMINI.md / CLAUDE.md | 常時（背景知識） | 技術スタック説明、コードベース構造、用語定義 | 行動ルール・手順 |
| Rule | 常時（制約） | コーディング規約、禁止事項、完了後の義務 | 複雑な多段階手順 |
| Workflow | /コマンドで手動 | deploy, test, changelog更新など繰り返し作業 | 常時適用したい制約 |
| Skill | 文脈で自動ロード | コードレビュー、DB移行、セキュリティ分析 | 単純な1行のルール |
| agents.md | セッション開始時 | マルチエージェントのチーム定義・役割分担 | 単一エージェント用 |

### システム・自動化系

| タイプ | 発動 | 向いている用途 | 向いていない用途 |
|--------|------|--------------|----------------|
| Hook | イベント自動検知 | ファイル保存後の処理、コマンド実行前の承認 | 複雑なロジック |
| MCP設定 | ツール呼び出し時 | 外部DB・API・クラウドサービスの接続 | 指示・制約の定義 |
| Scheduled Task | cron（時刻/周期） | 定期レポート、毎朝のPRサマリー、週次チェック | オンデマンド作業 |
| Dynamic Subagent | 実行時動的生成 | 大規模リファクタ、並列調査、並列テスト実行 | 単純な単一タスク |

---

## Step 3: 複合パターン（よくある組み合わせ）

```
「コードレビューを自動化したい」
  → Skill（レビュー手順の定義）
  + Hook（ファイル編集後に自動発火）

「デプロイ前にテストを必ず走らせたい」
  → Rule（テストなしでデプロイ禁止）
  + Workflow（/deploy コマンドで手順を実行）

「進捗管理ファイルをタスク後に更新させたい」
  → Rule（タスク完了後に更新する義務）
  + Workflow（/update-progress で手動更新）

「毎朝PRをレビューしてSlackに投稿したい」
  → Scheduled Task（毎朝9時に起動）
  + Skill（PRレビュー手順）
  + MCP設定（Slack連携）

「大規模なリファクタを並列でやらせたい」
  → agents.md（サブエージェントの役割定義）
  + Skill（各エージェントの作業手順）
  + Rule（マージ前の確認義務）

「PR を作る前に wiki/ の更新漏れを防ぎたい」
  → Hook（gh pr create の直前に差分を検査して自動リマインド）
  + Rule/CLAUDE.md（Ingest の具体手順）
```

---

## Step 4: テンプレート集

### GEMINI.md / CLAUDE.md テンプレート
```markdown
# Project Context

## Overview
<プロジェクトの目的・概要>

## Tech Stack
- Language: <言語>
- Framework: <FW>
- Database: <DB>

## Architecture
<コードベース構造・設計方針>

## Key Conventions
<プロジェクト固有の用語・慣習>
```

### Rule テンプレート
```markdown
# Rule: <タイトル>

## ALWAYS（必ずすること）
- <義務1>

## NEVER（してはいけないこと）
- <禁止事項1>

## When <条件>
- <条件付き振る舞い>
```

### Hook テンプレート

汎用ツール向け(`.agents/hooks.json`):
```json
{
  "hooks": [
    {
      "event": "after_tool_call",
      "tool": "edit_file",
      "instruction": "<ファイル編集後にやること>"
    },
    {
      "event": "before_tool_call",
      "tool": "run_command",
      "instruction": "<コマンド実行前にやること>"
    },
    {
      "event": "on_session_start",
      "instruction": "<セッション開始時にやること>"
    }
  ]
}
```

Claude Code 向け(`.claude/settings.json`):
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "bash .claude/hooks/<script>.sh" }
        ]
      }
    ]
  }
}
```
シェルスクリプト側は標準入力から JSON を受け取り、対象の操作に該当する場合のみ
`exit 2` + `stderr` メッセージでエージェントにリマインドを返す
(`.claude/hooks/check_wiki_updated.sh` を参照)。`PreToolUse` なら実行前に止められ、
`PostToolUse` なら実行後にリマインドする。

### Workflow テンプレート
```markdown
---
description: <スラッシュコマンドメニューに表示される説明>
---

When the user types `/<command-name> [args]`:

## Steps
1. <手順1>
2. <手順2>
```

### Skill テンプレート
```markdown
---
name: <skill-name>
description: >
  <発動条件を詳細に記述。日本語のトリガーフレーズも含める。
  エージェントはこのdescriptionだけ読んでロードするかを判断する>
---

# Skill: <タイトル>

## Objective
<このSkillが達成すること>

## Instructions
1. <手順1>
2. <手順2>

## Output
<生成するファイルや成果物>
```

### agents.md テンプレート（チーム定義 + Dynamic Subagent）
```markdown
# Agent Team

## Agents

### <エージェント名>
- Role: <役割>
- Responsibilities: <担当範囲>
- Skills: [<skill-name1>, <skill-name2>]
- Model: <モデル名>

## Subagent Patterns
<!-- 大きなタスクを動的に分割する場合の定義 -->
- <タスク種別>: spawn subagents for <分担方法>
```

### MCP設定テンプレート
```json
{
  "mcpServers": {
    "<server-name>": {
      "command": "<実行コマンド>",
      "args": ["<引数>"],
      "env": {
        "API_KEY": "<キー>"
      }
    }
  }
}
```

---

## Step 5: ファイル作成・更新の手順

1. 既存ファイルが存在する場合は必ず読み込んでから編集する
2. 既存内容を変更する場合はdiffをユーザーに提示してから書き込む
3. 設定ディレクトリ配下のファイルを削除するときは必ずユーザーに確認する

---

## Step 6: 作成後の報告

必ずユーザーに以下を報告する:
1. 作成/更新したファイルのパス
2. どのタイミング・条件で発動するか
3. 他に組み合わせると効果的なタイプがあれば提案する

---

## Step 7: 自己改善チェック

今回のタスクを通じてパターンが見えたら、さらにSkill/Rule化できないか提案する。
このSkill自体（agent-config-manager）も、新しいタイプが追加された場合は更新する。
