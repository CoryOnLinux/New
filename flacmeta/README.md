# flacmeta

One tool for a FLAC library: health check, safe tag fixes, ReplayGain 2.0, and `Album (Year)` album tags.

```
flacmeta check PATH...                       read-only report
flacmeta fix PATH... [--album-year]          preview fixes; add --apply to write
flacmeta replaygain PATH... [--force]        preview; add --apply to tag (rsgain, one album gain per album)
flacmeta info FILE...                        everything about a file
flacmeta spectrogram FILE [-o out.png]       PNG spectrogram (ffmpeg)
flacmeta undo LOG                            preview restoring a run; add --apply to restore
```

## Install (CachyOS, fish)

```fish
sudo pacman -S --needed python-mutagen python-numpy flac ffmpeg rsgain   # rsgain comes from chaotic-aur
git clone https://github.com/coryonlinux/new ~/Documents/new
cp -r ~/Documents/new/flacmeta ~/Documents/flacmeta
chmod +x ~/Documents/flacmeta/flacmeta.py
ln -sf ~/Documents/flacmeta/flacmeta.py ~/.local/bin/flacmeta
fish_add_path ~/.local/bin
```

numpy is needed only for the lossy and upsampling checks. Without it, `check` skips them and says so. rsgain is needed only for `replaygain`.

## Typical run

```fish
flacmeta check ~/Music/Qobuz/Albums                          # est. 5-10 min for 7000 files; -v also lists INFO
flacmeta check --quick ~/Music/Qobuz/Albums                  # tags/covers/layout only, seconds
flacmeta fix --album-year ~/Music/Qobuz/Albums | less        # preview
flacmeta fix --album-year --apply ~/Music/Qobuz/Albums
flacmeta replaygain ~/Music/Qobuz/Albums                     # preview: lists albums without RG tags
flacmeta replaygain --apply ~/Music/Qobuz/Albums
flacmeta undo ~/.local/state/flacmeta/fix-20261006-210000-1234.jsonl   # path is printed by --apply
```

`check` exits 1 when it finds an ERROR (a file that does not decode, an MD5 mismatch, an unreadable cover or folder), otherwise 0. `check --json` prints one JSON line per track with its measurements (`effective_bits`, `edge_hz`, `cliff_db`) and findings. `-j N` sets how many files are worked on at once (default: one per CPU); it works before or after the command.

## Safety

- Nothing is written without `--apply`. `check`, `info` and the previews only read.
- Each changed file is copied to a hidden `.flacmeta-*` file next to it (`cp --reflink`, so it is instant on btrfs). The copy is edited, then checked: STREAMINFO is unchanged, the audio frames are byte-identical to the original, and the tags came out as planned. Only then is an undo record written, the copy synced to disk and renamed over the original. The copy gets the original's owner, group, permissions and extended attributes (ACLs included); if it can't keep the owner and group, the file is left alone.
- A file that changed after flacmeta read it, whether from another tagger or another flacmeta run, is skipped with "changed since flacmeta read it; run again". Writing runs take a lock before they start reading, so two of them can't overlap.
- flacmeta never replaces symlinks, hard-linked files, files owned by another user (unless run as root), or files whose Vorbis comments aren't valid UTF-8 `KEY=value` text (rewriting those would lose bytes that can't be restored). `check` reports them as `not-rewritable` or `tag-unreadable` and doesn't count their other findings as fixable; `fix` and `replaygain` skip them in the preview.
- Ctrl+C or SIGTERM: files already replaced stay done, and the undo log path is printed. Files in progress stay untouched, with no temp files left behind. Rerun to finish. An album being ReplayGain-tagged is swapped in all at once, never half. Only a hard kill or a power cut can leave a `.flacmeta-*` file; `check` reports leftovers, which are safe to delete.
- Undo logs (JSON lines) are in `~/.local/state/flacmeta/`. They hold the old tags, the old picture headers and the exact bytes of any stripped ID3 tag. `undo` restores only the parts that log changed, so a later run's changes to other parts survive. It skips a file whose changed parts were modified again since (`--force` overrides). A record cut short by a crash is skipped with a warning, and the rest of the log still works.

