"""
合議制専門家パネル 実行エンジン (Consensus Multi-Expert Review Runner)
CLIおよび別エージェント・agyコマンドからの呼び出しを受け付け、観点別監査を
work/<task_id>/ に隔離保存した上で、合議トリアージレポートを出力する。

本エンジンの位置付け:
  - CI (lint / type / unit test) 通過**後**に走る後段レビューであり、
    CIが機械的に検出できない「意味論的欠陥」のみを対象とする。
  - 本エンジンは **レポート出力専用** であり、対象リポジトリを一切変更しない。
    修正の実施は、本レポートを受け取る依頼元エージェントの責務とする。
  - AGENTS.md 原則1「偽陽性の徹底排除」に従い、確証の持てない事象は
    指摘を **出さない**（Silent Pass）ことを、見逃しよりも優先する。
"""

import os
import sys
import json
import re
import fnmatch
import argparse
from datetime import datetime
from pathlib import Path

# このファイルは 2 種類の配置で動く。
#   1. 上流リポジトリ: <root>/scripts/panel_runner.py  → configs は ../configs
#   2. 他プロジェクトへの vendoring: <root>/review/panel_runner.py → configs は ./configs
# どちらでも同じファイルをそのまま置けるようにしておく（同期を cp 1 回で済ませるため）。
_HERE = Path(__file__).resolve().parent
PANEL_ROOT = _HERE if (_HERE / "configs" / "domain_invariants.json").exists() else _HERE.parent

# 検査するファイル種別。**散文を検査対象にしない**のが要点で、README・SKILL.md・
# CLAUDE.md は「@pytest.mark.skip を使うな」「ISR 内で Mutex を取るな」と
# パターンそのものを引用して説明するため、含めると必ず誤検知になる。
# 検査ロジック自体も Python / SQL 前提で書かれている。
SOURCE_EXTENSIONS = (".py", ".c", ".h", ".cpp", ".ts", ".tsx", ".js", ".jsx", ".sql", ".go", ".rs", ".java")

# 既定の除外パス。パネル自身のプロンプト・不変条件定義・フィクスチャには、
# 検出したいパターン（ハードコードされた鍵、@pytest.mark.skip、ISR 内 Mutex 等）が
# **説明のために書いてある**。除外しないとパネル自身を触る PR が必ず誤検知で落ちる。
DEFAULT_EXCLUDES = ["review/*", "scripts/panel_runner.py", "tests/test_panel_runner.py"]


def is_excluded(path: str, patterns) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(normalized, pat) for pat in patterns)

# ---------------------------------------------------------------------------
# 解析コンテキスト (Diff / File / Directory を統一的に扱う層)
# ---------------------------------------------------------------------------
# 旧実装は「監査対象を1本の巨大な文字列として部分文字列検索する」方式だったため、
#   - diff の削除行 (-) を「追加された実装」と誤認する
#   - 監査対象ディレクトリ内の無関係なテストが、全ファイルの検査を免除してしまう
#   - 単一ファイル監査時に「テストが同梱されていない」と必ず誤検知する
# という構造的な偽陽性を抱えていた。ここでは入力を必ずファイル単位・行単位へ
# 正規化し、「新規に持ち込まれた行 (added)」と「文脈として存在する行 (context)」を
# 明確に分離する。

DIFF_NEWFILE_RE = re.compile(r'^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$')
DIFF_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')

TEST_DIR_RE = re.compile(r'(^|/)(tests?|__tests__|spec)(/|$)')
TEST_FILE_RE = re.compile(r'^(test_.+\.py|.+_test\.(py|go|ts|js)|.+\.(test|spec)\.[jt]sx?|conftest\.py|.+Test\.java)$')


def is_test_path(path: str) -> bool:
    """テストコードとして扱うべきパスかを判定する。"""
    p = path.replace("\\", "/").lower()
    directory, _, name = p.rpartition("/")
    if TEST_DIR_RE.search(directory):
        return True
    return bool(TEST_FILE_RE.match(name))


def is_source_path(path: str) -> bool:
    """実装コード (ドキュメント・設定以外) として扱うべきパスかを判定する。"""
    return path.replace("\\", "/").lower().endswith(SOURCE_EXTENSIONS)


COMMENT_LINE_RE = re.compile(r'^\s*(?:#|//|\*|/\*|--\s)')


def is_comment_line(text: str) -> bool:
    """行全体がコメントか。仕様の記述例やTODOでの発火を防ぐため検査対象から外す。"""
    return bool(COMMENT_LINE_RE.match(text))


def has_keyword(text: str, keywords) -> bool:
    """単語境界つきのキーワード判定。

    'can' が 'cancel' / 'scan' に、'key' が 'monkey' に一致するといった
    部分文字列マッチによる誤ったドメイン判定を防ぐ。
    """
    pattern = r'(?<![A-Za-z0-9_])(?:' + "|".join(re.escape(k) for k in keywords) + r')(?![A-Za-z0-9_])'
    return bool(re.search(pattern, text, re.IGNORECASE))


