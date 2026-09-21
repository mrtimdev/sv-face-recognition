A **skill is a reusable set of instructions for Codex**. For your project, it can explain how to improve performance, change features, and test attendance reliably. Codex follows those instructions when working on your code.

Start with one skill named `face-attendance-maintainer`.

1. **Create the skill file**

Inside your project, create:

```text
.agents/skills/face-attendance-maintainer/SKILL.md
```

Codex discovers project skills in `.agents/skills`. Each skill requires a `SKILL.md` containing a name, description, and instructions. [Official documentation](https://learn.chatgpt.com/docs/build-skills)

2. **Paste these project-specific instructions**

```markdown
---
name: face-attendance-maintainer
description: Improve performance, fix bugs, and implement requested features in this Python face-recognition attendance project.
---

Work incrementally on the existing application.

Before changing code:
- Read README.md and the modules relevant to the request.
- Inspect existing changes and preserve unrelated work.
- For a review or diagnosis, report findings without implementing
  changes unless requested.

Project responsibilities:
- camera.py and channels.py: capture, reconnects, latest-frame buffers.
- recognition.py and tracking.py: matching and face tracking.
- attendance.py: verification, capture eligibility, cooldown.
- repository.py and persistence.py: transactions and background work.
- ui.py: overlays, HUD, and animations.
- config.py: tuning values.

Preserve these behaviors unless the requested feature changes them:
- Recognition correctness takes priority over FPS.
- Keep recognition, database operations, and snapshot writes
  outside the UI loop.
- Keep queues bounded and discard outdated recognition frames.
- Preserve independent employee states and duplicate prevention.
- Save clean frames without overlays.
- Show attendance success only after the database commits.
- Keep existing enrollment and command-line compatibility.

For performance requests:
- Identify the bottleneck before changing settings.
- Compare before/after measurements under equivalent conditions.
- Report frame time, recognition latency, or CPU usage as relevant.
- Distinguish synthetic measurements from live-camera results.
- Explain accuracy tradeoffs when changing recognition settings.

Verification:
- Use temporary databases and test fixtures.
- Run relevant tests; for pipeline or attendance changes, run:
  venv/bin/python -B -m unittest discover -s tests -v
- For UI changes, check 640x480, 1280x720, and 1920x1080.
- Summarize changes, test results, and remaining limitations.
```

3. **Use it in the Codex chat**

After saving, type `$` to select the skill, or send:

```text
$face-attendance-maintainer Improve camera FPS and reduce CPU usage.
Measure the bottleneck first and preserve recognition accuracy.
```

For reviewing:

```text
$face-attendance-maintainer Review camera.py for reconnect problems.
Explain the findings without changing code.
```

For a feature:

```text
$face-attendance-maintainer Add a configurable HUD theme and test
the supported resolutions.
```

Codex can also select the skill automatically when your request matches its description. If it does not appear, restart Codex. [Skill invocation guidance](https://learn.chatgpt.com/docs/build-skills)

You can also ask Codex to create the file for you:

```text
$skill-creator Create face-attendance-maintainer in this project's
.agents/skills directory using the project-specific instructions above.
```

To update the skill later, edit `SKILL.md` or ask `$skill-creator` to revise it.