## What `check` reports

| Code | Level | Meaning | `fix` |
|---|---|---|---|
| `decode-error`, `md5-mismatch` | ERROR | `flac -t` failed: the audio is damaged | no: restore from the source |
| `md5-missing` | WARN | no MD5 in STREAMINFO, so decoding can't be verified | no: needs a re-encode |
| `id3-glued` | WARN | ID3v2/ID3v1/APE tag stuck to the FLAC file | strips it (bytes kept in the undo log) |
| `no-seektable` | INFO | slow seeking | adds one (a point every 10 s) |
| `padded-bit-depth` | WARN | a 24-bit file whose low 8 bits are always zero: really 16-bit | no |
| `upsampled` | WARN | nothing above ~21-25 kHz in an 88.2-192k file, or above ~21-22 kHz in a 48k file (made from 44.1k); 176.4/192k files are also checked for 88.2/96k sources | no |
| `lossy-source` | WARN | sharp cutoff below ~20.5 kHz: probably made from an MP3 (a hi-res file can get both) | no |
| `tag-missing`, `-empty`, `-whitespace`, `-duplicate`, `-multiple`, `-malformed`, `-synonym` | WARN/INFO | missing required tags, empty values, stray spaces, repeated values, `3/12` track numbers, `TOTALTRACKS` instead of `TRACKTOTAL`, bad DATE/ISRC/BARCODE | empty, whitespace, duplicate, `N/M` and synonyms |
| `tag-conflict` | WARN | `TOTALTRACKS`/`TOTALDISCS` disagree with `TRACKTOTAL`/`DISCTOTAL` | no: decide which is right |
| `filename-mismatch` | WARN | `05 - …` but TRACKNUMBER=6 (`101 - …` = disc 1 track 1) | no |
| `album-inconsistent` | WARN | ALBUM, ALBUMARTIST, DATE, GENRE, LABEL, … differ inside an album | fills a tag that's missing on some tracks when the rest agree |
| `album-incomplete`, `track-duplicate` | WARN | gaps or repeats in track numbers, missing discs | no |
| `replaygain-missing`, `-partial`, `-malformed`, `-album-mismatch` | INFO/WARN | | use `flacmeta replaygain` |
| `cover-missing`, `-small`, `-big`, `-header`, `-mime`, `-bad` | WARN/INFO/ERROR | no front cover, < 500 px, > 3000 px or 2 MB, header says 0x0, wrong MIME type, unreadable image | header and MIME |
| `cover-file-missing`, `folder-year-mismatch`, `album-mixed-format` | INFO | no cover.jpg in the folder, folder year ≠ DATE, mixed formats | no |
| `not-rewritable`, `tag-unreadable` | WARN | a file fix/replaygain won't replace (see Safety) | no |
| `unreadable-folder`, `leftover-temp`, `check-failed` | ERROR/WARN | a folder flacmeta can't read, a `.flacmeta-*` file from a killed run (files of a run still in progress are named as such, not counted), a file or album the checks crashed on | no |

Albums are grouped by folder, with `Disc 01`, `Disc 02`, … merged into their album. Qobuz writes the whole album's track count into TRACKTOTAL on every disc. That's understood, so multi-disc albums don't show up as incomplete. Track numbering is checked only when every file of the album was scanned.

`--album-year` turns `ALBUM=Curtis` into `Curtis (1970)`, with the year taken from DATE, to match `~/Qobuz Downloads`. The year must be the same on every track of the album, otherwise the album is skipped (and says so) rather than split in two. It's idempotent: an ALBUM that already ends in `(1970)` is left alone.