class FileChange:
    """1ファイル分の変更内容。非diff監査では全行を added として扱う。"""

    def __init__(self, path: str):
        self.path = path
        self.added = []    # list[(new_lineno, text)]
        self.removed = []  # list[(old_lineno, text)]
        self.context = []  # list[(new_lineno, text)]  diff の変更なし行
        self.is_test = is_test_path(path)
        self.is_source = is_source_path(path)

    @property
    def added_text(self) -> str:
        """追加された「コード行」。コメント行はパターン検査の対象外。"""
        return "\n".join(t for _, t in self.added if not is_comment_line(t))

    @property
    def removed_text(self) -> str:
        return "\n".join(t for _, t in self.removed)

    @property
    def scan_text(self) -> str:
        """追加行＋文脈行。'この変更後のコードに何が存在するか' を見るための本文。"""
        merged = sorted(self.added + self.context, key=lambda x: x[0])
        return "\n".join(t for _, t in merged if not is_comment_line(t))


class ReviewContext:
    """全ファイルを束ねた監査対象。各アナライザはこのコンテキストのみを参照する。"""

    def __init__(self, mode: str, files: list, raw: str, target_name: str, dropped: list = None):
        self.mode = mode  # 'diff' | 'file' | 'directory'
        self.files = files
        self.raw = raw
        self.target_name = target_name
        self.dropped = dropped or []  # 除外パターン・対象外拡張子で落としたパス

    @property
    def filtered_empty(self) -> bool:
        """入力はあったが、除外の結果として検査対象が残らなかった状態。

        「検査した結果 APPROVE」と「何も検査していない」を混同させないため、
        レポートで明示する必要がある。
        """
        return bool(self.raw.strip()) and not self.files

    @property
    def is_diff(self) -> bool:
        return self.mode == "diff"

    @property
    def scan_text(self) -> str:
        """パターン検出用の本文。diff の削除行は含まない。"""
        return "\n".join(f.scan_text for f in self.files)

    @property
    def added_text(self) -> str:
        """『この変更で新たに持ち込まれた』ことが確実な行のみ。"""
        return "\n".join(f.added_text for f in self.files)

    @property
    def removed_text(self) -> str:
        return "\n".join(f.removed_text for f in self.files)

    def source_files(self) -> list:
        return [f for f in self.files if f.is_source and not f.is_test]

    def test_files(self) -> list:
        return [f for f in self.files if f.is_test]

    def locate(self, pattern, in_added: bool = True, limit: int = 5,
               code_only: bool = False, include_comments: bool = False) -> list:
        """該当行の 'path:line' を最大 limit 件返す。依頼元エージェントの修正着手点。

        code_only=True の場合、実装・テストコード以外（Markdown 等の散文）を除外する。
        レビュー観点ドキュメントがマーカー名に言及しただけで発火する事故を防ぐ。
        """
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        hits = []
        for f in self.files:
            if code_only and not (f.is_source or f.is_test):
                continue
            rows = f.added if in_added else f.context
            for lineno, text in rows:
                if not include_comments and is_comment_line(text):
                    continue
                if regex.search(text):
                    hits.append(f"{f.path}:{lineno}")
                    if len(hits) >= limit:
                        return hits
        return hits

    def scope_description(self) -> str:
        n_files = len(self.files)
        suffix = f" / 除外 {len(self.dropped)} ファイル" if self.dropped else ""
        if self.mode == "diff":
            n_added = sum(len(f.added) for f in self.files)
            n_removed = sum(len(f.removed) for f in self.files)
            return f"差分監査 ({n_files} ファイル / +{n_added} 行 / -{n_removed} 行{suffix})"
        if self.mode == "file":
            return "単一ファイル全体監査 (差分情報なし)"
        return f"ディレクトリ全体監査 ({n_files} ファイル{suffix})"


def looks_like_diff(content: str) -> bool:
    return bool(DIFF_HUNK_RE.search(content)) and ("+++ " in content or "--- " in content)


def parse_diff(content: str) -> list:
    """unified diff をファイル単位・行単位に分解する。"""
    files = []
    current = None
    new_lineno = 0
    old_lineno = 0

    for line in content.splitlines():
        if line.startswith("diff --git ") or line.startswith("Index: "):
            current = None
            continue
        if line.startswith("--- "):
            continue

        m = DIFF_NEWFILE_RE.match(line)
        if m:
            path = m.group(1).strip()
            if path == "/dev/null":
                # 削除されたファイル。追加行は存在しないため追跡対象外。
                current = None
                continue
            current = FileChange(path)
            files.append(current)
            new_lineno = old_lineno = 0
            continue

        m = DIFF_HUNK_RE.match(line)
        if m:
            old_lineno = int(m.group(1))
            new_lineno = int(m.group(2))
            continue

        if current is None or new_lineno == 0:
            continue

        if line.startswith("\\"):  # \ No newline at end of file
            continue
        if line.startswith("+"):
            current.added.append((new_lineno, line[1:]))
            new_lineno += 1
        elif line.startswith("-"):
            current.removed.append((old_lineno, line[1:]))
            old_lineno += 1
        else:
            text = line[1:] if line.startswith(" ") else line
            current.context.append((new_lineno, text))
            new_lineno += 1
            old_lineno += 1

    return files


