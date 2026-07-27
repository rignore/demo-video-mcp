"""Video Brief V1 validation and normalization."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


BRIEF_FIELDS = {
    "schema_version",
    "purpose",
    "audience",
    "key_messages",
    "duration_seconds",
    "constraints",
}


def validate_video_brief(brief: Any) -> List[str]:
    if not isinstance(brief, dict):
        return ["brief: object is required"]
    errors: List[str] = []
    unexpected = sorted(set(brief) - BRIEF_FIELDS)
    if unexpected:
        errors.append(
            "brief: unsupported properties: " + ", ".join(unexpected)
        )
    if brief.get("schema_version") != 1:
        errors.append("brief.schema_version: expected 1")
    for field, limit in (("purpose", 2000), ("audience", 1000)):
        value = brief.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"brief.{field}: non-empty string is required")
        elif len(value) > limit:
            errors.append(
                f"brief.{field}: must be at most {limit} characters"
            )

    key_messages = brief.get("key_messages")
    if not isinstance(key_messages, list) or not key_messages:
        errors.append("brief.key_messages: non-empty array is required")
    elif len(key_messages) > 10:
        errors.append("brief.key_messages: at most 10 items are allowed")
    else:
        normalized_messages = []
        for index, item in enumerate(key_messages):
            if not isinstance(item, str) or not item.strip():
                errors.append(
                    f"brief.key_messages[{index}]: non-empty string required"
                )
            elif len(item) > 500:
                errors.append(
                    f"brief.key_messages[{index}]: at most 500 characters"
                )
            else:
                normalized_messages.append(item.strip())
        if (
            len(normalized_messages) == len(key_messages)
            and len(set(normalized_messages)) != len(normalized_messages)
        ):
            errors.append("brief.key_messages: duplicate items are not allowed")

    duration = brief.get("duration_seconds", 60)
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 10
        or duration > 900
    ):
        errors.append(
            "brief.duration_seconds: integer between 10 and 900 required"
        )

    constraints = brief.get("constraints", [])
    if not isinstance(constraints, list):
        errors.append("brief.constraints: array is required")
    elif len(constraints) > 20:
        errors.append("brief.constraints: at most 20 items are allowed")
    else:
        for index, item in enumerate(constraints):
            if not isinstance(item, str) or not item.strip():
                errors.append(
                    f"brief.constraints[{index}]: non-empty string required"
                )
            elif len(item) > 500:
                errors.append(
                    f"brief.constraints[{index}]: at most 500 characters"
                )
    return errors


def normalize_video_brief(brief: Mapping[str, Any]) -> Dict[str, Any]:
    errors = validate_video_brief(brief)
    if errors:
        raise ValueError("\n".join(errors))
    return {
        "schema_version": 1,
        "purpose": brief["purpose"].strip(),
        "audience": brief["audience"].strip(),
        "key_messages": [
            item.strip() for item in brief["key_messages"]
        ],
        "duration_seconds": brief.get("duration_seconds", 60),
        "constraints": [
            item.strip() for item in brief.get("constraints", [])
        ],
    }
