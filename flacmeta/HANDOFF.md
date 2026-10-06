# flacmeta: handoff (2026-10-06, end of the build session)

Give this file to the next session. It replaces the earlier calibration-only handoff.

## Where things are

- Tool: `flacmeta/flacmeta.py` in GitHub repo `coryonlinux/new`, on `main` at `9bbb8e8`. History:
  `945ddc7` (first build), then `9bbb8e8` (fixes from a multi-agent review).
- On the user's machine: clone at `~/Documents/new`; the command `~/.local/bin/flacmeta` is a symlink
  to `~/Documents/new/flacmeta/flacmeta.py`, so `git pull` there updates it. `~/.local/bin` was added with
  `fish_add_path`. (An older copy may also exist at `~/Documents/flacmeta`; it is not used.)
- Tests: `cd ~/Documents/new/flacmeta; python -m unittest discover -s tests` (46 tests, all passing in the
  build container: Python 3.13, ffmpeg 6.1, flac 1.4.3, rsgain 3.4, mutagen 1.48, numpy 2.5).
- Cloud sessions can't push to this repo (git and the GitHub API both return 403). Workaround that works:
  the session sends a `git format-patch` file, the user runs `git am <patch>` in `~/Documents/new`, then
  `git push origin HEAD:main`. Fixing it for good: reconnect GitHub at claude.ai and install the Claude
  GitHub App on the repo.

## Open items (next steps)

1. **First real write is pending.** The user ran `flacmeta fix --apply ~/Music/Qobuz/Albums/Babytron` but
   the output was not shared yet. Ask for it. Good result: `Changed N files.` plus an `Undo log:` path.
   Watch for `FAILED ...: changed since flacmeta read it` on every file: that would mean `cp --reflink` on
   btrfs touches the original's ctime (untested; the build container could not mount btrfs). Then confirm
   with `flacmeta check --quick <album>` (no fixable left) and `flac -t -s <album>/*/*.flac` (silent).
2. **Spectral thresholds have not run on the real library.** Ask for the `flacmeta check` summary and any
   `lossy-source` / `upsampled` flags; compare `check --json` `cliff_db` values of trusted files.
   Suspected fake: `Babytron/Luka Troncic 2 (2025)/16 - End-Zone.flac` (24/88.2, nothing above ~23.5 kHz).
3. `replaygain --apply` has not been run on the real library.
4. `--album-year` produces `Album (YYYY)` from DATE. Not verified against the user's Qobuz-DL version
   (the Qobuz-DL sources in the repo lack `tag_album_from_folder_format`); compare one album with
   `~/Qobuz Downloads` before running it library-wide.

## Machine and library (unchanged from the first handoff)

- CachyOS, fish shell (commands for the user must be fish), Python 3.14. Installed: flac, metaflac,
  custom ffmpeg build, python-mutagen 1.48. Install for flacmeta: `python-numpy`, `rsgain` (chaotic-aur).
- Library `~/Music/Qobuz/Albums/<Artist>/<Album> (<Year>)/NN - Title.flac`; multi-disc in `Disc 01/` with
  `101 - ...`. 7030 files; 16/44.1 mostly, also 24/44.1, 24/48, 24/88.2, 24/96, 24/192. btrfs.
- Files have MD5, SEEKTABLE, a front cover with PICTURE width/height 0 (so `fix` plans a header fill on
  every file), PADDING, ReplayGain tags, Qobuz tags (TRACKTOTAL = whole album's count, also on
  multi-disc albums; DATE=YYYY-MM-DD; LYRICS are synced LRC).
- Reference library `~/Qobuz Downloads` has `ALBUM=Curtis (2007)` style tags.

## User's rules

- Preview first; never change files without `--apply`; keep an undo path.
- Never run sudo in the background; give the user the install command.
- Don't print the Qobuz-DL config's credentials (`~/.config/qobuz-dl/config.ini`).
- Commands for the user in fish. The repo also holds the user's `linux-scripting` skill
  (`linux-scripting/SKILL.md`) with their script conventions.

## What flacmeta does

`check` (read-only), `fix [--album-year] [--apply]`, `replaygain [--force] [--apply]`, `info`,
`spectrogram`, `undo LOG [--apply]`. Full table of check codes and the safety model: `flacmeta/README.md`.

Write path: lock taken before scanning; hidden temp copy `.flacmeta-<hex>.flac` next to the file
(`cp --reflink=auto`); edit the copy (mutagen, metaflac for seektables); keep owner/group/mode/xattrs
(or refuse); verify STREAMINFO and audio frames byte-identical; refuse if the file changed since it was
read (stat incl. inode+ctime); undo record (JSONL in `~/.local/state/flacmeta/`); fsync; `os.replace`.
Refused files: symlinks, hard links, other users' files, non-UTF-8 Vorbis comments (`not-rewritable` /
`tag-unreadable` in check). replaygain: `rsgain custom --album --tagmode=i --loudness=-18 --clip-mode=p`
on copies of a whole album (multi-disc = one album), every track decode-tested first, own tag order
restored, album swapped in all at once.

## Spectral detection (solved this session; needs numpy)

60 s excerpt, Welch FFT with a Kaiser(20) window into 100 Hz bands, then the sharpest drop above 12 kHz
after which nothing returns: `edge_hz` and `cliff_db`. The old firequalizer method had a ~-86 dBFS floor,
which is why it could not separate fakes. Calibrated on 12 genuine 24-bit Live Music Archive recordings
(archive.org) plus fakes made from them:

| Material | cliff dB | Flagged |
|---|---|---|
| genuine, their 16/44.1 and 24/48 versions, AAC 256k | 4-30 | 0 of 33 |
| soxr upsampled 44.1/48->96, 44.1->48, 48/96->192 | 56-108 | 27 of 27 |
| ffmpeg default resampler 44.1->88.2/96/192 | 23-73 | 19 of 21 |
| MP3 128k/192k/320k | 10-57 | 17 of 21 |
| MP3 V0 | 14-34 | 0 of 7 (undetectable) |

Thresholds: upsampled >= 30 dB (40 dB for 48k files) with edge windows near the old Nyquist
(44.1 source: 20.8-22.2 kHz; 48: 22.9-23.7; images up to 28 kHz); lossy-source >= 25 dB with edge
<= 20.6 kHz. Padded 16-in-24 bit depth: trailing zero bits across the excerpt. Known blind spots:
noise-shaped dither can hide MP3 cliffs; poor-filter upsamplers sometimes stay under 30 dB.

## Review done this session

A multi-agent review (6 reviewers, one verifier per finding; 60 agents) confirmed 46 of 54 findings
(8 medium, 38 low, none corrupting audio). All were fixed; a second pass (8 agents) re-ran every
reproduction on the fixed code (41 fixed, 4 partly, 1 documented) and found 18 regressions in the
fixes, which were fixed too. Biggest items: stale plans overwriting concurrent edits, MutagenError
crashing a run with workers still writing, half-tagged albums on Ctrl+C, torn undo logs, 48k fakes
missed, non-UTF-8 tags mangled. The user asked why verification ran 54 times: the script verified
every finding with its own agent; that was more than needed.
