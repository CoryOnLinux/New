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

`check` exits 1 when it finds an ERROR (a file that does not decode, an MD5 mismatch, an unreadable cover), otherwise 0. `check --json` prints one JSON line per track with its measurements (`effective_bits`, `edge_hz`, `cliff_db`) and findings.

## Safety

- Nothing is written without `--apply`. `check`, `info` and the previews only read.
- Each changed file is copied to a hidden `.flacmeta-*` file next to it (`cp --reflink`, so it is instant on btrfs). The copy is edited, then checked: STREAMINFO is unchanged, the audio frames are byte-identical to the original, and the tags came out as planned. Only then is an undo record written and the copy renamed over the original. If the original changed in the meantime, the file is skipped.
- Ctrl+C or SIGTERM: files that were already replaced stay done, and files still in progress stay untouched. No temp files are left behind. Rerun to finish. Only a hard kill or a power cut can leave a `.flacmeta-*` file. It is a complete copy and safe to delete.
- Undo logs (JSON lines) are in `~/.local/state/flacmeta/`. They hold the old tags, the old picture headers and the exact bytes of any stripped ID3 tag. `undo` restores a file only if its tags still match what the run wrote (`--force` overrides).
- Only one writing run at a time (lock file).

## What `check` reports

| Code | Level | Meaning | `fix` |
|---|---|---|---|
| `decode-error`, `md5-mismatch` | ERROR | `flac -t` failed: the audio is damaged | no: restore from the source |
| `md5-missing` | WARN | no MD5 in STREAMINFO, so decoding can't be verified | no: needs a re-encode |
| `id3-glued` | WARN | ID3v2/ID3v1/APE tag stuck to the FLAC file | strips it (bytes kept in the undo log) |
| `no-seektable` | INFO | slow seeking | adds one (a point every 10 s) |
| `padded-bit-depth` | WARN | a 24-bit file whose low 8 bits are always zero: really 16-bit | no |
| `upsampled` | WARN | hi-res file with nothing above 22/24 kHz (or 44/48 kHz on 176.4/192k files) | no |
| `lossy-source` | WARN | sharp cutoff below 20.5 kHz: probably made from an MP3 | no |
| `tag-missing`, `-empty`, `-whitespace`, `-duplicate`, `-multiple`, `-malformed`, `-synonym` | WARN/INFO | missing required tags, empty values, stray spaces, repeated values, `3/12` track numbers, `TOTALTRACKS` instead of `TRACKTOTAL`, bad DATE/ISRC/BARCODE | empty, whitespace, duplicate, `N/M` and synonyms |
| `filename-mismatch` | WARN | `05 - …` but TRACKNUMBER=6 (`101 - …` = disc 1 track 1) | no |
| `album-inconsistent` | WARN | ALBUM, ALBUMARTIST, DATE, GENRE, LABEL, … differ inside an album | fills a tag that's missing on some tracks when the rest agree |
| `album-incomplete`, `track-duplicate` | WARN | gaps or repeats in track numbers, missing discs | no |
| `replaygain-missing`, `-partial`, `-malformed`, `-album-mismatch` | INFO/WARN | | use `flacmeta replaygain` |
| `cover-missing`, `-small`, `-big`, `-header`, `-mime`, `-bad` | WARN/INFO/ERROR | no front cover, < 500 px, > 3000 px or 2 MB, header says 0x0, wrong MIME type, unreadable image | header and MIME |
| `cover-file-missing`, `folder-year-mismatch`, `album-mixed-format` | INFO | no cover.jpg in the folder, folder year ≠ DATE, mixed formats | no |

Albums are grouped by folder, with `Disc 01`, `Disc 02`, … merged into their album. Qobuz writes the whole album's track count into TRACKTOTAL on every disc. That's understood, so multi-disc albums don't show up as incomplete. Track numbering is checked only when every file of the album was scanned.

`--album-year` turns `ALBUM=Curtis` into `Curtis (1970)`, with the year taken from DATE, to match `~/Qobuz Downloads`. It's idempotent: an ALBUM that already ends in `(1970)` is left alone.

`replaygain` runs `rsgain custom --album --tagmode=i --loudness=-18 --clip-mode=p`. That's ReplayGain 2.0, the same settings as rsgain's easy mode. It runs on copies of all of an album's tracks at once, so a multi-disc album gets one album gain. Albums whose tracks all have valid track and album RG tags are skipped unless `--force`. rsgain may change only the REPLAYGAIN_* tags; anything else aborts that album.

## How the lossy/upsampling check works

ffmpeg decodes a 60 s excerpt (starting at ~30% of the track). numpy averages hundreds of overlapping FFTs over it into 100 Hz bands. A Kaiser window (β=20) keeps the leakage far below the 24-bit noise floor, so an empty band reads as empty. The old firequalizer method bottomed out around −86 dBFS, which is why it couldn't tell fakes apart. The check then finds the sharpest drop above 12 kHz after which the spectrum never comes back, and reports where it is (`edge_hz`) and how deep it is (`cliff_db`).

Calibration: 12 genuine 24-bit live recordings (7 at 96 kHz) from the Internet Archive's Live Music Archive, plus files made from them with ffmpeg/soxr and LAME:

| Material | cliff_db | Flagged |
|---|---|---|
| genuine recordings, their 16/44.1 versions, AAC 256k | 4 – 27 | 0 of 26 |
| upsampled 44.1/48 → 96, 48/96 → 192 | 63 – 108 | 20 of 20 (`upsampled`, source rate named) |
| MP3 128k / 192k / 320k → FLAC | 10 – 57 | 17 of 21 (`lossy-source`) |
| MP3 V0 → FLAC | 14 – 34 | 0 of 7 |

Thresholds: `upsampled` needs a cliff of at least 40 dB, `lossy-source` at least 25 dB with an edge below 20.5 kHz. Both are constants at the top of `flacmeta.py`.

## Limits

- **The thresholds have not yet been run on your library.** The calibration material was live concert recordings, not studio masters. Run `flacmeta check --json` once and look at the `cliff_db` values of files you trust before acting on a `lossy-source` flag.
- A genuine CD master with a steep 19–20 kHz mastering low-pass looks like an MP3 source. That's why it says *possible*: check with `flacmeta spectrogram`.
- MP3 at V0, AAC at 256k and other high-bitrate lossy sources leave no cutoff below 20.5 kHz, so they can't be detected this way. MP3 320k usually can.
- An upsampled file with added noise above the old Nyquist (noise-shaped dither, deliberate "hi-res" noise) can hide the cliff.
- `padded-bit-depth` catches zero padding only. 16-bit audio that was gain-changed or processed in 24-bit uses all 24 bits and isn't detected.
- `--album-year` was written to match what you described (`Curtis (2007)`). The Qobuz-DL sources in this repo don't contain `tag_album_from_folder_format`, so it isn't checked against your Qobuz-DL version. Compare one album with `~/Qobuz Downloads` before running it on everything.

## Tests

```fish
cd ~/Documents/flacmeta; python -m unittest discover -s tests -v
```

The tests build a small Qobuz-style library with ffmpeg and mutagen: glued ID3 tags, zero-dimension covers, messy tags, a multi-disc album, an album with gaps, odd file names, and upsampled, padded, MP3-sourced and corrupt files. They run the real commands on it: the check results, the fix preview leaving everything byte-identical, fix → recheck → undo round-trips, a failing step leaving the file alone, SIGTERM mid-run, and rsgain album gain across two discs.