`replaygain` runs `rsgain custom --album --tagmode=i --loudness=-18 --clip-mode=p`. That's ReplayGain 2.0, the same settings as rsgain's easy mode. It runs on copies of all of an album's tracks at once, so a multi-disc album gets one album gain. Albums whose tracks all have valid track and album RG tags are skipped unless `--force`. rsgain (through TagLib) re-sorts and upper-cases every tag, so flacmeta puts the file's own tag order back. Only the REPLAYGAIN_* values change; anything else aborts that album. Albums with glued ID3/APE tags are skipped until `fix` has stripped them. Every track is decode-tested first: a damaged track fails its album instead of getting a gain and skewing the album's.

## How the lossy/upsampling check works

ffmpeg decodes a 60 s excerpt (starting at ~30% of the track). numpy averages hundreds of overlapping FFTs over it into 100 Hz bands. A Kaiser window (β=20) keeps the leakage far below the 24-bit noise floor, so an empty band reads as empty. The old firequalizer method bottomed out around −86 dBFS, which is why it couldn't tell fakes apart. The check then finds the sharpest drop above 12 kHz after which the spectrum never comes back, and reports where it is (`edge_hz`) and how deep it is (`cliff_db`).

Calibration: 12 genuine 24-bit live recordings (7 at 96 kHz) from the Internet Archive's Live Music Archive, plus files made from them with ffmpeg (soxr and its default resampler) and LAME:

| Material | cliff_db | Flagged |
|---|---|---|
| genuine recordings, their 16/44.1 and 24/48 versions, AAC 256k | 4 – 30 | 0 of 33 |
| upsampled with soxr: 44.1/48 → 96, 44.1 → 48, 48/96 → 192 | 56 – 108 | 27 of 27 (`upsampled`, source rate named) |
| upsampled with ffmpeg's default resampler: 44.1 → 88.2/96/192 | 23 – 73 | 19 of 21 |
| MP3 128k / 192k / 320k → FLAC | 10 – 57 | 17 of 21 (`lossy-source`) |
| MP3 V0 → FLAC | 14 – 34 | 0 of 7 |

Thresholds: `upsampled` needs a cliff of at least 30 dB (40 dB for 48 kHz files), `lossy-source` at least 25 dB with an edge at or below 20.6 kHz. They're constants at the top of `flacmeta.py`.

## Limits

- **The thresholds have not yet been run on your library.** The calibration material was live concert recordings, not studio masters. Run `flacmeta check --json` once and look at the `cliff_db` values of files you trust before acting on a `lossy-source` flag.
- A genuine CD master with a steep 19–20 kHz mastering low-pass looks like an MP3 source. That's why it says *possible*: check with `flacmeta spectrogram`.
- MP3 at V0, AAC at 256k and other high-bitrate lossy sources leave no cutoff below 20.5 kHz, so they can't be detected this way. MP3 320k usually can.
- An upsampled file with added noise above the old Nyquist (noise-shaped dither, deliberate "hi-res" noise) can hide the cliff. So can strong noise-shaped dither on a 16-bit master made from an MP3 (Shibata-type dither brought one 128k cliff from 50 dB down to 13 dB).
- Resamplers with poor filters (ffmpeg's default) leave images above the old Nyquist; 2 of 21 such fakes stayed under the threshold, and the others are usually named "44.1 or 48 kHz" because the images hide which it was.
- `padded-bit-depth` catches zero padding only. 16-bit audio that was gain-changed or processed in 24-bit uses all 24 bits and isn't detected.
- `--album-year` was written to match what you described (`Curtis (2007)`). The Qobuz-DL sources in this repo don't contain `tag_album_from_folder_format`, so it isn't checked against your Qobuz-DL version. Compare one album with `~/Qobuz Downloads` before running it on everything.

## Tests

```fish
cd ~/Documents/flacmeta; python -m unittest discover -s tests -v
```

The tests build a small Qobuz-style library with ffmpeg and mutagen: glued ID3 tags, zero-dimension covers, messy tags, a multi-disc album, an album with gaps, odd file names, and upsampled, padded, MP3-sourced and corrupt files. They run the real commands on it: the check results, the fix preview leaving everything byte-identical, fix → recheck → undo round-trips, a failing step leaving the file alone, SIGTERM mid-run, and rsgain album gain across two discs.
