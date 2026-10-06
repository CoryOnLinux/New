# ffmpeg and media conversion

Read before writing anything that converts, remuxes or encodes media. Behaviour marked *verified* was checked on ffmpeg 6.1. Check the rest on the user's machine (`ffmpeg -version`) and in your own test run.

## Ground rules

- Always `-nostdin` (or ffmpeg eats a `while read` loop's input) and `-hide_banner -loglevel error -stats`.
- Pass every path as `file:$path`. Without it, `Season:1/ep.mkv` is read as a protocol and fails *(verified)*.
- Write to a temp name in the output folder with an explicit format (`-f mp4 "file:$dir/.$stem.mp4.part"`), validate, then `mv`. A failed TrueHD-in-MP4 write left an unplayable file with no moov atom *(verified)*.
- Build `-map` per stream from `ffprobe -of json -show_streams`. `-map 0 -c copy` into MP4 fails as soon as one stream can't go there *(verified)*.
- Validate before keeping: ffprobe reads it, its duration is within ~1% (or 1 s) of the source, and it has the stream count you planned. A truncated file that exits 0 is otherwise skipped forever on reruns.

## Cheapest correct route first

| Source video | To MP4 |
|---|---|
| H.264, HEVC, AV1 | **copy** (`-c:v copy`, plus `-tag:v hvc1` for HEVC). Lossless and instant. |
| VP9 | copy works, but players vary. Re-encode if the user targets TVs or Apple. |
| MPEG-2, MPEG-4 ASP (DivX/Xvid), VC-1, others | re-encode |

When the user explicitly asks for encoding ("using my GPU"), encode, and offer copy as a flag (`--copy-video`) or a note: re-encoding H.264 or HEVC loses quality and gains little.

HEVC copied into MP4 gets the `hev1` tag by default *(verified)*. Apple devices and many TVs need `hvc1`: always add `-tag:v hvc1` for HEVC.

## Audio into MP4

| Codec | Do | Why |
|---|---|---|
| aac, ac3, eac3, mp3, alac | copy | native |
| flac, opus | copy | works in mpv, VLC, browsers and Jellyfin *(verified mux)*; Apple and many TVs won't play them. For Apple, use ALAC instead of FLAC. |
| dts (core) | copy | MP4 carries it *(verified)* |
| dts (DTS-HD MA) | copy, or decode to FLAC | it's lossless: never to AAC |
| truehd, mlp | FLAC (ALAC for Apple) | TrueHD in MP4 is experimental (`-strict -2`) and badly supported *(verified)* |
| pcm_* | FLAC | ffmpeg 6.1 copies PCM into MP4 *(verified)*, but few players read it; FLAC is lossless and smaller |
| vorbis, wma, others | AAC at ~96 kb/s per channel (or Opus) | lossy and unsupported in MP4 |

Never turn lossless into lossy unless the user asked for smaller files. If you do, say so.
Language tags and default/forced flags carry over automatically *(verified)*. Keep track order.

## Subtitles

- **Text** (subrip, ass, ssa, webvtt, mov_text): `-c:s mov_text`. The text and timing survive *(verified)*, but ASS styling and positioning don't. If the styling matters, also write `<stem>.<lang>.ass` next to the file.
- **Picture** (hdmv_pgs_subtitle = Blu-ray, dvd_subtitle = VobSub, dvb_subtitle): MP4 can't hold them. "Keep subtitles if possible" means extracting them, not dropping them:
  - PGS: `-map 0:N -c copy -f sup "file:$dir/$stem.$lang.sup"` *(verified)*
  - VobSub, DVB: `-map 0:N -c copy -f matroska "file:$dir/$stem.$lang.mks"` *(verified; ffmpeg has no idx/sub muxer)*
  - Name them `<stem>.<lang>[.forced].<ext>` so mpv, VLC, Jellyfin and Plex pick them up.
  - Text versions need OCR (Subtitle Edit, pgsrip). Mention it; don't do it unasked.
- If the user wants everything kept in one file, MKV is the right container. Say so in a note when MP4 forces losses.

## Other streams

- **Cover art** in MKV shows up as a video stream with `disposition.attached_pic=1`. Never encode it as video. Keep it with `-map 0:N -c:v:1 copy -disposition:v:1 attached_pic` *(verified)*, or drop it and say so.
- **Attachments** (fonts for ASS) and data streams can't go into MP4. List them as not carried over.
- **Chapters** and global metadata copy by default *(verified)*.

## HDR, 10-bit, interlacing

- Keep 10-bit sources 10-bit when encoding HEVC or AV1 (`p010` for VAAPI). H.264 hardware encoders are 8-bit only.
- HDR10: check that the output keeps `color_primaries=bt2020`, `color_transfer=smpte2084`, `color_space=bt2020nc`. If it doesn't, pass `-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc`.
- Dolby Vision: `ffprobe -show_streams` lists a "DOVI configuration record" with `dv_profile`. **Profile 5** has no HDR10 base layer: re-encoding gives green/purple colours, so copy the video. Profiles 7 and 8 re-encode to plain HDR10 and lose DV. Say so.
- Interlaced sources (`field_order` tt, bb, tb, bt): deinterlace (`deinterlace_vaapi` on GPU frames, `bwdif` on CPU frames), or the output combs.

## VAAPI on AMD (RX 6000/7000/9000, Ryzen iGPUs)

- **Pick the right GPU.** Ryzen 7000/9000 CPUs have an amdgpu iGPU too, and `card*`/`renderD*` numbers aren't stable. Take the AMD card with the most VRAM, then the render node on the same PCI device, and allow an override (`VAAPI_DEVICE=`). *(Tested against a fake sysfs with an iGPU, a dGPU, an NVIDIA card and a connector entry; `SYSFS` is there for that kind of test.)*
  ```bash
  # Prints the render node of the AMD GPU with the most VRAM: the dGPU, not the Ryzen iGPU.
  amd_render_node() {
    local sys=${SYSFS:-/sys} card vendor vram best='' best_vram=-1 dev r
    for card in "$sys"/class/drm/card*; do
      [[ ${card##*/} =~ ^card[0-9]+$ ]] || continue   # skip connectors such as card1-DP-1
      read -r vendor 2>/dev/null <"$card/device/vendor" || continue
      [[ $vendor == 0x1002 ]] || continue
      read -r vram 2>/dev/null <"$card/device/mem_info_vram_total" || vram=0
      if ((vram > best_vram)); then best=$card best_vram=$vram; fi
    done
    [[ -n $best ]] || return 1
    dev=$(readlink -f -- "$best/device")
    for r in "$sys"/class/drm/renderD*; do
      if [[ $(readlink -f -- "$r/device") == "$dev" ]]; then printf '/dev/dri/%s\n' "${r##*/}"; return 0; fi
    done
    return 1
  }
  node=${VAAPI_DEVICE:-$(amd_render_node)} || die "no AMD GPU found; set VAAPI_DEVICE=/dev/dri/renderD12x"
  ```
- **Test once before the batch**, so a missing driver fails in one second instead of on every file:
  `ffmpeg -nostdin -v error -init_hw_device vaapi=va:$node -filter_hw_device va -f lavfi -i testsrc2=d=0.5 -vf format=nv12,hwupload -c:v hevc_vaapi -f null -`
- **Pipeline**: `-init_hw_device vaapi=va:$node -filter_hw_device va -hwaccel vaapi -hwaccel_device va -hwaccel_output_format vaapi -i ... -vf 'format=nv12|vaapi,hwupload' -c:v hevc_vaapi`. `format=X|vaapi` lets GPU-decoded frames pass through and uploads CPU-decoded ones. Use `p010` instead of `nv12` for 10-bit.
- **Fallback**: if GPU decode fails (10-bit H.264, MPEG-4 ASP and others), retry the file with CPU decode and GPU encode. Exit 255 means the user hit Ctrl+C: stop the batch, don't retry.
- **Quality**: `-rc_mode CQP -global_quality N`. HEVC/H.264 use a 0–51 scale (around 22–26 for transparent-ish), AV1 uses 0–255 (around 90–110). Confirm other rate-control modes with the test encode before offering them.
- **AV1 encode** needs RDNA3 or newer (RX 7000/9000). Older cards: HEVC.
- **Packages** (Arch/CachyOS): `ffmpeg`, `libva-utils` for `vainfo`. The VAAPI driver comes with `mesa` (on older installs, `libva-mesa-driver`). Check with `vainfo --display drm --device $node`.

## Testing without the GPU

Swap only the encoder: a PATH shim named `ffmpeg` that rewrites `*_vaapi` to `libx265`/`libx264`/`libsvtav1`, drops the `-hwaccel*`, `-init_hw_device`, `-filter_hw_device`, `-rc_mode` and `-global_quality` options, strips `hwupload` and `|vaapi` from filters, then execs the real ffmpeg. Everything else (stream maps, muxing, sidecars, temp files, exit codes) runs for real. Generate inputs with `ffmpeg -f lavfi -i testsrc2=d=3 -f lavfi -i sine=d=3`, including every audio codec class above. Then check the result with `ffprobe -of json -show_streams -show_chapters`.