def build_context(target: str = None, diff_file: str = None, excludes=None) -> ReviewContext:
    """CLI引数から監査対象を読み込み、正規化された ReviewContext を構築する。"""
    patterns = list(DEFAULT_EXCLUDES) + list(excludes or [])

    def read(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def whole_file_change(path, text, display_path=None):
        fc = FileChange(display_path or path)
        fc.added = [(i, line) for i, line in enumerate(text.splitlines(), 1)]
        return fc

    def sift(files):
        """検査対象外のファイルを落とす。(残したもの, 落としたパス)"""
        kept, dropped = [], []
        for fc in files:
            if is_excluded(fc.path, patterns) or not fc.is_source:
                dropped.append(fc.path)
            else:
                kept.append(fc)
        return kept, dropped

    def diff_context(raw, name):
        kept, dropped = sift(parse_diff(raw))
        return ReviewContext("diff", kept, raw, name, dropped)

    if diff_file and os.path.exists(diff_file):
        return diff_context(read(diff_file), f"Diff: {diff_file}")

    if target and os.path.exists(target):
        if os.path.isfile(target):
            raw = read(target)
            # .patch/.diff を --target で渡された場合も差分として正しく扱う。
            if looks_like_diff(raw):
                return diff_context(raw, f"Diff: {target}")
            # ファイルを名指しされた場合は除外を適用しない。
            # 除外パス配下を意図して見せていることがある（煙試験など）。
            return ReviewContext("file", [whole_file_change(target, raw)], raw, f"File: {target}")

        files = []
        chunks = []
        base = Path(target)
        for root, dirs, names in os.walk(target):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
            for name in names:
                if not is_source_path(name):
                    continue
                fpath = Path(root) / name
                try:
                    text = read(fpath)
                except Exception:
                    continue
                try:
                    rel = str(fpath.relative_to(base)).replace("\\", "/")
                except ValueError:
                    rel = str(fpath).replace("\\", "/")
                files.append(whole_file_change(str(fpath), text, display_path=rel))
                chunks.append(f"\n# === FILE: {rel} ===\n{text}")
        kept, dropped = sift(files)
        return ReviewContext("directory", kept, "\n".join(chunks), f"Directory: {target}", dropped)

    return None


def load_domain_invariants(domain: str) -> dict:
    config_path = PANEL_ROOT / "configs" / "domain_invariants.json"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        invariants = json.load(f)
    return invariants.get(domain, invariants.get("general", {}))


def finding(severity, category, issue, impact, recommendation, fix_code=None, locations=None):
    item = {
        "severity": severity,
        "category": category,
        "issue": issue,
        "impact": impact,
        "recommendation": recommendation,
    }
    if fix_code:
        item["fix_code"] = fix_code
    if locations:
        item["locations"] = locations
    return item


# ---------------------------------------------------------------------------
# STAGE 1: Security & Auth
# ---------------------------------------------------------------------------
# 代入先の名前だけでなく「右辺が文字列リテラルであること」まで要求する。
HARDCODED_SECRET_RE = re.compile(
    r'(?i)\b(secret_key|api_key|apikey|password|passwd|access_token|private_key)\b'
    r'\s*[:=]\s*(?![=])[\'"][^\'"\n]{4,}[\'"]'
)


def analyze_security(ctx: ReviewContext, domain_info: dict) -> list:
    findings = []
    added = ctx.added_text
    scan = ctx.scan_text

    # 1. Timing Attack
    if "==" in added and has_keyword(added, ["token", "secret", "key", "hash"]):
        if "compare_digest" not in scan:
            findings.append(finding(
                "CRITICAL", "Timing Attack Vulnerability",
                "秘密トークンまたはハッシュの比較に通常の '==' 演算子が使用されています。",
                "攻撃者が応答時間の微小な差異（ナノ秒単位）を統計解析し、認証バイパスを行うリスクがあります。",
                "hmac.compare_digest() を使用して定数時間で比較を行ってください。",
                "# 修正例:\nimport hmac\nis_valid = hmac.compare_digest(user_token, secret_token)",
                ctx.locate(r'==.*(token|secret|key|hash)|(token|secret|key|hash).*==')
            ))

    # 2. Hardcoded secret
    #    右辺が文字列リテラルの場合のみ指摘する。os.environ / vault / 引数からの
    #    取得（= 正しい実装）を「ハードコード」と誤認しないため。
    if HARDCODED_SECRET_RE.search(added):
        findings.append(finding(
            "CRITICAL", "Hardcoded Credential",
            "機密情報（シークレットキー/APIキー）がコード内に直接ハードコードされています。",
            "リポジトリ閲覧者による不正アクセスや情報漏洩に直結します。",
            "環境変数またはシークレットマネージャーから取得するように改修してください。",
            "# 修正例:\nimport os\nSECRET_KEY = os.environ.get('APP_SECRET_KEY')",
            ctx.locate(HARDCODED_SECRET_RE)
        ))

    # 3. IDOR
    if "user_id" in added and ("query_params" in scan or "Query(" in scan):
        findings.append(finding(
            "CRITICAL", "IDOR (Insecure Direct Object References)",
            "リクエストパラメータ経由で受け取った user_id で他者のリソースにアクセスできる構造です。",
            "認証済み悪意ユーザーが他人の決済情報や非公開データを不正閲覧・改ざんできます。",
            "リクエストパラメータではなく、認証コンテキスト (current_user.id) からユーザーを特定してください。",
            "# 修正例:\nasync def get_resource(current_user: User = Depends(get_current_active_user)):\n    return await fetch_user_data(user_id=current_user.id)",
            ctx.locate(r'user_id')
        ))

    return findings


# ---------------------------------------------------------------------------
# STAGE 2: Architecture & Concurrency
# ---------------------------------------------------------------------------
def analyze_architecture(ctx: ReviewContext, domain_info: dict) -> list:
    findings = []
    added = ctx.added_text
    scan = ctx.scan_text

    # 1. N+1 Query (ループ本体は文脈行のこともあるため scan を併用)
    if "for " in scan and any(q in added for q in ["SELECT ", "select ", ".fetch_", ".execute("]):
        findings.append(finding(
            "WARNING", "N+1 Query Bottleneck",
            "ループ処理の内部で個別にSQLクエリまたは非同期取得が実行されています。",
            "対象データ数が数十〜数百件に増加した際、DB往復オーバーヘッドによりレイテンシが数十倍に悪化します。",
            "IN / ANY 句を用いたバルク一括フェッチクエリに統合してください。",
            "# 修正例:\nquery = 'SELECT * FROM items WHERE id = ANY(:ids)'\nresults = await db.fetch_all(query=query, values={'ids': item_ids})",
            ctx.locate(r'SELECT |select |\.fetch_|\.execute\(')
        ))

    # 2. Deadlock Risk
    if "async with" in added and "lock" in added.lower() and "sorted(" not in scan:
        findings.append(finding(
            "CRITICAL", "Deadlock Risk in Concurrency",
            "複数リソースへの排他ロック取得順序が保証されておらず、並行アクセス時のデッドロックリスクがあります。",
            "逆順ロック待機によりプロセス全体が永久フリーズし、サービス停止に至ります。",
            "ロック取得対象のIDリストを常に昇順（sorted）に整列してから順次ロックを取得してください。",
            "# 修正例:\nfor resource_id in sorted(target_ids):\n    async with get_lock(resource_id):\n        # 安全な順次処理",
            ctx.locate(r'async with .*lock')
        ))

    return findings


# ---------------------------------------------------------------------------
# STAGE 3: Domain Invariants & Adversarial
# ---------------------------------------------------------------------------
def analyze_domain(ctx: ReviewContext, domain: str, domain_info: dict) -> list:
    findings = []
    added = ctx.added_text
    scan = ctx.scan_text
    scan_l = scan.lower()

    # 1. FinTech
    if domain == "fintech" or has_keyword(added, ["margin", "currency", "price", "interest", "balance"]):
        if re.search(r'\bfloat\(', added) or ": float" in added or "float =" in added:
            findings.append(finding(
                "CRITICAL", "Domain Invariant Violation (IEEE 754 Float)",
                "金融計算・金利・証拠金計算に float (二進浮動小数点数) が使用されています。",
                "二進表現の微小丸め誤差が複利計算や大量取引で累積し、重大な帳簿残高乖離・法的監査違反を招きます。",
                "decimal.Decimal を使用し、丸めモード (ROUND_HALF_EVEN等) を明示的に指定してください。",
                "# 修正例:\nfrom decimal import Decimal, ROUND_HALF_EVEN\namount = Decimal(str(raw_val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)",
                ctx.locate(r'\bfloat\(|:\s*float|float\s*=')
            ))
        if "balance" in added and ("withdraw" in added or "UPDATE" in added) \
                and "for update" not in scan_l and "lock" not in scan_l:
            findings.append(finding(
                "CRITICAL", "Domain Invariant Violation (TOCTOU Double Spending)",
                "残高確認から出金・残高更新までの処理がアトミックに保護されていません。",
                "並行して複数の出金リクエストが到達した場合、残高以上の引き出し（二重出金）が発生します。",
                "DBトランザクション内の SELECT FOR UPDATE または分散排他ロックを導入してください。",
                "# 修正例 (SQL):\nSELECT balance FROM accounts WHERE id = :id FOR UPDATE;",
                ctx.locate(r'balance')
            ))

    # 2. Distributed
    if domain == "distributed" or has_keyword(added, ["kafka", "outbox", "rabbit"]):
        if "commit" in added and ("send(" in added or "producer" in added):
            if "outbox" not in scan_l:
                findings.append(finding(
                    "CRITICAL", "Domain Invariant Violation (Dual Write Pattern)",
                    "DBコミットとメッセージブローカーへの直接送信が分離して実行されています。",
                    "送信直前のクラッシュやネットワークエラー時に配送イベントが永久消失し、幽霊注文や不整合が発生します。",
                    "Transactional Outbox パターンを採用し、同一DBトランザクション内にイベントを永続化してください。",
                    "# 修正例:\nawait db.execute('INSERT INTO outbox_events (...) VALUES (...)')",
                    ctx.locate(r'commit|producer|send\(')
                ))

    # 3. Healthcare
    if domain == "healthcare" or has_keyword(added, ["dose", "patient", "clinical"]):
        if "mg/dl" in scan_l and "umol" in scan_l and "conversion" not in scan_l:
            findings.append(finding(
                "CRITICAL", "Domain Invariant Violation (Unit Mismatch)",
                "クレアチニン値等の臨床単位 (mg/dL と µmol/L) の未換算・混同リスクが検出されました。",
                "約88倍の計算乖離が発生し、患者への致死量過剰投与または重篤な急性腎不全を招きます。",
                "Pydantic単位型安全バリデータを適用し、受取時にISO単位へ厳密正規化してください。",
                locations=ctx.locate(r'(?i)mg/dl|umol')
            ))

    # 4. Embedded
    if domain == "embedded" or has_keyword(added, ["can", "isr", "rtos"]):
        if has_keyword(added, ["isr"]) and has_keyword(added, ["mutex", "lock", "malloc"]):
            findings.append(finding(
                "CRITICAL", "Domain Invariant Violation (Blocking in ISR)",
                "割込みサービスルーチン (ISR) 内でブロッキング同期または動的メモリ確保が検出されました。",
                "割込みコンテキストでの永久デッドロックおよびハードウェア緊急停止の不能を招きます。",
                "ISR内は非同期イベントフラグのセットのみ行い、処理はタスクスレッドへ委譲してください。",
                locations=ctx.locate(r'(?i)isr')
            ))

    return findings


# ---------------------------------------------------------------------------
# STAGE 4: QA & Breaking Changes Guard
# ---------------------------------------------------------------------------
# テスト品質の判定は「文字列がどこかに存在するか」ではなく、
# 「この変更で追加されたテスト関数が、実際に何を検証しているか」で行う。

TEST_DEF_RE = re.compile(r'^(\s*)(?:async\s+)?def\s+(test_[A-Za-z0-9_]*)\s*\(')
LOGIC_DEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+[A-Za-z_]\w*\s*\(|^\s*class\s+[A-Za-z_]\w*\s*[\(:]')
# 値・状態・例外を検証する「実効アサーション」。mock の呼び出し検証は含めない。
VALUE_ASSERT_RE = re.compile(
    r'(?:^|[^\w.])assert\b|self\.assert[A-Za-z]+\(|pytest\.raises|pytest\.warns'
    r'|expect\(|assertThat\(|np\.testing\.assert|\.should\b'
)
# mock の呼び出し有無だけを見るアサーション。
MOCK_ASSERT_RE = re.compile(r'\.assert_(?:called|any_call|has_calls|not_called|awaited)[A-Za-z_]*\(')
MOCK_USAGE_RE = re.compile(r'(?:mock\.patch|@patch|MagicMock|AsyncMock|Mock\(|monkeypatch\.)')
SKIP_MARKER_RE = re.compile(
    r'@pytest\.mark\.skip|@pytest\.mark\.xfail|@unittest\.skip|pytest\.skip\('
    r'|\bit\.skip\(|\bdescribe\.skip\(|\bxit\(|\bt\.Skip\('
)
COMMENTED_TEST_RE = re.compile(r'^\s*(?:#|//)\s*(?:(?:async\s+)?def\s+test_|assert\b|self\.assert)')


def _extract_added_test_functions(fc: FileChange) -> list:
    """追加行から『この変更で新規に定義されたテスト関数』とその本体を抽出する。

    本体が追加行として連続していない場合（既存関数の部分修正など）は
    全体像を判定できないため complete=False とし、指摘対象から除外する。
    これは AGENTS.md 原則1（偽陽性の排除）に基づく意図的な取りこぼしである。

    テストファイル以外は対象外とする。本番コードの `def test_connection()` 等を
    テスト関数と誤認しないため。
    """
    functions = []
    if not fc.is_test:
        return functions
    lines = fc.added
    i = 0
    while i < len(lines):
        lineno, text = lines[i]
        m = TEST_DEF_RE.match(text)
        if not m:
            i += 1
            continue

        indent = len(m.group(1))

        # 直前に連続するデコレータ行 (@patch(...) 等) は関数の一部として扱う。
        decorators = []
        k = i - 1
        expected = lineno - 1
        while k >= 0 and lines[k][0] == expected and lines[k][1].lstrip().startswith("@"):
            decorators.insert(0, lines[k][1])
            expected -= 1
            k -= 1

        body = []
        complete = True
        prev_lineno = lineno
        j = i + 1
        while j < len(lines):
            ln, t = lines[j]
            if ln != prev_lineno + 1:
                complete = False  # 途中に未変更行が挟まる = 部分改修
                break
            if t.strip() and (len(t) - len(t.lstrip())) <= indent:
                break  # 同階層以下へデデント = 関数終了
            body.append(t)
            prev_lineno = ln
            j += 1

        functions.append({
            "name": m.group(2),
            "location": f"{fc.path}:{lineno}",
            "body": "\n".join(body),
            "decorators": "\n".join(decorators),
            "complete": complete,
        })
        i = max(j, i + 1)
    return functions


def analyze_qa(ctx: ReviewContext, domain_info: dict) -> list:
    findings = []
    added = ctx.added_text
    scan = ctx.scan_text

    # 1. Breaking Migration (ゼロダウンタイム違反)
    if "add_column" in added and "nullable=False" in added and "server_default" not in scan:
        findings.append(finding(
            "CRITICAL", "Breaking Migration (Zero-Downtime Violation)",
            "既存データが存在するテーブルに対し、デフォルト値のない NOT NULL カラムが直接追加されています。",
            "本番環境でのマイグレーション実行時に既存行への制約違反でDB更新が失敗し、デプロイが中断・クラッシュします。",
            "段階的マイグレーション（1. NULL許容で追加 → 2. 既存行バックフィル → 3. NOT NULL制約適用）を行ってください。",
            "# 修正例 (Alembic):\nop.add_column('table', sa.Column('col', sa.String(), server_default='default_val', nullable=False))",
            ctx.locate(r'add_column')
        ))

    # 2. Test Suppression (テスト隠蔽・無力化)
    #    「追加された」スキップマーカーのみを対象とする。既存のスキップや、
    #    スキップに言及しただけのドキュメント・レビュー定義ファイルでは発火しない。
    skip_locs = ctx.locate(SKIP_MARKER_RE, code_only=True)
    comment_locs = ctx.locate(COMMENTED_TEST_RE, code_only=True, include_comments=True)
    if skip_locs or comment_locs:
        findings.append(finding(
            "CRITICAL", "Test Suppression & Quality Regression",
            "テストケースがスキップマーカーまたはコメントアウトによって無力化されています。",
            "既存の正常動作・後方互換性が破壊された事実がCIで隠蔽され、本番不具合の原因となります。",
            "テストをスキップせず、実装側を修正して既存テストの契約を満たしてください。恒久的に不要なテストであれば、削除理由をコミットメッセージに明記してください。",
            locations=(skip_locs + comment_locs)[:5]
        ))

    # 3. Test Deletion (テストの消滅)
    #    差分監査時のみ。削除されたテスト関数名が、どこにも再追加されていない場合に指摘。
    if ctx.is_diff:
        removed_tests = {}
        for fc in ctx.files:
            if not fc.is_test:
                continue
            for lineno, text in fc.removed:
                m = TEST_DEF_RE.match(text)
                if m:
                    removed_tests.setdefault(m.group(2), f"{fc.path}:{lineno}")
        readded = {f["name"] for fc in ctx.files for f in _extract_added_test_functions(fc)}
        orphaned = {n: loc for n, loc in removed_tests.items() if n not in readded}
        if orphaned:
            names = ", ".join(sorted(orphaned)[:5])
            findings.append(finding(
                "CRITICAL", "Test Deletion (Silent Coverage Loss)",
                f"既存のテスト関数が、同名の代替を伴わずに削除されています ({names})。",
                "削除されたテストが守っていた契約は以後CIで検証されず、退行が無検知で本番に到達します。テスト削除はCIをグリーンに保ったまま実施できるため、CI通過は安全性の根拠になりません。",
                "対象仕様が廃止されたのであれば削除理由をPR説明に明記し、仕様が存続するのであれば同等の検証を行う代替テストを提示してください。",
                locations=list(orphaned.values())[:5]
            ))

    # 4. Assertion Weakening (アサーションの削減)
    if ctx.is_diff:
        removed_asserts = 0
        added_asserts = 0
        weakened_locs = []
        for fc in ctx.files:
            if not fc.is_test:
                continue
            for lineno, text in fc.removed:
                if VALUE_ASSERT_RE.search(text):
                    removed_asserts += 1
                    if len(weakened_locs) < 5:
                        weakened_locs.append(f"{fc.path}:{lineno}")
            for _, text in fc.added:
                if VALUE_ASSERT_RE.search(text):
                    added_asserts += 1
        if removed_asserts > added_asserts:
            findings.append(finding(
                "WARNING", "Assertion Weakening",
                f"テストコードから実効アサーションが正味 {removed_asserts - added_asserts} 件減少しています。",
                "検証条件を緩めてCIを通した場合、テストは存在するのに欠陥を捕捉できない状態になり、品質ゲートが形骸化します。",
                "アサーションを削除・緩和した理由を明示してください。実装側の仕様変更が理由であれば、新しい期待値に対する等価な強度のアサーションへ置き換えてください。",
                locations=weakened_locs
            ))

    # 5. Diff Coverage & Test Omission (テスト不足)
    #    単一ファイル監査ではテストの所在を判断できないため、意図的に評価しない。
    if ctx.mode in ("diff", "directory"):
        logic_files = []
        for fc in ctx.source_files():
            locs = [f"{fc.path}:{ln}" for ln, t in fc.added if LOGIC_DEF_RE.match(t)]
            if locs:
                logic_files.append((fc, locs))

        test_activity = any(
            _extract_added_test_functions(fc) or any(VALUE_ASSERT_RE.search(t) for _, t in fc.added)
            for fc in ctx.test_files()
        )
        if logic_files and not test_activity:
            locations = [loc for _, locs in logic_files for loc in locs][:5]
            names = ", ".join(sorted({fc.path for fc, _ in logic_files})[:5])
            findings.append(finding(
                "WARNING", "Diff Coverage & Test Omission",
                f"実装ロジック（関数・クラス）が追加されていますが、対応するテストの追加が確認できません ({names})。",
                "未検証のロジックが本番へ到達し、異常系・境界値で初めて破綻します。CIは『既存テストが通ること』しか保証しないため、新規ロジックの正しさはCI通過では担保されません。",
                "追加ロジックの正常系・異常系・境界値を対象とするテストを追加してください。既存テストで十分にカバーされている場合は、その対応関係をPR説明に明記してください。",
                locations=locations
            ))

    # 6. No-Assertion Test / Over-Mocking (追加されたテスト関数の実効性)
    empty_tests = []
    mock_only_tests = []
    for fc in ctx.files:
        for fn in _extract_added_test_functions(fc):
            if not fn["complete"]:
                continue  # 本体全体を観測できていない = 判定不能
            body = fn["body"]
            has_value_assert = bool(VALUE_ASSERT_RE.search(body))
            has_mock_assert = bool(MOCK_ASSERT_RE.search(body))
            if not has_value_assert and not has_mock_assert:
                empty_tests.append(fn)
            elif not has_value_assert and has_mock_assert and MOCK_USAGE_RE.search(fn["decorators"] + "\n" + body):
                mock_only_tests.append(fn)

    if empty_tests:
        names = ", ".join(fn["name"] for fn in empty_tests[:5])
        findings.append(finding(
            "CRITICAL", "Meaningless / No-Assertion Test",
            f"追加されたテスト関数に実効的なアサーションが1つも含まれていません ({names})。",
            "『例外を送出しないこと』しか確認しておらず、戻り値や状態遷移が誤っていてもテストは成功します。カバレッジ数値だけが上昇し、品質ゲートが実質的に無効化されます。",
            "期待される戻り値・状態変化（DBレコード数、発行イベント、例外種別など）に対する明示的な assert を記述してください。",
            "# 修正例:\ndef test_apply_discount_rounds_half_even():\n    result = apply_discount(Decimal('100.005'), rate=Decimal('0.1'))\n    assert result == Decimal('90.00')          # 戻り値\n    assert order.status == OrderStatus.APPLIED  # 状態遷移\n\ndef test_apply_discount_rejects_negative_rate():\n    with pytest.raises(ValueError):\n        apply_discount(Decimal('100'), rate=Decimal('-0.1'))",
            [fn["location"] for fn in empty_tests[:5]]
        ))

    if mock_only_tests:
        names = ", ".join(fn["name"] for fn in mock_only_tests[:5])
        findings.append(finding(
            "WARNING", "Over-Mocking (Tautological Test)",
            f"追加されたテストの検証が mock の呼び出し確認のみで、実データに対する値の検証がありません ({names})。",
            "テストが『モックに指示した通りにモックが動いたこと』を確認するだけの同語反復になっており、実装のロジックそのものは一切検証されません。実装を書き換えてもテストは成功し続けます。",
            "外部I/O（HTTP・決済API・時刻）の境界のみをモックし、計算・分岐といった中核ロジックは実物を実行して戻り値を assert してください。",
            "# 修正例:\n@patch('billing.gateway.charge')          # 外部APIのみモック\ndef test_checkout_totals(mock_charge):\n    mock_charge.return_value = ChargeResult(ok=True)\n    total = checkout(cart, coupon='SAVE10')   # 中核ロジックは実物を実行\n    assert total == Decimal('90.00')          # 値そのものを検証",
            [fn["location"] for fn in mock_only_tests[:5]]
        ))

    return findings


# ---------------------------------------------------------------------------
# STAGE 5: Lead Reviewer Triage
# ---------------------------------------------------------------------------
def lead_reviewer_triage(all_findings: list) -> dict:
    criticals, warnings, nitpicks = [], [], []

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
        "nitpicks": nitpicks,
    }


def _render_item(lines: list, idx: int, item: dict, label: str, impact_label: str):
    lines.append(f"### {label} {idx}: {item.get('category', label)}")
    lines.append(f"- **問題内容**: {item.get('issue')}")
    if item.get("locations"):
        lines.append(f"- **該当箇所**: " + ", ".join(f"`{loc}`" for loc in item["locations"]))
    if item.get("impact"):
        lines.append(f"- **{impact_label}**: {item.get('impact')}")
    if item.get("recommendation"):
        lines.append(f"- **改善方針**: {item.get('recommendation')}")
    if item.get("fix_code"):
        lines.append("```python")
        lines.append(item.get("fix_code"))
        lines.append("```")
    lines.append("")


def generate_markdown_report(task_id: str, domain: str, target: str, triage: dict,
                             scope: str = "", filtered_empty: bool = False) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("# 合議制専門家パネル コード監査＆トリアージレポート")
    lines.append(f"- **監査タスクID**: `{task_id}`")
    lines.append(f"- **対象ドメイン**: `{domain}`")
    lines.append(f"- **対象ソース**: `{target}`")
    if scope:
        lines.append(f"- **解析スコープ**: `{scope}`")
    lines.append(f"- **監査実施日時**: `{now_str}`")
    lines.append(f"- **総合判定**: **{triage['verdict']}** ({triage['verdict_comment']})")
    lines.append("")
    lines.append("> 本パネルはCI通過後に走る意味論レビューであり、**レポート出力専用**です。")
    lines.append("> 対象リポジトリへの修正は行いません。修正の適用可否と実施は依頼元エージェントが判断してください。")
    lines.append("")
    if filtered_empty:
        # 「検査した結果 APPROVE」と「何も検査していない」を混同させない。
        lines.append("> ⚠️ **入力はありましたが、除外の結果として検査対象のコードが 1 行も残りませんでした。**")
        lines.append("> この判定は「問題なし」ではなく「検査していない」を意味します。")
        lines.append("")
    lines.append("## 監査概要サマリー")
    lines.append("| 🚨 Critical (即時ブロック) | ⚠️ Warning (改善推奨) | 💡 Nitpick (軽微) |")
    lines.append("| :---: | :---: | :---: |")
    lines.append(f"| **{triage['critical_count']} 件** | **{triage['warning_count']} 件** | **{triage['nitpick_count']} 件** |")
    lines.append("")
    lines.append("---")
    lines.append("")

    if triage["criticals"]:
        lines.append("## 🚨 1. Critical 指摘事項（マージを即座にブロックすべき欠陥）")
        for idx, item in enumerate(triage["criticals"], 1):
            _render_item(lines, idx, item, "欠陥", "本番実害インパクト")

    if triage["warnings"]:
        lines.append("## ⚠️ 2. Warning 指摘事項（改善推奨）")
        for idx, item in enumerate(triage["warnings"], 1):
            _render_item(lines, idx, item, "注意", "影響")

    if triage["nitpicks"]:
        lines.append("## 💡 3. Nitpick 指摘事項（任意対応）")
        for idx, item in enumerate(triage["nitpicks"], 1):
            lines.append(f"- **提案 {idx}**: {item.get('issue')}")
        lines.append("")

    lines.append("---")
    lines.append("## 🤝 心理的安全性とコラボレーションについて")
    lines.append("本レポートの指摘事項は、本番稼働時の予期せぬインシデント（金銭差損・並行障害・セキュリティ漏洩）を未然に防ぎ、コードの長期健全性を保つための技術的提案です。疑問点や代替アプローチがある場合は、気軽にご相談ください。")

    return "\n".join(lines)


def run_panel(target: str = None, diff_file: str = None, domain: str = "general",
              task_id: str = None, work_dir: str = None, output_file: str = None,
              json_file: str = None, excludes=None):
    if not task_id:
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    work_dir = Path(work_dir) if work_dir else PANEL_ROOT / "work" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INIT] タスクID: {task_id}")
    print(f"[INIT] 作業ディレクトリ (隔離): {work_dir}")

    ctx = build_context(target=target, diff_file=diff_file, excludes=excludes)
    if ctx is None:
        print("[ERROR] --target または --diff の有効なパスを指定してください。")
        sys.exit(1)

    domain_info = load_domain_invariants(domain)
    print(f"[SCAN] ドメイン: {domain} ({domain_info.get('name', 'General')})")
    print(f"[SCAN] 解析スコープ: {ctx.scope_description()}")
    if ctx.dropped:
        shown = ", ".join(ctx.dropped[:5]) + (" ..." if len(ctx.dropped) > 5 else "")
        print(f"[SKIP] 検査対象外 {len(ctx.dropped)} ファイル: {shown}")
    if ctx.filtered_empty:
        print("[WARN] 除外の結果、検査対象のコードが残りませんでした（検査は行われていません）。")

    print("[STAGE 1] Security & Auth Specialist 監査中...")
    sec_findings = analyze_security(ctx, domain_info)
    with open(work_dir / "01_security_report.json", "w", encoding="utf-8") as f:
        json.dump(sec_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 2] Architecture & Concurrency Specialist 監査中...")
    arch_findings = analyze_architecture(ctx, domain_info)
    with open(work_dir / "02_architecture_report.json", "w", encoding="utf-8") as f:
        json.dump(arch_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 3] Domain Invariant & Adversarial Specialist 監査中...")
    dom_findings = analyze_domain(ctx, domain, domain_info)
    with open(work_dir / "03_domain_adversarial_report.json", "w", encoding="utf-8") as f:
        json.dump(dom_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 4] QA & Breaking Changes Guard 監査中...")
    qa_findings = analyze_qa(ctx, domain_info)
    with open(work_dir / "04_qa_report.json", "w", encoding="utf-8") as f:
        json.dump(qa_findings, f, ensure_ascii=False, indent=2)

    print("[STAGE 5] Lead Reviewer 合議・トリアージ処理中...")
    all_findings = sec_findings + arch_findings + dom_findings + qa_findings
    triage = lead_reviewer_triage(all_findings)
    triage["task_id"] = task_id
    triage["domain"] = domain
    triage["target"] = ctx.target_name
    triage["scope"] = ctx.scope_description()
    with open(work_dir / "05_consensus_triage.json", "w", encoding="utf-8") as f:
        json.dump(triage, f, ensure_ascii=False, indent=2)

    report_md = generate_markdown_report(task_id, domain, ctx.target_name, triage,
                                         ctx.scope_description(), ctx.filtered_empty)
    final_report_path = work_dir / "final_consensus_review.md"
    with open(final_report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    if output_file:
        out_p = Path(output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[SUCCESS] レポートを出力しました: {out_p}")

    if json_file:
        json_p = Path(json_file)
        json_p.parent.mkdir(parents=True, exist_ok=True)
        with open(json_p, "w", encoding="utf-8") as f:
            json.dump(triage, f, ensure_ascii=False, indent=2)
        print(f"[SUCCESS] 機械可読レポートを出力しました: {json_p}")

    print("[SUCCESS] 合議制パネル監査完了!")
    print(f"  - 判定: {triage['verdict']}")
    print(f"  - Critical: {triage['critical_count']}件, Warning: {triage['warning_count']}件, Nitpick: {triage['nitpick_count']}件")
    print(f"  - 最終レポート: {final_report_path}")
    return {"report_path": str(final_report_path), "triage": triage}


def main():
    parser = argparse.ArgumentParser(description="Consensus Multi-Expert Review Panel Runner (report-only)")
    parser.add_argument("--target", type=str, help="Target file or directory to audit")
    parser.add_argument("--diff", type=str, help="Patch / diff file to review")
    parser.add_argument("--domain", type=str, default="general",
                        choices=["general", "fintech", "distributed", "healthcare", "embedded"], help="Target domain")
    parser.add_argument("--task-id", type=str, help="Custom task identifier")
    parser.add_argument("--work-dir", type=str, help="Custom working directory for artifacts")
    parser.add_argument("--output", type=str, help="Custom destination file for the final markdown report")
    parser.add_argument("--json", type=str, dest="json_file",
                        help="Destination file for the machine-readable triage JSON (for calling agents)")
    # CI から使うときだけ付ける。既定で落とさないのは、本スクリプトが静的パターン検査であり
    # 誤検知しうるため。落としたい CI では明示的に付けさせる。
    parser.add_argument("--fail-on-critical", action="store_true",
                        help="Critical が 1 件でもあれば exit code 1 で終了する (CI 用)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help=f"検査対象から外すパス (繰り返し可)。既定の除外に追加される: {DEFAULT_EXCLUDES}")

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
        output_file=args.output,
        json_file=args.json_file,
        excludes=args.exclude,
    )

    if args.fail_on_critical and result["triage"]["critical_count"] > 0:
        print("[BLOCK] Critical 指摘があるためマージをブロックします。")
        sys.exit(1)


if __name__ == "__main__":
    main()
