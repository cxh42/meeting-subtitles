# Meeting Subtitles

## Overview

A Linux desktop utility for starting an English-to-Chinese meeting transcript,
reading unobtrusive live captions, and reviewing saved text. The launcher should
feel like a compact desktop settings window. The transcript reader prioritizes
long reading sessions; the floating captions prioritize contrast over a call.

The user rejected the previous navy-and-mint dashboard treatment as generic AI
design. Keep the interface restrained, with ordinary Chinese labels, clear
alignment, and a single prominent action. Visual complexity must serve a task.

## Colors

The immutable light and dark palettes in `meeting_subtitles/gnome.py` are
authoritative. The launcher, preview, floating captions, and transcript reader
all use the selected palette; each window holds its own palette reference.

| Role | Light | Dark | Use |
| --- | --- | --- | --- |
| Window | `#f5f5f4` | `#232628` | All window and caption backgrounds |
| Field | `#ffffff` | `#2d3235` | Editable input |
| Control thumb | `#ffffff` | `#f4f4f1` | Switch and slider thumbs |
| Primary text | `#252a2c` | `#f4f4f1` | Labels and finished translations |
| Secondary text | `#5d6468` | `#b5bdc2` | Explanations, source text, timestamps |
| Muted text | `#697176` | `#a7b1b7` | Hints and inactive labels |
| Divider | `#dcdedc` | `#424a4f` | Thin separators |
| Accent | `#345f84` | `#91b8d6` | Start action, enabled switches, focus and selection |
| Accent hover / pressed | `#284e6e` / `#20435f` | `#abcce4` / `#c1ddf0` | Interactive feedback |
| Accent text | `#ffffff` | `#1c2b36` | Labels on accent surfaces |
| Caption draft | `#414b51` | `#c9cfd2` | Streaming Chinese translation |

Green, amber, and red indicate engine or recording state and accompany text.
The launcher accent is not a decorative border or a caption highlight.

## Typography

Use an installed CJK-capable sans family, preferring Noto Sans CJK SC. Preserve
the existing font fallback and system-Tk compatibility handling. Font sizes are
Tk points; control dimensions scale separately with display DPI.

- Window titles: 13 pt bold. Launcher labels: 11 pt regular.
- Explanations and metadata: 9–10 pt regular.
- Transcript reader: Chinese 14 pt, English 12 pt, timestamps 9 pt; regular weight.
- Live captions: the user's chosen size, regular weight. English is 3 pt smaller,
  with an 11 pt floor. The preview uses the same relationship.

Reserve bold for titles, group headings, and the primary action. Chinese caption
hierarchy comes from placement, spacing, and contrast rather than heavy weight.

## Layout

The launcher has one column, 24 logical-pixel side margins, and a 48-pixel title
bar. Meeting options precede subtitle display options, then engine status and
actions. Switch rows are 62 pixels high; slider rows are 42 pixels high.

The subtitle preview opens at the screen bottom and remains visible while the
user adjusts size and opacity. Label its sample content explicitly. The real
overlay separates the pinned finished translation from the live source and
draft. Measure wrapping with the actual font and keep captions within the screen.

The history window is a continuous reading surface. Separate records with
vertical space and small timestamps. Scrolling back must preserve the reader's
position and selection until they choose to return to the latest text.

## Elevation & Depth

Use flat surfaces and thin separators. There are no ornamental shadows,
gradients, translucent settings panels, or nested cards. Only the floating
subtitle window and its preview use user-controlled whole-window opacity.

## Shapes

Buttons and fields have modest 6-pixel corners; switches are capsules. Scale
geometry with DPI. Preserve the existing X11 window-shape handling. The app icon
uses editable geometric SVG paths with graphite, white, and muted blue.

## Components

The start button is the launcher’s only filled action. Secondary actions are
quiet text buttons with visible hover and keyboard focus. Controls support Tab,
Space or Enter; sliders also support arrow keys. Disabled actions do not activate.

The title-bar action names its destination: “切换深色” or “切换浅色”. It immediately
updates the launcher and any open preview, saves the choice, and restores it on
the next launch. Missing theme preferences default to light without resetting
existing settings. Meeting windows inherit this choice; `--theme light|dark`
overrides it for a command-line session, while omitting the option uses the saved
preference.

Keep engine progress and actionable feedback beside the meeting controls.
Automated preview and screenshot modes must not start an engine, capture audio,
or change saved settings. Use the real Tk main loop when verifying threaded callbacks.

## Do's and Don'ts

- Keep user-facing text in Chinese and controls named after their actions.
- Preserve the audio, engine, translation, recording, and complete-snapshot contracts.
- Avoid promotional headings, repeated feature cards, decorative rails, and badge clutter.
- Generate documentation screenshots with `tools/screenshots.py` under Xvfb;
  its default captures both themes, with dark images in the output's `dark/` subdirectory.
- Check ordinary desktop and high-DPI rendering, including large caption sizes.
