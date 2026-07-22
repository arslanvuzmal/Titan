import pytest
from pydantic import ValidationError
from app.agents.schemas import ActionRequest


def test_action_request_valid():
    """
    Ensures a valid ActionRequest passes schema validation.
    """
    req = ActionRequest(
        tool_name="send_email",
        arguments={"to_email": "elon@x.com", "subject": "Test", "body": "Msg"},
        requires_approval=True,
    )
    assert req.tool_name == "send_email"
    assert req.requires_approval is True


def test_action_request_invalid_type():
    """
    Ensures Pydantic strictly catches type mismatches (e.g., LLM passes string instead of boolean).
    """
    with pytest.raises(ValidationError) as exc:
        ActionRequest(
            tool_name="send_email",
            arguments={"to_email": "elon@x.com"},
            requires_approval="DEFINITELY_NOT_A_BOOLEAN",  # Invalid, should be bool
        )

    # Assert that the error correctly identifies the field and type mismatch
    assert "Input should be a valid boolean" in str(exc.value)


def test_action_request_missing_field():
    """
    Ensures Pydantic catches missing required fields.
    """
    with pytest.raises(ValidationError) as exc:
        ActionRequest(
            arguments={"to_email": "elon@x.com"}, requires_approval=True
        )  # Missing tool_name

    assert "Field required" in str(exc.value)
