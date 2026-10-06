# linux-scripting skill

A Claude skill for writing, fixing and reviewing Linux shell scripts, plus the evals that measure it.

- `linux-scripting/`: the skill. Install by copying it to `~/.claude/skills/linux-scripting/`.
  - `SKILL.md`: what to do before writing, the user's machine (CachyOS, fish, systemd), pitfalls that pass review and fail in use, rerunnable and destructive work, testing, reply format.
  - `template.sh`: a small skeleton for scripts that get installed, scheduled or rerun.
  - `references/`: loaded only when relevant: ffmpeg and media, scheduled jobs, POSIX sh/busybox, bash pitfalls in detail, testing.
- `linux-scripting-evals/`: seven evals graded by running the produced scripts on generated inputs. Kept outside the skill folder so a skill run can't read the checks or reference answers. See its README.

```fish
cp -r linux-scripting ~/.claude/skills/
```
