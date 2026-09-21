from __future__ import annotations

import json

import pytest

from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_explore_payload,
    validate_reason_payload,
    validate_writeup_payload,
)
from cairn.dispatcher.workers.adapters.pi import PiDriver


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_reason_payload_limits_number_of_intents() -> None:
    kind, intents = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_intents_empty=True,
        max_intents=1,
    )

    assert kind == "intents"
    assert intents == [{"from": ["f001"], "description": "one"}]


def test_reason_payload_requires_intent_when_none_are_open() -> None:
    with pytest.raises(ValueError, match="intents is required"):
        validate_reason_payload(
            {"accepted": True, "data": {}},
            open_intents_empty=True,
            max_intents=3,
        )


def test_explore_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_explore_payload(parse_json_output("Need inspect files and keep working."))


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_writeup_payload_accepts_markdown_content() -> None:
    kind, content = validate_writeup_payload(
        {"accepted": True, "data": {"writeup": "# Writeup\n\nstep 1"}}
    )

    assert kind == "writeup"
    assert content == "# Writeup\n\nstep 1"


def test_writeup_payload_rejected() -> None:
    kind, content = validate_writeup_payload({"accepted": False, "reason": "policy_refusal"})

    assert kind == "rejected"
    assert content is None


def test_writeup_payload_requires_non_empty_writeup() -> None:
    with pytest.raises(ValueError, match="writeup is required"):
        validate_writeup_payload({"accepted": True, "data": {"writeup": "  "}})

    with pytest.raises(ValueError):
        validate_writeup_payload({"accepted": True, "data": {"description": "wrong key"}})


def test_writeup_payload_accepts_legacy_unwrapped_shape() -> None:
    kind, content = validate_writeup_payload({"writeup": "legacy content"})

    assert kind == "writeup"
    assert content == "legacy content"
