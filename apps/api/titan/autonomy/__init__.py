"""The campaign manager: what it decides, and the narrow surface it decides through.

Split three ways on purpose. ``health`` judges, ``manager`` proposes, and
``actuator`` is the only thing that can change anything -- so a mistake in the
first two is a clamped number and a row in the audit trail, not an unbounded
action. The boundary is a property of what exists here, not of what the
docstrings promise.
"""
