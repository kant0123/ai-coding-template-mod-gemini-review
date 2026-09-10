"""
合議制専門家パネル 静的プレスキャナ (Consensus Multi-Expert Review Pre-Scanner)

観点別の監査を work/<task_id>/ に隔離保存した上で、合議トリアージレポートを出力する。

**このスクリプトは LLM を呼ばない。** 実体は正規表現・部分文字列による静的パターン検査で、
パネル 5 名分の観点のうち「機械が確実に判定できる部分」だけを実装したものにすぎない。
文脈を読む本来の監査は `review/prompts/` の 5 プロンプトをエージェントが実行する
(`.agent/skills/multi-expert-review/SKILL.md`)。役割分担は次のとおり。

- 本スクリプト: CI で毎 PR に自動実行する足切り。速い・決定的・API キー不要。
  当たれば確度は高いが、**通っても「問題なし」を意味しない**(見落とす)。
- エージェント側パネル: 文脈依存の欠陥(認可の抜け、ドメイン不変条件の破綻)を見る本命。

CLI:
    python review/panel_runner.py --diff diff.patch --domain general --fail-on-critical
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime
from pathlib import Path

PANEL_ROOT = Path(__file__).resolve().parent

def load_domain_invariants(domain: str) -> dict:
    config_path = PANEL_ROOT / "configs" / "domain_invariants.json"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        invariants = json.load(f)
    return invariants.get(domain, invariants.get("general", {}))

def analyze_security(content: str, domain_info: dict) -> list:
    findings = []
    # 1. Timing Attack Check
    if "==" in content and any(k in content.lower() for k in ["token", "secret", "key", "hash"]):
        if "compare_digest" not in content:
            findings.append({
                "severity": "CRITICAL",
                "category": "Timing Attack Vulnerability",
                "issue": "秘密トークンまたはハッシュの比較に通常の '==' 演算子が使用されています。",
                "impact": "攻撃者が応答時間の微小な差異（ナノ秒単位）を統計解析し、認証バイパスを行うリスクがあります。",
                "recommendation": "hmac.compare_digest() を使用して定数時間で比較を行ってください。",
                "fix_code": "# 修正例:\nimport hmac\nis_valid = hmac.compare_digest(user_token, secret_token)"
            })

    # 2. Hardcoded secret check
    if any(k in content.lower() for k in ["secret_key =", "api_key =", "password ="]):
        findings.append({
            "severity": "CRITICAL",
            "category": "Hardcoded Credential",
            "issue": "機密情報（シークレットキー/APIキー）がコード内に直接ハードコードされています。",
            "impact": "リポジトリ閲覧者による不正アクセスや情報漏洩に直結します。",
            "recommendation": "環境変数またはシークレットマネージャーから取得するように改修してください。",
            "fix_code": "# 修正例:\nimport os\nSECRET_KEY = os.environ.get('APP_SECRET_KEY')"
        })

    # 3. IDOR Check
    if "user_id" in content and ("query_params" in content or "Query(" in content):
        findings.append({
            "severity": "CRITICAL",
            "category": "IDOR (Insecure Direct Object References)",
            "issue": "リクエストパラメータ経由で受け取った user_id で他者のリソースにアクセスできる構造です。",
            "impact": "認証済み悪意ユーザーが他人の決済情報や非公開データを不正閲覧・改ざんできます。",
            "recommendation": "リクエストパラメータではなく、認証コンテキスト (current_user.id) からユーザーを特定してください。",
            "fix_code": "# 修正例:\nasync def get_resource(current_user: User = Depends(get_current_active_user)):\n    return await fetch_user_data(user_id=current_user.id)"
        })

    return findings

def analyze_architecture(content: str, domain_info: dict) -> list:
    findings = []
    # 1. N+1 Query Check
    if "for " in content and any(q in content for q in ["SELECT ", "select ", ".fetch_", ".execute("]):
        findings.append({
            "severity": "WARNING",
            "category": "N+1 Query Bottleneck",
            "issue": "ループ処理の内部で個別にSQLクエリまたは非同期取得が実行されています。",
            "impact": "対象データ数が数十〜数百件に増加した際、DB往復オーバーヘッドによりレイテンシが数十倍に悪化します。",
            "recommendation": "IN / ANY 句を用いたバルク一括フェッチクエリに統合してください。",
            "fix_code": "# 修正例:\nquery = 'SELECT * FROM items WHERE id = ANY(:ids)'\nresults = await db.fetch_all(query=query, values={'ids': item_ids})"
        })

    # 2. Concurrency Lock Order
    if "async with" in content and "lock" in content.lower() and "sorted(" not in content:
        findings.append({
            "severity": "CRITICAL",
            "category": "Deadlock Risk in Concurrency",
            "issue": "複数リソースへの排他ロック取得順序が保証されておらず、並行アクセス時のデッドロックリスクがあります。",
            "impact": "逆順ロック待機によりプロセス全体が永久フリーズし、サービス停止に至ります。",
            "recommendation": "ロック取得対象のIDリストを常に昇順（sorted）に整列してから順次ロックを取得してください。",
            "fix_code": "# 修正例:\nfor resource_id in sorted(target_ids):\n    async with get_lock(resource_id):\n        # 安全な順次処理"
        })

    return findings

def analyze_domain(content: str, domain: str, domain_info: dict) -> list:
    findings = []

    # 1. FinTech
    if domain == "fintech" or any(k in content.lower() for k in ["margin", "currency", "price", "interest", "balance"]):
        if re.search(r'\bfloat\(', content) or ": float" in content or "float =" in content:
            findings.append({
                "severity": "CRITICAL",
                "category": "Domain Invariant Violation (IEEE 754 Float)",
                "issue": "金融計算・金利・証拠金計算に float (二進浮動小数点数) が使用されています。",
                "impact": "二進表現の微小丸め誤差が複利計算や大量取引で累積し、重大な帳簿残高乖離・法的監査違反を招きます。",
                "recommendation": "decimal.Decimal を使用し、丸めモード (ROUND_HALF_EVEN等) を明示的に指定してください。",
                "fix_code": "# 修正例:\nfrom decimal import Decimal, ROUND_HALF_EVEN\namount = Decimal(str(raw_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)"
            })
        if "balance" in content and ("withdraw" in content or "UPDATE" in content) and "for update" not in content.lower() and "lock" not in content.lower():
            findings.append({
                "severity": "CRITICAL",
                "category": "Domain Invariant Violation (TOCTOU Double Spending)",
                "issue": "残高確認から出金・残高更新までの処理がアトミックに保護されていません。",
                "impact": "並行して複数の出金リクエストが到達した場合、残高以上の引き出し（二重出金）が発生します。",
                "recommendation": "DBトランザクション内の SELECT FOR UPDATE または分散排他ロックを導入してください。",
                "fix_code": "# 修正例 (SQL):\nSELECT balance FROM accounts WHERE id = :id FOR UPDATE;"
            })

    # 2. Distributed
    if domain == "distributed" or any(k in content.lower() for k in ["kafka", "outbox", "rabbit"]):
        if "commit" in content and ("send(" in content or "producer" in content):
            if "outbox" not in content.lower():
                findings.append({
                    "severity": "CRITICAL",
                    "category": "Domain Invariant Violation (Dual Write Pattern)",
                    "issue": "DBコミットとメッセージブローカーへの直接送信が分離して実行されています。",
                    "impact": "送信直前のクラッシュやネットワークエラー時に配送イベントが永久消失し、幽霊注文や不整合が発生します。",
                    "recommendation": "Transactional Outbox パターンを採用し、同一DBトランザクション内にイベントを永続化してください。",
                    "fix_code": "# 修正例:\nawait db.execute('INSERT INTO outbox_events (...) VALUES (...)')"
                })

    # 3. Healthcare
    if domain == "healthcare" or any(k in content.lower() for k in ["dose", "patient", "clinical"]):
        if "mg/dl" in content.lower() and "umol" in content.lower() and "conversion" not in content.lower():
            findings.append({
                "severity": "CRITICAL",
                "category": "Domain Invariant Violation (Unit Mismatch)",
                "issue": "クレアチニン値等の臨床単位 (mg/dL と µmol/L) の未換算・混同リスクが検出されました。",
                "impact": "約88倍の計算乖離が発生し、患者への致死量過剰投与または重篤な急性腎不全を招きます。",
                "recommendation": "Pydantic単位型安全バリデータを適用し、受取時にISO単位へ厳密正規化してください。"
            })

    # 4. Embedded
    if domain == "embedded" or any(k in content.lower() for k in ["can", "isr", "rtos"]):
        if "isr" in content.lower() and any(m in content.lower() for m in ["mutex", "lock", "malloc"]):
            findings.append({
                "severity": "CRITICAL",
                "category": "Domain Invariant Violation (Blocking in ISR)",
                "issue": "割込みサービスルーチン (ISR) 内でブロッキング同期または動的メモリ確保が検出されました。",
                "impact": "割込みコンテキストでの永久デッドロックおよびハードウェア緊急停止の不能を招きます。",
                "recommendation": "ISR内は非同期イベントフラグのセットのみ行い、処理はタスクスレッドへ委譲してください。"
            })

    return findings

def analyze_qa(content: str, domain_info: dict) -> list:
    findings = []
    # 1. NOT NULL column without default
    if "add_column" in content and "nullable=False" in content and "server_default" not in content:
        findings.append({
            "severity": "CRITICAL",
            "category": "Breaking Migration (Zero-Downtime Violation)",
            "issue": "既存データが存在するテーブルに対し、デフォルト値のない NOT NULL カラムが直接追加されています。",
            "impact": "本番環境でのマイグレーション実行時に既存行への制約違反でDB更新が失敗し、デプロイが中断・クラッシュします。",
            "recommendation": "段階的マイグレーション（1. NULL許容で追加 → 2. 既存行バックフィル → 3. NOT NULL制約適用）を行ってください。",
            "fix_code": "# 修正例 (Alembic):\nop.add_column('table', sa.Column('col', sa.String(), server_default='default_val', nullable=False))"
        })

    # 2. Test suppression
    if "@pytest.mark.skip" in content or "# pytest.skip" in content:
        findings.append({
            "severity": "CRITICAL",
            "category": "Test Suppression & Quality Regression",
            "issue": "失敗したテストケースが @pytest.mark.skip やコメントアウトによって無力化されています。",
            "impact": "既存の正常動作・後方互換性が破壊された事実がCIで隠蔽され、本番不具合の原因となります。",
            "recommendation": "テストをスキップせず、実装側を修正して既存テストの契約を満たしてください。"
        })

    return findings

def lead_reviewer_triage(all_findings: list) -> dict:
    criticals = []
    warnings = []
    nitpicks = []

    seen = set()
    for item in all_findings:
        key = f"{item.get('category')}_{item.get('issue')}"
        if key in seen:
            continue
        seen.add(key)

        sev = item.get("severity", "WARNING").upper()
        if sev == "CRITICAL":
            criticals.append(item)
        elif sev == "WARNING":
            warnings.append(item)
        else:
            nitpicks.append(item)

    if criticals:
        verdict = "REQUEST_CHANGES"
        verdict_comment = "マージを直ちにブロックすべきクリティカルな問題が検出されました。"
    elif warnings:
        verdict = "COMMENT"
        verdict_comment = "本番障害および保守性低下を防ぐための推奨修正事項があります。"
    else:
        verdict = "APPROVE"
        verdict_comment = "致命的欠陥およびアーキテクチャ違反は検出されませんでした。"

    return {
        "verdict": verdict,
        "verdict_comment": verdict_comment,
        "critical_count": len(criticals),
        "warning_count": len(warnings),
        "nitpick_count": len(nitpicks),
        "criticals": criticals,
        "warnings": warnings,
        "nitpicks": nitpicks
    }

def generate_markdown_report(task_id: str, domain: str, target: str, triage: dict, scanned_chars: int = 0) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"# 合議制専門家パネル コード監査＆トリアージレポート")
    lines.append(f"- **監査タスクID**: `{task_id}`")
    lines.append(f"- **対象ドメイン**: `{domain}`")
    lines.append(f"- **対象ソース**: `{target}`")
    lines.append(f"- **監査実施日時**: `{now_str}`")
    lines.append(f"- **読み込んだ文字数**: `{scanned_chars}`")
    lines.append(f"- **総合判定**: **{triage['verdict']}** ({triage['verdict_comment']})")
    lines.append("")
    if scanned_chars == 0:
        # 0 文字で APPROVE すると「通っているのに何も見ていない」状態に黙って落ちる。
        lines.append("> ⚠️ **対象が空です。** パスまたは差分の抽出条件が壊れている可能性があります。")
        lines.append("> この APPROVE は「問題が無い」ことを意味しません。")
        lines.append("")
    lines.append("> このレポートは静的パターン検査 (`review/panel_runner.py`) の結果です。")
    lines.append("> 文脈依存の欠陥は検出できません。本命の監査は `review/prompts/` の 5 プロンプトを")
    lines.append("> エージェントが実行する `multi-expert-review` スキル側で行ってください。")
    lines.append("")
    lines.append("## 監査概要サマリー")
    lines.append(f"| 🚨 Critical (即時ブロック) | ⚠️ Warning (改善推奨) | 💡 Nitpick (軽微) |")
    lines.append(f"| :---: | :---: | :---: |")
    lines.append(f"| **{triage['critical_count']} 件** | **{triage['warning_count']} 件** | **{triage['nitpick_count']} 件** |")
    lines.append("")
    lines.append("---")
    lines.append("")

    if triage["criticals"]:
        lines.append("## 🚨 1. Critical 指摘事項（マージを即座にブロックすべき欠陥）")
        for idx, item in enumerate(triage["criticals"], 1):
            lines.append(f"### 欠陥 {idx}: {item.get('category', 'クリティカル欠陥')}")
            lines.append(f"- **問題内容**: {item.get('issue')}")
            if item.get("impact"):
                lines.append(f"- **本番実害インパクト**: {item.get('impact')}")
            if item.get("recommendation"):
                lines.append(f"- **改善方針**: {item.get('recommendation')}")
            if item.get("fix_code"):
                lines.append("```python")
                lines.append(item.get("fix_code"))
                lines.append("```")
            lines.append("")

    if triage["warnings"]:
        lines.append("## ⚠️ 2. Warning 指摘事項（改善推奨）")
        for idx, item in enumerate(triage["warnings"], 1):
            lines.append(f"### 注意 {idx}: {item.get('category', '注意')}")
            lines.append(f"- **問題内容**: {item.get('issue')}")
            if item.get("impact"):
                lines.append(f"- **影響**: {item.get('impact')}")
            if item.get("recommendation"):
                lines.append(f"- **改善方針**: {item.get('recommendation')}")
            if item.get("fix_code"):
                lines.append("```python")
                lines.append(item.get("fix_code"))
                lines.append("```")
            lines.append("")

    if triage["nitpicks"]:
        lines.append("## 💡 3. Nitpick 指摘事項（任意対応）")
        for idx, item in enumerate(triage["nitpicks"], 1):
            lines.append(f"- **提案 {idx}**: {item.get('issue')}")
            lines.append("")

    lines.append("---")
    lines.append("## 🤝 心理的安全性とコラボレーションについて")
    lines.append("本レポートの指摘事項は、本番稼働時の予期せぬインシデント（金銭差損・並行障害・セキュリティ漏洩）を未然に防ぎ、コードの長期健全性を保つための技術的提案です。疑問点や代替アプローチがある場合は、気軽にご相談ください。")

    return "\n".join(lines)

def run_panel(target: str = None, diff_file: str = None, domain: str = "general", task_id: str = None, work_dir: str = None, output_file: str = None):
    if not task_id:
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if not work_dir:
        work_dir = PANEL_ROOT / "work" / task_id
    else:
        work_dir = Path(work_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INIT] タスクID: {task_id}")
    print(f"[INIT] 作業ディレクトリ (隔離): {work_dir}")

    content = ""
    target_name = ""
    if diff_file and os.path.exists(diff_file):
        with open(diff_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        target_name = f"Diff: {diff_file}"
    elif target and os.path.exists(target):
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            target_name = f"File: {target}"
        else:
            aggregated = []
            for root, _, files in os.walk(target):
                for file in files:
                    if file.endswith((".py", ".c", ".h", ".ts", ".js", ".sql")):
                        fpath = os.path.join(root, file)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                aggregated.append(f"\n# === FILE: {file} ===\n" + f.read())
                        except Exception:
                            pass
            content = "\n".join(aggregated)
            target_name = f"Directory: {target}"
    else:
        print("[ERROR] --target または --diff の有効なパスを指定してください。")
        sys.exit(1)

    domain_info = load_domain_invariants(domain)
    print(f"[SCAN] ドメイン: {domain} ({domain_info.get('name', 'General')})")

    print("[STAGE 1] Security & Auth Specialist 監査中...")
    sec_findings = analyze_security(content, domain_info)
    with open(work_dir / "01_security_report.json", "w", encoding="utf-8") as f:
        json.dump(sec_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 2] Architecture & Concurrency Specialist 監査中...")
    arch_findings = analyze_architecture(content, domain_info)
    with open(work_dir / "02_architecture_report.json", "w", encoding="utf-8") as f:
        json.dump(arch_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 3] Domain Invariant & Adversarial Specialist 監査中...")
    dom_findings = analyze_domain(content, domain, domain_info)
    with open(work_dir / "03_domain_adversarial_report.json", "w", encoding="utf-8") as f:
        json.dump(dom_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 4] QA & Breaking Changes Guard 監査中...")
    qa_findings = analyze_qa(content, domain_info)
    with open(work_dir / "04_qa_report.json", "w", encoding="utf-8") as f:
        json.dump(qa_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 5] Lead Reviewer 合議・トリアージ処理中...")
    all_findings = sec_findings + arch_findings + dom_findings + qa_findings
    triage = lead_reviewer_triage(all_findings)
    with open(work_dir / "05_consensus_triage.json", "w", encoding="utf-8") as f:
        json.dump(triage, f, ensure_ascii=False, indent=2)

    report_md = generate_markdown_report(task_id, domain, target_name, triage, len(content))
    final_report_path = work_dir / "final_consensus_review.md"
    with open(final_report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    if output_file:
        out_p = Path(output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[SUCCESS] レポートを出力しました: {out_p}")

    print(f"[SUCCESS] 合議制パネル監査完了!")
    print(f"  - 判定: {triage['verdict']}")
    print(f"  - Critical: {triage['critical_count']}件, Warning: {triage['warning_count']}件, Nitpick: {triage['nitpick_count']}件")
    print(f"  - 最終レポート: {final_report_path}")
    return {"report_path": str(final_report_path), "triage": triage}

def main():
    parser = argparse.ArgumentParser(description="Consensus Multi-Expert Review Panel Runner")
    parser.add_argument("--target", type=str, help="Target file or directory to audit")
    parser.add_argument("--diff", type=str, help="Patch / diff file to review")
    parser.add_argument("--domain", type=str, default="general", choices=["general", "fintech", "distributed", "healthcare", "embedded"], help="Target domain")
    parser.add_argument("--task-id", type=str, help="Custom task identifier")
    parser.add_argument("--work-dir", type=str, help="Custom working directory for artifacts")
    parser.add_argument("--output", type=str, help="Custom destination file for the final report")
    # CI から使うときだけ付ける。既定で落とさないのは、このスクリプトが静的パターン検査で
    # あり誤検知しうるため。落としたい CI では明示的に付けさせる。
    parser.add_argument("--fail-on-critical", action="store_true",
                        help="Critical が 1 件でもあれば exit code 1 で終了する (CI 用)")

    args = parser.parse_args()
    if not args.target and not args.diff:
        parser.print_help()
        sys.exit(1)

    result = run_panel(
        target=args.target,
        diff_file=args.diff,
        domain=args.domain,
        task_id=args.task_id,
        work_dir=args.work_dir,
        output_file=args.output
    )

    if args.fail_on_critical and result["triage"]["critical_count"] > 0:
        print("[BLOCK] Critical 指摘があるためマージをブロックします。")
        sys.exit(1)

if __name__ == "__main__":
    main()
