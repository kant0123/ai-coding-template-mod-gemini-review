"""
panel_runner の QA ガード回帰テスト。

本パネルは「CI通過後に、依頼元エージェントへ修正を依頼するためのレポート」を出力する。
そのため偽陽性は依頼元に無駄な修正を強制する実害に直結する（AGENTS.md 原則1）。
ここでは「発火すべきケース」と同等以上に「発火してはならないケース」を固定する。

実行: python -m pytest tests/ -q
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# panel_runner.py と同じく、2 種類の配置で動くようにしておく。
#   上流:       <root>/tests/        → パネルは <root>/scripts/
#   vendoring:  <root>/review/tests/ → パネルは <root>/review/
_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (_ROOT / "scripts", _ROOT):
    if (_candidate / "panel_runner.py").exists():
        sys.path.insert(0, str(_candidate))
        break

from panel_runner import (  # noqa: E402
    ReviewContext,
    analyze_domain,
    analyze_qa,
    analyze_security,
    build_context,
    has_keyword,
    is_test_path,
    parse_diff,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def diff_ctx(diff_text: str) -> ReviewContext:
    """アナライザ単体を見るための、除外フィルタを通さないコンテキスト。"""
    return ReviewContext("diff", parse_diff(diff_text), diff_text, "test-diff")


def build_context_from_text(diff_text: str, excludes=None) -> ReviewContext:
    """除外フィルタを通した本番同等のコンテキスト（build_context 経由）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as f:
        f.write(diff_text)
        path = f.name
    try:
        return build_context(diff_file=path, excludes=excludes)
    finally:
        os.unlink(path)


def categories(findings) -> set:
    return {f["category"] for f in findings}


def qa(diff_text: str) -> set:
    return categories(analyze_qa(diff_ctx(diff_text), {}))


