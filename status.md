# Project Status

## Completed

- Caption-aware recording flow
  - Decide caption necessity from the full user flow during planning.
  - Include each screen and its exact caption text in preflight for user review.
  - Bind approved captions to the frozen plan hash.
  - Generate timed WebVTT and burned-in Full HD MP4 artifacts for web and
    Android recordings.

## Verification

- `python -m unittest discover -s tests -p 'test_*.py' -v` — 59 passed
- Web and Android integration tests produced `captions.vtt` and
  `video-captioned.mp4`.

## In Progress

- GitHub publish
  - Publish the caption workflow, contributor guide, and native runtime fix.
  - Exclude generated MP4 and manifest artifacts.
