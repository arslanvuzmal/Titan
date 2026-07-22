from app.actions.engine import ActionEngine, RiskLevel
from app.agents.schemas import ActionRequest


def test_risk_classifier_high_risk():
    req = ActionRequest(tool_name="send_email", arguments={}, requires_approval=True)
    assert ActionEngine.evaluate_risk(req) == RiskLevel.HIGH

    req2 = ActionRequest(
        tool_name="execute_sql_query", arguments={}, requires_approval=True
    )
    assert ActionEngine.evaluate_risk(req2) == RiskLevel.HIGH


def test_risk_classifier_medium_risk():
    req = ActionRequest(tool_name="update_crm", arguments={}, requires_approval=True)
    assert ActionEngine.evaluate_risk(req) == RiskLevel.MEDIUM


def test_risk_classifier_low_risk():
    req = ActionRequest(tool_name="search_web", arguments={}, requires_approval=False)
    assert ActionEngine.evaluate_risk(req) == RiskLevel.LOW

    # Unrecognized tools default to LOW in this implementation,
    # but they won't execute if not in the registry.
    req2 = ActionRequest(
        tool_name="some_random_tool", arguments={}, requires_approval=False
    )
    assert ActionEngine.evaluate_risk(req2) == RiskLevel.LOW