def make_diff(path: str, added=(), removed=(), start: int = 1) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},{len(removed)} +{start},{len(added)} @@\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# diff parser
# ---------------------------------------------------------------------------
def test_parse_diff_separates_added_removed_and_context():
    files = parse_diff(
        "diff --git a/app/svc.py b/app/svc.py\n"
        "--- a/app/svc.py\n"
        "+++ b/app/svc.py\n"
        "@@ -10,3 +10,3 @@\n"
        " def keep():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    assert len(files) == 1
    assert files[0].path == "app/svc.py"
    assert files[0].added == [(11, "    return 2")]
    assert files[0].removed == [(11, "    return 1")]
    assert files[0].context == [(10, "def keep():")]


def test_parse_diff_tracks_line_numbers_across_hunks():
    files = parse_diff(
        "+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+first\n"
        "@@ -50,1 +80,1 @@\n"
        "+second\n"
    )
    assert files[0].added == [(1, "first"), (80, "second")]


@pytest.mark.parametrize("path,expected", [
    ("tests/test_x.py", True),
    ("app/test_service.py", True),
    ("app/service_test.py", True),
    ("web/Button.spec.tsx", True),
    ("conftest.py", True),
    ("app/service.py", False),
    ("app/contest_results.py", False),   # 'test' を含むが非テスト
    ("app/latest_price.py", False),
])
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


# ---------------------------------------------------------------------------
# 偽陽性ガード (発火してはならない)
# ---------------------------------------------------------------------------
def test_production_code_mentioning_pytest_is_not_flagged_as_empty_test():
    """conftest 相当の fixture 定義など、pytest に言及するだけの本番コードで
    'No-Assertion Test' を出してはならない（旧実装の CRITICAL 偽陽性）。"""
    found = qa(make_diff("app/fixtures.py", added=[
        "import pytest",
        "",
        "@pytest.fixture",
        "def client():",
        "    return build_client()",
    ]))
    assert "Meaningless / No-Assertion Test" not in found


def test_production_function_named_test_something_is_not_a_test_function():
    """本番コードの `def test_connection()` をテスト関数と誤認してはならない。"""
    found = qa(make_diff("app/health.py", added=[
        "def test_connection(dsn):",
        "    return psycopg.connect(dsn).closed == 0",
    ]))
    assert "Meaningless / No-Assertion Test" not in found


def test_pure_deletion_is_not_reported_as_new_logic():
    """関数を削除しただけの差分を『新規ロジックの追加』と誤認してはならない。"""
    found = qa(make_diff("app/svc.py", removed=[
        "def legacy_helper(x):",
        "    return x + 1",
    ]))
    assert "Diff Coverage & Test Omission" not in found


def test_single_file_audit_does_not_demand_colocated_tests(tmp_path):
    """単一ファイル監査ではテストの所在を判断できないため評価しない。"""
    src = tmp_path / "svc.py"
    src.write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    ctx = build_context(target=str(src))
    assert ctx.mode == "file"
    assert "Diff Coverage & Test Omission" not in categories(analyze_qa(ctx, {}))


def test_documentation_mentioning_skip_marker_is_not_flagged():
    """レビュー観点ドキュメントが '@pytest.mark.skip' に言及しただけでは発火しない。"""
    found = qa(make_diff("docs/qa_guard.md", added=[
        "- 既存テストを `@pytest.mark.skip` で握りつぶしていないか？",
    ]))
    # 検知対象はコード行であり、Markdown の散文ではない。
    assert "Test Suppression & Quality Regression" not in found


def test_partially_modified_test_is_not_judged():
    """本体の一部だけを書き換えた既存テストは全体像が不明なため判定しない。"""
    diff = (
        "+++ b/tests/test_svc.py\n"
        "@@ -1,3 +1,3 @@\n"
        "+def test_partial():\n"
        "@@ -20,1 +20,1 @@\n"
        "+    value = compute()\n"
    )
    assert "Meaningless / No-Assertion Test" not in qa(diff)


def test_well_formed_test_addition_is_silent():
    found = qa(make_diff("tests/test_svc.py", added=[
        "def test_compute_doubles_positive():",
        "    assert compute(2) == 4",
        "",
        "def test_compute_rejects_negative():",
        "    with pytest.raises(ValueError):",
        "        compute(-1)",
    ]))
    assert found == set()


# ---------------------------------------------------------------------------
# 真陽性 (発火しなければならない)
# ---------------------------------------------------------------------------
def test_added_test_without_assertion_is_critical():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_svc.py", added=[
        "def test_it_runs():",
        "    compute(1)",
    ])), {})
    hit = [f for f in findings if f["category"] == "Meaningless / No-Assertion Test"]
    assert hit and hit[0]["severity"] == "CRITICAL"
    assert hit[0]["locations"] == ["tests/test_svc.py:1"]
    assert "test_it_runs" in hit[0]["issue"]


def test_mock_only_test_is_warned_as_tautological():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_billing.py", added=[
        "@patch('billing.gateway.charge')",
        "def test_checkout(mock_charge):",
        "    checkout(cart)",
        "    mock_charge.assert_called_once()",
    ])), {})
    hit = [f for f in findings if f["category"] == "Over-Mocking (Tautological Test)"]
    assert hit and hit[0]["severity"] == "WARNING"
    # 呼び出し検証は存在するので「アサーション皆無」ではない。
    assert "Meaningless / No-Assertion Test" not in categories(findings)


def test_new_logic_without_any_test_change_is_warned():
    findings = analyze_qa(diff_ctx(make_diff("app/pricing.py", added=[
        "def apply_discount(amount, rate):",
        "    if rate < 0:",
        "        raise ValueError(rate)",
        "    return amount * (1 - rate)",
    ])), {})
    hit = [f for f in findings if f["category"] == "Diff Coverage & Test Omission"]
    assert hit and hit[0]["locations"] == ["app/pricing.py:1"]


def test_new_logic_with_accompanying_test_is_silent():
    diff = make_diff("app/pricing.py", added=[
        "def apply_discount(amount, rate):",
        "    return amount * (1 - rate)",
    ]) + make_diff("tests/test_pricing.py", added=[
        "def test_apply_discount():",
        "    assert apply_discount(100, 0.1) == 90",
    ])
    assert "Diff Coverage & Test Omission" not in qa(diff)


