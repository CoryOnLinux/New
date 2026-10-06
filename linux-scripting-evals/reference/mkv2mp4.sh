#!/usr/bin/env bash
# mkv2mp4.sh - convert every .mkv under the given folders to an .mp4 beside it,
# encoding video on the AMD GPU (VAAPI) and keeping all audio and text subtitles.
# Rerun-safe: finished .mp4 files are skipped, and partial ones never look finished.
# Reference answer for eval 1: the benchmark's skill-run script with its two gaps fixed
# (lossless audio stays lossless; picture subtitles go to sidecar files).
set -Eeuo pipefail
shopt -s nullglob

readonly PROG=${0##*/}
DRY_RUN=0
CODEC=${CODEC:-hevc}                    # hevc | h264 | av1
QP=${QP:-}                              # empty = per-codec default (see usage)
VAAPI_DEVICE=${VAAPI_DEVICE:-}          # e.g. /dev/dri/renderD129; empty = auto-detect the dGPU
AAC_KBPS_PER_CH=${AAC_KBPS_PER_CH:-96}  # bitrate for audio that has to be re-encoded
SYSFS=${SYSFS:-/sys}
DRI_DIR=${DRI_DIR:-/dev/dri}

# Audio that MP4 carries is copied untouched; lossless audio MP4 can't carry well
# (TrueHD, PCM...) becomes FLAC; other lossy audio (Vorbis...) becomes AAC. Text subs
# become mov_text. Picture subs (PGS, VobSub, DVB) can't be stored in MP4, so they are
# written next to it as .sup (PGS) or .mks (others) files that players pick up.
readonly COPY_AUDIO=' aac ac3 eac3 mp3 mp2 opus flac alac dts '
readonly LOSSLESS_AUDIO='^(truehd|mlp|pcm_.*|wavpack|tta|ape)$'
readonly TEXT_SUBS=' subrip srt ass ssa webvtt mov_text text microdvd subviewer subviewer1 sami mpl2 realtext '
readonly HIGH_DEPTH='(10|12|14|16)[bl]e$'   # pix_fmt suffix of >8-bit video, e.g. yuv420p10le

NODE='' CUR_TMP='' TAG='' FF_PID=''
n_ok=0 n_skip=0 n_fail=0

log()   { printf '%s: %s\n' "$PROG" "$*" >&2; }
die()   { log "error: $*"; exit 1; }
need()  { command -v "$1" >/dev/null 2>&1 || die "missing '$1' (sudo pacman -S --needed ${2:-$1})"; }
usage() {
  cat <<EOF
Usage: $PROG [-n] [-c hevc|h264|av1] [-q QP] DIR|FILE...
Convert every .mkv under DIR (recursively) to an .mp4 beside it, with the video
encoded on the AMD GPU via VAAPI. MKVs whose .mp4 already exists are skipped.
  -c CODEC  video codec: hevc (default), h264, av1
  -q QP     constant quality, lower = better and bigger
            defaults: hevc 24, h264 22, av1 96 (AV1 uses a 0-255 scale)
  -n        dry run: show what would be done, write nothing
  -h        this help
Env: VAAPI_DEVICE=/dev/dri/renderD129 picks the render node by hand.
EOF
}

summary() {
  local verb=converted
  if ((DRY_RUN)); then verb='to convert'; fi
  log "done: $n_ok $verb, $n_skip skipped, $n_fail failed"
}
drop_tmp()  { if [[ -n $CUR_TMP ]]; then rm -f -- "$CUR_TMP"; fi; CUR_TMP=''; }
on_signal() {
  log "interrupted"
  # The partial output is discarded anyway, so stop ffmpeg now rather than let it finalize.
  if [[ -n $FF_PID ]]; then kill -KILL "$FF_PID" 2>/dev/null || true; wait "$FF_PID" 2>/dev/null || true; fi
  summary
  exit "$1"
}
trap drop_tmp EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'log "failed at line $LINENO: $BASH_COMMAND"' ERR

# Ryzen 9000 has an amdgpu iGPU too, and card/renderD numbers aren't stable, so take
# the AMD card with the most VRAM and the render node on the same PCI device.
find_render_node() {
  local card vendor vram best='' best_vram=-1 r
  for card in "$SYSFS"/class/drm/card*; do
    [[ ${card##*/} =~ ^card[0-9]+$ ]] || continue
    read -r vendor 2>/dev/null <"$card/device/vendor" || continue
    [[ $vendor == 0x1002 ]] || continue
    read -r vram 2>/dev/null <"$card/device/mem_info_vram_total" || vram=0
    [[ $vram =~ ^[0-9]+$ ]] || vram=0
    if ((vram > best_vram)); then best=$card best_vram=$vram; fi
  done
  [[ -n $best ]] || return 1
  best=$(readlink -f -- "$best/device")
  for r in "$SYSFS"/class/drm/renderD*; do
    if [[ $(readlink -f -- "$r/device") == "$best" ]]; then NODE=$DRI_DIR/${r##*/}; return 0; fi
  done
  return 1
}

# Fail fast (missing VAAPI driver, encoder unsupported) before touching any file.
test_encoder() {
  local err
  if ! err=$(ffmpeg -nostdin -hide_banner -loglevel error \
      -init_hw_device "vaapi=va:$NODE" -filter_hw_device va \
      -f lavfi -i testsrc2=size=640x360:rate=30:duration=0.5 \
      -vf 'format=nv12,hwupload' -c:v "${CODEC}_vaapi" -f null - 2>&1); then
    die "test encode with ${CODEC}_vaapi on $NODE failed: ${err:-no output}
  check the driver with: vainfo --display drm --device $NODE"
  fi
}

# "5025.042000" -> milliseconds, into the variable named by $2 (0 if unknown)
to_ms() {
  local frac=0
  if [[ $1 =~ ^([0-9]+)(\.([0-9]+))?$ ]]; then
    frac=${BASH_REMATCH[3]}000
    printf -v "$2" '%d' $((10#${BASH_REMATCH[1]} * 1000 + 10#${frac:0:3}))
  else
    printf -v "$2" '%d' 0
  fi
}

# Returns 0 converted, 1 failed, 2 skipped. Called in an `||` context, so set -e
# is off in here and every step checks its own result.
convert_one() {
  local src=$1 dir name stem out tmp probe line f idx type codec ch pix pic lang
  local dur='' vidx='' vpix='' fmt=nv12 vf o=1 rc=1 attempt d src_ms out_ms start=$SECONDS
  local -a fields=() maps=() codecs=() dropped=() venc=() cmd=() pics=()
  dir=${src%/*} name=${src##*/}
  stem=${name%.*}
  out=$dir/$stem.mp4
  tmp=$dir/.$stem.mp4.part   # same folder, so the final mv is atomic

  if [[ -s $out ]]; then log "$TAG skip, already converted: $out"; return 2; fi
  if [[ ! -w $dir ]]; then log "$TAG FAIL, folder not writable: $dir"; return 1; fi
  if ! probe=$(ffprobe -v error -of compact=p=0 -show_entries \
      'format=duration:stream=index,codec_type,codec_name,channels,pix_fmt:stream_disposition=attached_pic:stream_tags=language' \
      "file:$src"); then
    log "$TAG FAIL, ffprobe can't read: $src"; return 1
  fi

  while IFS= read -r line; do
    idx='' type='' codec='' ch='' pix='' pic=0 lang=''
    IFS='|' read -ra fields <<<"$line"
    for f in "${fields[@]}"; do
      case $f in
        index=*) idx=${f#*=} ;;            codec_type=*) type=${f#*=} ;;
        codec_name=*) codec=${f#*=} ;;     channels=*) ch=${f#*=} ;;
        pix_fmt=*) pix=${f#*=} ;;          disposition:attached_pic=*) pic=${f#*=} ;;
        tag:language=*) lang=${f#*=} ;;    duration=*) dur=${f#*=} ;;
      esac
    done
    [[ -n $idx ]] || continue
    case $type in
      video)
        # MKV cover art shows up as an attached-picture video stream: never encode it.
        if [[ $pic != 0 ]]; then continue; fi
        if [[ -z $vidx ]]; then vidx=$idx vpix=$pix; else dropped+=("#$idx extra video track"); fi ;;
      audio)
        maps+=(-map "0:$idx")
        if [[ $COPY_AUDIO == *" $codec "* ]]; then
          codecs+=("-c:$o" copy)
        elif [[ $codec =~ $LOSSLESS_AUDIO ]]; then
          codecs+=("-c:$o" flac)
        else
          [[ $ch =~ ^[1-9][0-9]*$ ]] || ch=2
          codecs+=("-c:$o" aac "-b:$o" "$((ch * AAC_KBPS_PER_CH))k")
        fi
        o=$((o + 1)) ;;
      subtitle)
        if [[ $TEXT_SUBS == *" $codec "* ]]; then
          maps+=(-map "0:$idx"); codecs+=("-c:$o" mov_text); o=$((o + 1))
        else
          pics+=("$idx:$codec:${lang:-und}")
        fi ;;
    esac
  done <<<"$probe"

  if [[ -z $vidx ]]; then log "$TAG FAIL, no video stream: $src"; return 1; fi
  if ((${#dropped[@]})); then
    printf -v f '%s, ' "${dropped[@]}"
    log "$TAG note, not carried over to MP4: ${f%, }"
  fi

  if [[ $vpix =~ $HIGH_DEPTH && $CODEC != h264 ]]; then fmt=p010; fi
  # "|vaapi" lets GPU-decoded frames pass straight through hwupload, while
  # CPU-decoded frames get converted and uploaded.
  vf="format=$fmt|vaapi,hwupload"
  # h264_vaapi is 8-bit only, so GPU-decoded 10-bit frames are converted on the GPU.
  if [[ $CODEC == h264 && $vpix =~ $HIGH_DEPTH ]]; then vf+=',scale_vaapi=format=nv12'; fi
  venc=(-vf "$vf" -c:v "${CODEC}_vaapi" -rc_mode CQP -global_quality "$QP")
  # hvc1 (not ffmpeg's default hev1) is what Apple/QuickTime and many TVs require.
  if [[ $CODEC == hevc ]]; then venc+=(-tag:v hvc1); fi
  maps=(-map "0:$vidx" "${maps[@]}")

  # Try full-GPU decode+encode first; if the GPU can't decode the source
  # (10-bit H.264, MPEG-4 ASP, ...), retry with CPU decode + GPU encode.
  for attempt in gpu cpu; do
    cmd=(ffmpeg -nostdin -hide_banner -loglevel error -stats -y
      -init_hw_device "vaapi=va:$NODE" -filter_hw_device va)
    if [[ $attempt == gpu ]]; then cmd+=(-hwaccel vaapi -hwaccel_device va -hwaccel_output_format vaapi); fi
    cmd+=(-i "file:$src" "${maps[@]}" "${venc[@]}" "${codecs[@]}"
      -max_muxing_queue_size 4096 -movflags +faststart -f mp4 "file:$tmp")
    if ((DRY_RUN)); then log "$TAG would run: $(printf '%q ' "${cmd[@]}")"; return 0; fi

    log "$TAG converting ($attempt decode): $src"
    CUR_TMP=$tmp rc=0
    # Run in the background and wait, so Ctrl+C / kill take effect immediately
    # instead of after the current file finishes.
    "${cmd[@]}" &
    FF_PID=$!
    wait "$FF_PID" || rc=$?
    FF_PID=''
    if ((rc == 0)); then break; fi
    drop_tmp
    # ffmpeg exits 255 when it caught Ctrl+C: stop the batch instead of retrying.
    if ((rc == 255)); then on_signal 130; fi
    if [[ $attempt == gpu ]]; then log "$TAG failed with GPU decode (exit $rc), retrying with CPU decode"; fi
  done
  if ((rc != 0)); then log "$TAG FAIL, ffmpeg exit $rc: $src"; return 1; fi

  # A truncated output would be skipped forever on reruns, so check before keeping it.
  to_ms "$dur" src_ms
  out_ms=0
  if d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "file:$tmp"); then to_ms "$d" out_ms; fi
  if ((out_ms == 0 || out_ms < src_ms * 9 / 10)); then
    drop_tmp; log "$TAG FAIL, output too short ($((out_ms / 1000))s of $((src_ms / 1000))s): $src"; return 1
  fi
  if ! mv -f -- "$tmp" "$out"; then drop_tmp; log "$TAG FAIL, can't move output into place: $out"; return 1; fi
  CUR_TMP=''

  # Picture subtitles: one sidecar per track, also via a temp name.
  for f in "${pics[@]}"; do
    IFS=: read -r idx codec lang <<<"$f"
    if [[ $codec == hdmv_pgs_subtitle ]]; then d=sup; else d=mks; fi
    o=$dir/$stem.$lang.$d
    if [[ -e $o ]]; then o=$dir/$stem.$lang.$idx.$d; fi
    CUR_TMP=$dir/.$stem.$idx.$d.part
    if ffmpeg -nostdin -hide_banner -loglevel error -y -i "file:$src" -map "0:$idx" -c copy \
        -f "$([[ $d == sup ]] && echo sup || echo matroska)" "file:$CUR_TMP" && mv -f -- "$CUR_TMP" "$o"; then
      log "$TAG subtitle #$idx ($codec, $lang) -> ${o##*/}"
    else
      log "$TAG warning: couldn't extract subtitle #$idx ($codec)"
    fi
    drop_tmp
  done
  log "$TAG ok ($((SECONDS - start))s): $out"
}

main() {
  local opt f qmax i=0 rc
  local -a roots=() files=()
  while getopts ':nc:q:h' opt; do
    case $opt in
      n) DRY_RUN=1 ;;
      c) CODEC=$OPTARG ;;
      q) QP=$OPTARG ;;
      h) usage; exit 0 ;;
      :) usage >&2; die "-$OPTARG needs a value" ;;
      *) usage >&2; exit 2 ;;
    esac
  done
  shift $((OPTIND - 1))
  if (($# == 0)); then usage >&2; exit 2; fi

  case $CODEC in
    hevc) : "${QP:=24}"; qmax=51 ;;
    h264) : "${QP:=22}"; qmax=51 ;;
    av1)  : "${QP:=96}"; qmax=255 ;;
    *) die "unknown codec '$CODEC' (use hevc, h264 or av1)" ;;
  esac
  if ! [[ $QP =~ ^[0-9]+$ ]] || ((10#$QP > qmax)); then die "QP must be 0-$qmax for $CODEC, got '$QP'"; fi
  QP=$((10#$QP))

  for f in "$@"; do
    [[ -e $f ]] || die "no such file or folder: $f"
    if [[ $f == -* ]]; then f=./$f; fi   # find would read a leading-dash name as an option
    roots+=("$f")
  done

  need ffmpeg
  need ffprobe ffmpeg
  need flock util-linux

  if [[ -n $VAAPI_DEVICE ]]; then
    NODE=$VAAPI_DEVICE
  elif ! find_render_node; then
    die "no AMD GPU render node found; set VAAPI_DEVICE=/dev/dri/renderD12x"
  fi
  [[ -r $NODE && -w $NODE ]] || die "can't open $NODE (missing, or no permission)"
  log "using $NODE with ${CODEC}_vaapi, QP $QP"

  if ((!DRY_RUN)); then
    # Two runs at once would fight over the GPU and the same temp files.
    exec 9>"${XDG_RUNTIME_DIR:-/tmp}/mkv2mp4.lock"
    flock -n 9 || die "another $PROG run is already active"
    test_encoder
  fi

  # Skip desktop trash folders (.Trash-1000) on external drives.
  mapfile -d '' files < <(find -H "${roots[@]}" -name '.Trash-*' -prune -o \
    -type f -iname '*.mkv' -print0 | sort -z)
  if ((${#files[@]} == 0)); then log "no .mkv files found"; exit 0; fi

  for f in "${files[@]}"; do
    i=$((i + 1))
    TAG="[$i/${#files[@]}]"
    if [[ $f != */* ]]; then f=./$f; fi
    rc=0
    convert_one "$f" || rc=$?
    case $rc in
      0) n_ok=$((n_ok + 1)) ;;
      2) n_skip=$((n_skip + 1)) ;;
      *) n_fail=$((n_fail + 1)) ;;
    esac
  done
  summary
  ((n_fail == 0)) || exit 1
}

main "$@"
