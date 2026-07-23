"""Tests for the LLM parser — the network call is MOCKED so no real API usage.

We verify: batch parsing maps results correctly, low-confidence replies are
flagged, and the cheap regex short-circuits plain approvals without any call.
"""

from unittest.mock import MagicMock, patch

from app.services import llm_parser


def _fake_response(text):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


def test_plain_approval_skips_llm():
    # "approved" should be caught by regex — no client created/called.
    with patch.object(llm_parser, "_get_client") as gc:
        result = llm_parser.parse_manager_reply("approved")
        assert result == {"action": "approve"}
        gc.assert_not_called()


def test_availability_batch_maps_results():
    fake = _fake_response(
        '{"1": {"days": ["Mon","Wed"], "caveats": "", "confident": true}, '
        '"2": {"days": [], "caveats": "", "confident": false}}'
    )
    with patch.object(llm_parser, "_get_client") as gc:
        gc.return_value.messages.create.return_value = fake
        out = llm_parser.parse_availability_batch(
            [
                {"employee_id": 1, "name": "Alex", "text": "Mon Wed"},
                {"employee_id": 2, "name": "Sam", "text": "???"},
            ]
        )
    assert out[1]["days"] == ["Mon", "Wed"]
    assert out[1]["confident"] is True
    assert out[2]["confident"] is False  # flagged for review


def test_availability_batch_survives_api_error():
    with patch.object(llm_parser, "_get_client") as gc:
        gc.return_value.messages.create.side_effect = RuntimeError("boom")
        out = llm_parser.parse_availability_batch(
            [{"employee_id": 1, "name": "Alex", "text": "Mon"}]
        )
    # Whole batch must not crash — everyone comes back not-confident.
    assert out[1]["confident"] is False


def test_manager_revision_parsed():
    fake = _fake_response(
        '{"action":"revise","changes":[{"day":"Thu","direction":"increase",'
        '"count":1,"employee":null}]}'
    )
    with patch.object(llm_parser, "_get_client") as gc:
        gc.return_value.messages.create.return_value = fake
        out = llm_parser.parse_manager_reply("need one more person Thursday")
    assert out["action"] == "revise"
    assert out["changes"][0]["day"] == "Thu"