def test_added_skip_marker_is_critical():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_svc.py", added=[
        "@pytest.mark.skip(reason='flaky')",
        "def test_legacy_contract():",
        "    assert compute(1) == 2",
    ])), {})
    hit = [f for f in findings if f["category"] == "Test Suppression & Quality Regression"]
    assert hit and hit[0]["severity"] == "CRITICAL"
    assert hit[0]["locations"] == ["tests/test_svc.py:1"]


def test_deleted_test_is_critical():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_svc.py", removed=[
        "def test_legacy_contract():",
        "    assert compute(1) == 2",
    ])), {})
    hit = [f for f in findings if f["category"] == "Test Deletion (Silent Coverage Loss)"]
    assert hit and "test_legacy_contract" in hit[0]["issue"]


def test_renamed_test_is_still_reported_but_moved_test_is_not():
    """同名テストが別ファイルへ移設された場合は消滅ではないため発火しない。"""
    diff = make_diff("tests/test_old.py", removed=[
        "def test_contract():",
        "    assert compute(1) == 2",
    ]) + make_diff("tests/test_new.py", added=[
        "def test_contract():",
        "    assert compute(1) == 2",
    ])
    assert "Test Deletion (Silent Coverage Loss)" not in qa(diff)


def test_assertion_weakening_is_warned():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_svc.py", added=[
        "    assert result is not None",
    ], removed=[
        "    assert result.total == Decimal('90.00')",
        "    assert result.currency == 'JPY'",
    ])), {})
    hit = [f for f in findings if f["category"] == "Assertion Weakening"]
    assert hit and "1 件減少" in hit[0]["issue"]


def test_breaking_migration_still_detected():
    findings = analyze_qa(diff_ctx(make_diff("migrations/0002_add_col.py", added=[
        "def upgrade():",
        "    op.add_column('orders', sa.Column('memo', sa.String(), nullable=False))",
    ])), {})
    assert "Breaking Migration (Zero-Downtime Violation)" in categories(findings)


# ---------------------------------------------------------------------------
# 全観点共通の偽陽性ガード
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,keywords,expected", [
    ("if order.cancelled:", ["can"], False),      # cancel を 'can' と誤認しない
    ("rows = scan_table()", ["can"], False),
    ("monkeypatch.setattr(...)", ["key"], False),
    ("can_send = True", ["can"], False),          # 識別子の一部
    ("if CAN.receive():", ["can"], True),         # CANバス
    ("isr_handler()", ["isr"], False),
    ("void ISR(void) {", ["isr"], True),
])
def test_has_keyword_uses_word_boundaries(text, keywords, expected):
    assert has_keyword(text, keywords) is expected


def test_commented_out_examples_do_not_trigger_security_rules():
    """コメント内の記述例（修正例・TODO）でセキュリティ指摘を出してはならない。"""
    ctx = diff_ctx(make_diff("app/config.py", added=[
        "# 修正例: SECRET_KEY = os.environ.get('APP_SECRET_KEY')",
        "# TODO: password = load_from_vault()",
        "SECRET_KEY = os.environ['APP_SECRET_KEY']",
    ]))
    assert categories(analyze_security(ctx, {})) == set()


def test_literal_secret_is_still_detected():
    ctx = diff_ctx(make_diff("app/config.py", added=[
        "SECRET_KEY = 'sk_live_9f3a2b7c41'",
    ]))
    assert "Hardcoded Credential" in categories(analyze_security(ctx, {}))


def test_commented_out_test_is_still_detected_despite_comment_filtering():
    """コメント除外は『コメントアウトされたテスト』検知を無効化してはならない。"""
    found = qa(make_diff("tests/test_svc.py", added=[
        "# def test_legacy_contract():",
        "#     assert compute(1) == 2",
    ]))
    assert "Test Suppression & Quality Regression" in found


def test_prose_about_cancel_does_not_trigger_embedded_domain():
    ctx = diff_ctx(make_diff("app/orders.py", added=[
        "def cancel_order(order):",
        "    with order_lock:",
        "        order.cancelled = True",
    ]))
    assert "Domain Invariant Violation (Blocking in ISR)" not in categories(analyze_domain(ctx, "general", {}))


