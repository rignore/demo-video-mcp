from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from demo_video_mcp.captions import (
    caption_cues,
    caption_storyboard,
    render_webvtt,
    validate_caption_contract,
)
from demo_video_mcp.media import (
    find_ffmpeg,
    generate_caption_artifacts,
    probe_video_size,
)


def captioned_scenario(output: str = "both") -> dict:
    return {
        "captions": {
            "decision": "required",
            "reason": "신규 사용자가 화면 전환의 목적을 이해해야 합니다.",
            "language": "ko-KR",
            "output": output,
        },
        "steps": [
            {
                "id": "dashboard",
                "title": "대시보드 확인",
                "hold_ms": 1500,
                "caption": {
                    "screen": "홈 > 대시보드",
                    "text": "현재 상황을 대시보드에서 확인합니다.",
                },
            }
        ],
    }


class CaptionTests(unittest.TestCase):
    def test_required_decision_needs_a_captioned_scene(self):
        scenario = captioned_scenario()
        scenario["steps"][0].pop("caption")
        errors = validate_caption_contract(
            scenario,
            scenario["steps"],
        )
        self.assertTrue(
            any("at least one captioned scene" in error for error in errors)
        )

    def test_not_required_decision_rejects_caption_text(self):
        scenario = captioned_scenario()
        scenario["captions"]["decision"] = "not_required"
        errors = validate_caption_contract(
            scenario,
            scenario["steps"],
        )
        self.assertTrue(
            any("captions are not allowed" in error for error in errors)
        )

    def test_storyboard_and_webvtt_use_recording_timeline(self):
        scenario = captioned_scenario()
        results = [
            {
                "step_id": "dashboard",
                "state": "passed",
                "video_start_ms": 1250,
                "video_end_ms": 2750,
            }
        ]

        storyboard = caption_storyboard(scenario)
        cues = caption_cues(scenario, results)
        webvtt = render_webvtt(cues)

        self.assertEqual(storyboard[0]["screen"], "홈 > 대시보드")
        self.assertEqual(cues[0]["start_ms"], 1250)
        self.assertIn("00:00:01.250 --> 00:00:02.750", webvtt)
        self.assertIn("현재 상황을 대시보드에서 확인합니다.", webvtt)

    def test_generates_sidecar_and_burned_in_video(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            artifact_dir = Path(temporary)
            source = artifact_dir / "video.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=1920x1080:d=2",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    source,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            scenario = captioned_scenario()
            scenario["steps"].append(
                {
                    "id": "detail",
                    "title": "상세 확인",
                    "hold_ms": 1200,
                    "caption": {
                        "screen": "상황 상세",
                        "text": "선택한 상황의 상세 내용을 확인합니다.",
                    },
                }
            )
            outputs = generate_caption_artifacts(
                scenario,
                [
                    {
                        "step_id": "dashboard",
                        "state": "passed",
                        "video_start_ms": 100,
                        "video_end_ms": 700,
                    },
                    {
                        "step_id": "detail",
                        "state": "passed",
                        "video_start_ms": 900,
                        "video_end_ms": 1600,
                    },
                ],
                source,
                artifact_dir,
            )

            names = {item["name"] for item in outputs["artifacts"]}
            self.assertEqual(
                names,
                {"captions.vtt", "video-captioned.mp4"},
            )
            self.assertEqual(
                probe_video_size(
                    artifact_dir / "video-captioned.mp4"
                ),
                {"width": 1920, "height": 1080},
            )
            self.assertGreater(
                (artifact_dir / "video-captioned.mp4").stat().st_size,
                source.stat().st_size,
            )
            self.assertEqual(
                outputs["manifest"]["burned_in_video"],
                "video-captioned.mp4",
            )


if __name__ == "__main__":
    unittest.main()
