#!/usr/bin/env bash
# make-media.sh - generate small MKV fixtures for the mkv-to-mp4 eval.
#   full DIR    the mixed library: every audio/subtitle case, odd names, cover art,
#               10-bit HEVC, chapters, and an MKV that already has its MP4
#   single DIR  one plain H.264 + AAC file (single.mkv), SECONDS long (default 4)
set -Eeuo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly HERE
ff() { ffmpeg -nostdin -hide_banner -loglevel error -y "$@"; }
die() { printf 'make-media: %s\n' "$*" >&2; exit 1; }

full() {
  local d=$1 tmp
  tmp=$(mktemp -d)
  trap 'rm -rf -- "$tmp"' RETURN
  mkdir -p -- "$d/Season:1" "$d/cover" "$d/already" "$d/Extras"

  printf '1\n00:00:00,200 --> 00:00:01,400\nHello\n\n2\n00:00:01,600 --> 00:00:02,800\nWorld\n' >"$tmp/s.srt"
  printf ';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=One\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=Two\n' >"$tmp/chapters.txt"
  python3 "$HERE/make_pgs.py" "$tmp/s.sup" 320 240 2

  # Every audio codec class a remux has to decide about, plus text and picture subs.
  #  a0 aac (eng)  a1 flac (jpn)  a2 opus  a3 pcm_s24le  a4 truehd  a5 dts  a6 vorbis
  #  s0 srt (eng)  s1 ass (jpn)   s2 PGS (eng)  s3 VobSub (ger)
  # (-fix_sub_duration: without it the VobSub's last caption never ends and the
  #  file reports a 1193-hour duration)
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 \
    -f lavfi -i "sine=f=440:d=3:sample_rate=48000" \
    -i "$tmp/s.srt" -i "$tmp/s.sup" -i "$tmp/chapters.txt" -fix_sub_duration -i "$tmp/s.sup" \
    -map 0:v -map 1:a -map 1:a -map 1:a -map 1:a -map 1:a -map 1:a -map 1:a \
    -map 2:s -map 2:s -map 3:s -map 5:s -map_chapters 4 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -ac 2 \
    -c:a:0 aac -c:a:1 flac -c:a:2 libopus -c:a:3 pcm_s24le -c:a:4 truehd -c:a:5 dca -c:a:6 libvorbis \
    -strict -2 \
    -c:s:0 srt -c:s:1 ass -c:s:2 copy -c:s:3 dvdsub \
    -metadata:s:a:0 language=eng -metadata:s:a:1 language=jpn \
    -metadata:s:s:0 language=eng -metadata:s:s:1 language=jpn \
    -metadata:s:s:2 language=eng -metadata:s:s:3 language=ger \
    -disposition:s:1 forced \
    "$d/Season:1/ep 01 [x].mkv"

  # Already HEVC 10-bit: a remux candidate, and 10-bit must survive a re-encode.
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 -f lavfi -i "sine=d=3:sample_rate=48000" \
    -c:v libx265 -preset ultrafast -x265-params log-level=error -pix_fmt yuv420p10le \
    -c:a eac3 -ac 2 "$d/-leading dash.mkv"

  # A newline in the name.
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 -f lavfi -i "sine=d=3:sample_rate=48000" \
    -c:v libx264 -preset ultrafast -c:a aac "$d/new"$'\n'"line.mkv"

  # Cover art: shows up as an attached-picture video stream that must not be encoded.
  ff -f lavfi -i "color=c=red:s=64x64:d=1" -frames:v 1 "$tmp/cover.jpg"
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 -f lavfi -i "sine=d=3:sample_rate=48000" \
    -c:v libx264 -preset ultrafast -c:a aac \
    -attach "$tmp/cover.jpg" -metadata:s:t:0 mimetype=image/jpeg -metadata:s:t:0 filename=cover.jpg \
    "$d/cover/with cover.mkv"

  # Already converted on an earlier run: must be left alone.
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 -f lavfi -i "sine=d=3:sample_rate=48000" \
    -c:v libx264 -preset ultrafast -c:a aac "$d/already/done.mkv"
  ff -i "file:$d/already/done.mkv" -c copy "$d/already/done.mp4"
  touch -d '2020-01-01 00:00' -- "$d/already/done.mp4"

  # Upper-case extension: "all the mkv files" arguably includes it.
  ff -f lavfi -i testsrc2=s=320x240:r=24:d=3 -f lavfi -i "sine=d=3:sample_rate=48000" \
    -c:v libx264 -preset ultrafast -c:a aac "$d/Extras/TRAILER.MKV"
}

single() {
  local d=$1 secs=${2:-4}
  mkdir -p -- "$d"
  ff -f lavfi -i "testsrc2=s=320x240:r=24:d=$secs" -f lavfi -i "sine=d=$secs:sample_rate=48000" \
    -c:v libx264 -preset ultrafast -c:a aac "$d/single.mkv"
}

(($# >= 2)) || die "usage: make-media.sh full|single DIR [SECONDS]"
command -v ffmpeg >/dev/null || die "needs ffmpeg"
case $1 in
  full) full "$2" ;;
  single) single "$2" "${3:-4}" ;;
  *) die "unknown mode: $1" ;;
esac