# ---------------------------------------------------------------------------
# レポート契約 (依頼元エージェントが機械的に消費するため固定する)
# ---------------------------------------------------------------------------
def test_every_finding_carries_actionable_metadata():
    findings = analyze_qa(diff_ctx(make_diff("tests/test_svc.py", added=[
        "def test_it_runs():",
        "    compute(1)",
    ])), {})
    for f in findings:
        assert f["severity"] in {"CRITICAL", "WARNING", "NITPICK"}
        assert f["issue"] and f["impact"] and f["recommendation"]


def test_panel_own_source_is_excluded_by_default():
    """パネル自身のソースは既定で検査対象外。

    検出したいパターン（ハードコードされた鍵、@pytest.mark.skip 等）を
    文字列リテラルとして持つため、除外しないと自身を触る PR が必ず落ちる。
    """
    diff = make_diff("review/panel_runner.py", added=[
        "SECRET_KEY = 'sk_live_9f3a2b7c41'",
    ]) + make_diff("scripts/panel_runner.py", added=[
        "@pytest.mark.skip(reason='x')",
    ])
    ctx = build_context_from_text(diff)
    assert ctx.files == []
    assert sorted(ctx.dropped) == ["review/panel_runner.py", "scripts/panel_runner.py"]
    assert categories(analyze_security(ctx, {})) == set()
    assert "Test Suppression & Quality Regression" not in categories(analyze_qa(ctx, {}))


def test_prose_files_are_not_scanned():
    """README や SKILL.md は検出パターンを引用して説明するため検査しない。"""
    ctx = build_context_from_text(make_diff("docs/guide.md", added=[
        "SECRET_KEY = 'sk_live_9f3a2b7c41' と書いてはいけない",
    ]))
    assert ctx.files == []
    assert categories(analyze_security(ctx, {})) == set()


def test_filtered_empty_is_distinguished_from_clean(tmp_path):
    """全件除外された結果の APPROVE を「問題なし」と読ませない。"""
    diff = tmp_path / "d.patch"
    diff.write_text(make_diff("review/panel_runner.py", added=["x = 1"]), encoding="utf-8")

    from panel_runner import run_panel
    result = run_panel(diff_file=str(diff), work_dir=str(tmp_path / "w"), task_id="filtered")

    assert result["triage"]["verdict"] == "APPROVE"
    report = (tmp_path / "w" / "final_consensus_review.md").read_text(encoding="utf-8")
    assert "検査していない" in report


def test_run_panel_returns_triage_for_ci(tmp_path):
    """--fail-on-critical を実装する CI 側が参照する戻り値の契約。"""
    diff = tmp_path / "d.patch"
    diff.write_text(make_diff("tests/test_svc.py", added=[
        "def test_it_runs():",
        "    compute(1)",
    ]), encoding="utf-8")

    from panel_runner import run_panel
    result = run_panel(diff_file=str(diff), work_dir=str(tmp_path / "w"), task_id="ci")

    assert result["triage"]["critical_count"] == 1
    assert result["report_path"].endswith("final_consensus_review.md")


def test_panel_root_resolves_configs_in_both_layouts():
    """上流 (scripts/) と vendoring 先 (review/) で同じファイルが動くこと。"""
    from panel_runner import PANEL_ROOT, load_domain_invariants
    assert (PANEL_ROOT / "configs" / "domain_invariants.json").exists()
    assert load_domain_invariants("fintech").get("name")


def test_panel_is_report_only_and_does_not_touch_the_target(tmp_path):
    """本パネルは対象リポジトリを一切変更しない（修正は依頼元エージェントの責務）。"""
    from panel_runner import run_panel

    target_dir = tmp_path / "repo"
    target_dir.mkdir()
    src = target_dir / "svc.py"
    src.write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in target_dir.rglob("*") if p.is_file()}

    run_panel(target=str(target_dir), work_dir=str(tmp_path / "work"), task_id="report_only")

    after = {p: p.read_bytes() for p in target_dir.rglob("*") if p.is_file()}
    assert before == after
