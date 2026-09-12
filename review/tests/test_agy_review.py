"""agy_review.py のうち、壊れると「レビューしていないのに通る」部分の回帰テスト。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agy_review as ar  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40


def result_line(**result):
    return json.dumps({"event": "result", "result": result}, ensure_ascii=False)


# --- parse_output: 空・失敗を APPROVE にしない ---------------------------------
@pytest.mark.parametrize("stdout", ["", "\n", '{"event":"step_update"}'])
def test_output_without_result_is_error(stdout):
    with pytest.raises(ar.ReviewError):
        ar.parse_output(stdout)


def test_failed_status_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="ERROR", error="quota"))


def test_missing_findings_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="SUCCESS", response="レビューしました"))


def test_structured_output_is_used_and_severity_normalized():
    out = "\n".join([
        '{"event":"step_update"}',
        result_line(status="SUCCESS", structured_output={"findings": [
            {"severity": "critical", "category": "x", "location": "a.py:1", "issue": "i", "impact": "m"}]}),
    ])
    findings = ar.parse_output(out)
    assert findings[0]["severity"] == "CRITICAL"


def test_fenced_response_is_parsed():
    body = '```json\n{"findings": [{"severity": "NITPICK", "fix_code": "```py\\nx\\n```"}]}\n```'
    assert ar.parse_output(result_line(status="SUCCESS", response=body))[0]["severity"] == "NITPICK"


def test_unknown_severity_is_error():
    with pytest.raises(ar.ReviewError):
        ar.parse_output(result_line(status="SUCCESS", structured_output={"findings": [{"severity": "INFO"}]}))


# --- verdict / render ----------------------------------------------------------
def test_verdict():
    assert ar.verdict_of([]) == "APPROVE"
    assert ar.verdict_of([{"severity": "NITPICK"}]) == "APPROVE"
    assert ar.verdict_of([{"severity": "WARNING"}]) == "CHANGES_REQUESTED"


def test_render_marker_is_recognized_by_check():
    report = ar.render([{"severity": "WARNING", "fix_code": "```\nx\n```"}], SHA, "m", "general")
    assert ar.REVIEW_MARKER_RE.search(report).groups() == (SHA, "CHANGES_REQUESTED")
    # fix_code 内のフェンスで Markdown が壊れない
    assert "````" in report


# --- check_merge: マージ可否 ---------------------------------------------------
def review(sha, verdict):
    return f"<!-- agy-review sha={sha} verdict={verdict} -->\n"


def triage(sha):
    return f"<!-- agy-review-triage sha={sha} -->\n"


def test_no_review_blocks():
    assert not ar.check_merge("", SHA)[0]


def test_review_of_old_head_blocks():
    assert not ar.check_merge(review(OTHER, "APPROVE"), SHA)[0]


def test_approve_passes():
    assert ar.check_merge(review(SHA, "APPROVE"), SHA)[0]


def test_changes_requested_without_triage_blocks():
    comments = review(SHA, "CHANGES_REQUESTED") + triage(OTHER)
    assert not ar.check_merge(comments, SHA)[0]


def test_changes_requested_with_triage_passes():
    assert ar.check_merge(review(SHA, "CHANGES_REQUESTED") + triage(SHA), SHA)[0]
