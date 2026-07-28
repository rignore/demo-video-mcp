"""Caption planning, validation, and WebVTT generation."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence


CAPTION_DECISIONS = {"required", "not_required"}
CAPTION_OUTPUTS = {"sidecar", "burned_in", "both"}


def validate_caption_contract(
    scenario: Mapping[str, Any],
    steps: Sequence[Any],
) -> List[str]:
    errors: List[str] = []
    config = scenario.get("captions")
    captioned_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if isinstance(step, dict) and "caption" in step
    ]
    if config is None:
        if captioned_steps:
            errors.append(
                "captions: configuration is required when steps have captions"
            )
        return errors
    if not isinstance(config, dict):
        return ["captions: object is required"]
    extra = sorted(
        set(config) - {"decision", "reason", "language", "output"}
    )
    if extra:
        errors.append(
            "captions: unsupported properties: " + ", ".join(extra)
        )
    decision = config.get("decision")
    if decision not in CAPTION_DECISIONS:
        errors.append(
            "captions.decision: expected required or not_required"
        )
    reason = config.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("captions.reason: non-empty string is required")
    elif len(reason) > 500:
        errors.append("captions.reason: at most 500 characters allowed")
    language = config.get("language", "ko-KR")
    if not isinstance(language, str) or not language.strip():
        errors.append("captions.language: non-empty string is required")
    elif len(language) > 35:
        errors.append("captions.language: at most 35 characters allowed")
    output = config.get("output", "both")
    if output not in CAPTION_OUTPUTS:
        errors.append(
            "captions.output: expected sidecar, burned_in, or both"
        )

    for index, step in captioned_steps:
        location = f"steps[{index}].caption"
        caption = step["caption"]
        if not isinstance(caption, dict):
            errors.append(f"{location}: object is required")
            continue
        extra = sorted(set(caption) - {"screen", "text"})
        if extra:
            errors.append(
                f"{location}: unsupported properties: {', '.join(extra)}"
            )
        for field, limit in (("screen", 120), ("text", 160)):
            value = caption.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"{location}.{field}: non-empty string is required"
                )
            elif len(value) > limit:
                errors.append(
                    f"{location}.{field}: at most {limit} characters allowed"
                )
        hold_ms = step.get("hold_ms", 800)
        if not isinstance(hold_ms, int) or hold_ms < 1200:
            errors.append(
                f"steps[{index}].hold_ms: captioned scenes require at "
                "least 1200ms"
            )

    if decision == "required" and not captioned_steps:
        errors.append(
            "steps: at least one captioned scene is required when captions "
            "are required"
        )
    if decision == "not_required" and captioned_steps:
        errors.append(
            "steps: captions are not allowed when captions are not required"
        )
    return errors


def caption_review_required(scenario: Mapping[str, Any]) -> bool:
    config = scenario.get("captions")
    return (
        isinstance(config, dict)
        and config.get("decision") == "required"
    )


def caption_storyboard(scenario: Mapping[str, Any]) -> List[Dict[str, Any]]:
    storyboard = []
    for step in scenario.get("steps", []):
        if not isinstance(step, dict):
            continue
        caption = step.get("caption")
        if not isinstance(caption, dict):
            continue
        storyboard.append(
            {
                "step_id": step.get("id"),
                "scene": step.get("title"),
                "screen": caption.get("screen"),
                "caption": caption.get("text"),
                "hold_ms": step.get("hold_ms", 800),
            }
        )
    return storyboard


def caption_summary(scenario: Mapping[str, Any]) -> Dict[str, Any]:
    config = scenario.get("captions")
    if not isinstance(config, dict):
        return {
            "decision": "not_requested",
            "reason": None,
            "language": None,
            "output": None,
            "scene_count": 0,
        }
    return {
        "decision": config.get("decision"),
        "reason": config.get("reason"),
        "language": config.get("language", "ko-KR"),
        "output": config.get("output", "both"),
        "scene_count": len(caption_storyboard(scenario)),
    }


def caption_cues(
    scenario: Mapping[str, Any],
    step_results: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not caption_review_required(scenario):
        return []
    results = {
        item.get("step_id"): item
        for item in step_results
        if item.get("state") == "passed"
    }
    cues = []
    for item in caption_storyboard(scenario):
        result = results.get(item["step_id"])
        if not result:
            continue
        start_ms = result.get("video_start_ms")
        end_ms = result.get("video_end_ms")
        if (
            not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            continue
        cues.append(
            {
                "step_id": item["step_id"],
                "screen": item["screen"],
                "text": item["caption"],
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    return cues


def _vtt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def render_webvtt(cues: Sequence[Mapping[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        text = str(cue["text"]).replace("-->", "→").strip()
        lines.extend(
            [
                str(index),
                (
                    f"{_vtt_timestamp(int(cue['start_ms']))} --> "
                    f"{_vtt_timestamp(int(cue['end_ms']))}"
                ),
                text,
                "",
            ]
        )
    return "\n".join(lines)
