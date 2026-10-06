#!/usr/bin/env python3
"""flacmeta: health checks, safe tag fixes and ReplayGain 2.0 for a FLAC library.

  flacmeta check PATH...                     read-only report
  flacmeta fix PATH... [--album-year] [--apply]
  flacmeta replaygain PATH... [--force] [--apply]
  flacmeta info FILE...
  flacmeta spectrogram FILE [-o PNG]
  flacmeta undo LOG [--apply]

Nothing is written without --apply. Every write goes: copy (a reflink on btrfs) -> edit the
copy -> check the audio frames are byte-identical -> undo record -> atomic rename.
"""
import argparse
import base64
import concurrent.futures as cf
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import threading
from collections import Counter, defaultdict

try:
    from mutagen import MutagenError
    from mutagen.flac import FLAC
except ImportError:
    sys.exit('flacmeta: needs mutagen: sudo pacman -S --needed python-mutagen')
try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
except ImportError:
    np = None

# Spectral analysis: one excerpt per file. Thresholds come from calibration on genuine 24-bit
# recordings (cliff 9-21 dB), the same recordings upsampled with soxr (63-108 dB) and MP3
# 128k/192k sources (24-57 dB, edge 16-18.8 kHz). MP3 320k, V0 and AAC 256k are not detectable.
EXCERPT_S = 60
BAND_HZ = 100
UPSAMPLE_CLIFF_DB = 40.0
LOSSY_CLIFF_DB = 25.0
LOSSY_MAX_EDGE_HZ = 20500

COVER_MIN_PX = 500
COVER_MAX_PX = 3000
COVER_MAX_BYTES = 2 << 20
COVER_FILES = ('cover.jpg', 'cover.png', 'folder.jpg', 'folder.png', 'front.jpg', 'front.png')

REQUIRED_TAGS = ('TITLE', 'ARTIST', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'DATE')
OPTIONAL_TAGS = ('TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'GENRE')
SINGLE_TAGS = {'TITLE', 'ALBUM', 'DATE', 'TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'ISRC',
               'BARCODE', 'UPC', 'LABEL', 'COPYRIGHT', 'MEDIATYPE', 'ITUNESADVISORY', 'LYRICS',
               'REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN',
               'REPLAYGAIN_ALBUM_PEAK', 'REPLAYGAIN_REFERENCE_LOUDNESS'}
TEXT_TAGS = {'LYRICS', 'UNSYNCEDLYRICS', 'COMMENT', 'DESCRIPTION'}  # multi-line: never trimmed
ALBUM_TAGS = ('ALBUM', 'ALBUMARTIST', 'DATE', 'GENRE', 'LABEL', 'BARCODE', 'COPYRIGHT', 'DISCTOTAL', 'MEDIATYPE')
SYNONYMS = {'TOTALTRACKS': 'TRACKTOTAL', 'TOTALDISCS': 'DISCTOTAL'}
RG_TAGS = ('REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK')
RSGAIN_ARGS = ['custom', '--album', '--tagmode=i', '--loudness=-18', '--clip-mode=p', '--quiet']

DATE_RE = re.compile(r'^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$')
SLASH_RE = re.compile(r'^\s*(\d+)\s*/\s*(\d+)\s*$')
ISRC_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{3}\d{7}$')
GAIN_RE = re.compile(r'^[+-]?\d+(\.\d+)? dB$')
PEAK_RE = re.compile(r'^\d+(\.\d+)?$')
DISC_DIR_RE = re.compile(r'^(disc|disk|cd)\s*\d+\b', re.I)
FILE_NUM_RE = re.compile(r'^(\d{1,3})\s*[-.]\s')
YEAR_DIR_RE = re.compile(r'\((\d{4})\)\s*$')
TMP_PREFIX = '.flacmeta-'

LEVELS = ('ERROR', 'WARN', 'INFO')
BLOCK_NAMES = {0: 'STREAMINFO', 1: 'PADDING', 2: 'APPLICATION', 3: 'SEEKTABLE', 4: 'VORBIS_COMMENT',
               5: 'CUESHEET', 6: 'PICTURE'}
STATE_DIR = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'flacmeta')

STOP = threading.Event()  # set on Ctrl+C/SIGTERM: workers finish their step, clean up, and don't replace


class BadFile(Exception):
    pass


@dataclasses.dataclass
class Finding:
    level: str
    code: str
    message: str
    path: str | None = None  # None: about the whole album
    fixable: bool = False


@dataclasses.dataclass
class Layout:
    size: int
    flac_start: int   # offset of "fLaC" (> 0 when an ID3v2 tag is glued in front)
    audio_start: int  # first audio frame
    audio_end: int    # end of audio frames (< size when ID3v1/APEv2 tags are glued at the end)
    blocks: list      # (type, data offset, length)

    def block(self, path, btype):
        for t, off, n in self.blocks:
            if t == btype:
                with open(path, 'rb') as f:
                    f.seek(off)
                    return f.read(n)
        return None


@dataclasses.dataclass
class Track:
    path: str
    layout: Layout | None = None
    tags: list = dataclasses.field(default_factory=list)  # [(key, value)] in file order, keys as stored
    rate: int = 0
    bits: int = 0
    channels: int = 0
    samples: int = 0
    md5: int = 0
    pictures: list = dataclasses.field(default_factory=list)
    seektable: bool = False
    findings: list = dataclasses.field(default_factory=list)
    eff_bits: int | None = None
    spectrum: dict | None = None

    def get(self, key):
        return [v for k, v in self.tags if k.upper() == key]

    def first(self, key):
        vals = self.get(key)
        return vals[0].strip() if vals else ''

    def add(self, level, code, message, fixable=False):
        self.findings.append(Finding(level, code, message, self.path, fixable))

    @property
    def duration(self):
        return self.samples / self.rate if self.rate else 0.0


# ---------------------------------------------------------------------------------------------
# File layout, images, small helpers


def run(cmd, **kw):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, **kw)


def tail(text, n=200):
    text = text.decode(errors='replace') if isinstance(text, bytes) else text
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return (lines[-1] if lines else 'no error output')[:n]


def read_layout(path):
    """Parse where the FLAC stream, its metadata blocks and its audio frames are."""
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(10)
        start = 0
        if head[:3] == b'ID3' and len(head) == 10:
            n = 0
            for b in head[6:10]:
                n = (n << 7) | (b & 0x7F)
            start = 10 + n + (10 if head[5] & 0x10 else 0)
        f.seek(start)
        if f.read(4) != b'fLaC':
            raise BadFile('not a FLAC file (no fLaC marker)')
        pos, blocks = start + 4, []
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                raise BadFile('metadata is truncated')
            btype, length = hdr[0] & 0x7F, int.from_bytes(hdr[1:4], 'big')
            blocks.append((btype, pos + 4, length))
            pos += 4 + length
            if pos > size:
                raise BadFile(f'{BLOCK_NAMES.get(btype, btype)} block runs past the end of the file')
            f.seek(pos)
            if hdr[0] & 0x80:
                break
        end = size
        while True:  # ID3v1 and APEv2 tags glued to the end, in either order
            if end - pos >= 128:
                f.seek(end - 128)
                if f.read(3) == b'TAG':
                    end -= 128
                    continue
            if end - pos >= 32:
                f.seek(end - 32)
                foot = f.read(32)
                if foot[:8] == b'APETAGEX':
                    total = int.from_bytes(foot[12:16], 'little')
                    total += 32 if int.from_bytes(foot[20:24], 'little') & 0x80000000 else 0
                    if 32 <= total <= end - pos:
                        end -= total
                        continue
            break
        return Layout(size, start, pos, end, blocks)


def audio_digest(path, layout):
    h = hashlib.blake2b(digest_size=20)
    with open(path, 'rb') as f:
        f.seek(layout.audio_start)
        left = layout.audio_end - layout.audio_start
        while left > 0:
            chunk = f.read(min(left, 4 << 20))
            if not chunk:
                raise BadFile('file shrank while reading')
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def image_info(data):
    """(mime, width, height, depth, colors) from the image header, or None if unreadable."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 26 and data[12:16] == b'IHDR':
        w, h, bitdepth, ctype = struct.unpack('>IIBB', data[16:26])
        colors, pos = 0, 8
        while ctype == 3 and pos + 8 <= len(data):
            ln, typ = struct.unpack('>I4s', data[pos:pos + 8])
            if typ == b'PLTE':
                colors = ln // 3
                break
            pos += 12 + ln
        return 'image/png', w, h, bitdepth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype, 1), colors
    if data[:3] == b'\xff\xd8\xff':
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                return None
            marker = data[pos + 1]
            if marker == 0xFF:
                pos += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            ln = struct.unpack('>H', data[pos + 2:pos + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if pos + 10 > len(data):
                    return None
                prec, h, w, comps = struct.unpack('>BHHB', data[pos + 4:pos + 10])
                return 'image/jpeg', w, h, prec * comps, 0
            pos += 2 + ln
        return None
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 11:
        w, h, packed = struct.unpack('<HHB', data[6:11])
        bits = (packed & 7) + 1
        return 'image/gif', w, h, bits, (1 << bits) if packed & 0x80 else 0
    return None


def picture_header(p):
    return {'type': p.type, 'mime': p.mime, 'desc': p.desc, 'width': p.width, 'height': p.height,
            'depth': p.depth, 'colors': p.colors, 'sha1': hashlib.sha1(p.data).hexdigest()}


def int_tag(value):
    value = value.strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def fmt_rate(rate):
    return f'{rate / 1000:g} kHz'


def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def album_dir(path):
    d = os.path.dirname(os.path.abspath(path))
    return os.path.dirname(d) if DISC_DIR_RE.match(os.path.basename(d)) else d


def album_files_on_disk(adir):
    """Every FLAC file of an album: the folder itself plus its Disc NN subfolders."""
    found = set()
    for d in [adir] + [os.path.join(adir, s) for s in sorted(os.listdir(adir))
                       if DISC_DIR_RE.match(s) and os.path.isdir(os.path.join(adir, s))]:
        for name in os.listdir(d):
            if name.lower().endswith('.flac') and not name.startswith(TMP_PREFIX) and os.path.isfile(os.path.join(d, name)):
                found.add(os.path.join(d, name))
    return found


def expand(paths):
    """FLAC files under the given paths, sorted, without our own temp files."""
    out, seen = [], set()
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            files = []
            for d, dirs, names in os.walk(p):
                dirs.sort()
                files += [os.path.join(d, n) for n in names if n.lower().endswith('.flac') and not n.startswith(TMP_PREFIX)]
        elif os.path.isfile(p):
            files = [p]
        else:
            sys.exit(f'flacmeta: no such file or directory: {p}')
        for f in sorted(files):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def display_root(paths):
    root = os.path.commonpath([os.path.abspath(p) for p in paths])
    return os.path.dirname(root) if os.path.isfile(root) else root


def show(path, root):
    r = os.path.relpath(path, root)
    return os.path.basename(path) if r == '.' else r


# ---------------------------------------------------------------------------------------------
# Loading and per-file checks


def load_track(path):
    t = Track(path)
    try:
        t.layout = read_layout(path)
        f = FLAC(path)
    except (BadFile, MutagenError, OSError, ValueError, struct.error) as e:
        t.add('ERROR', 'unreadable', f'cannot read FLAC metadata: {e}')
        return t
    t.tags = list(f.tags) if f.tags is not None else []
    t.rate, t.bits, t.channels = f.info.sample_rate, f.info.bits_per_sample, f.info.channels
    t.samples, t.md5 = f.info.total_samples, f.info.md5_signature
    t.pictures = f.pictures
    t.seektable = f.seektable is not None
    return t


def check_stream(t):
    lay = t.layout
    if lay.flac_start:
        t.add('WARN', 'id3-glued', f'ID3v2 tag ({fmt_size(lay.flac_start)}) glued in front of the FLAC stream', True)
    if lay.audio_end < lay.size:
        t.add('WARN', 'id3-glued', f'ID3v1/APE tag ({fmt_size(lay.size - lay.audio_end)}) glued to the end of the file', True)
    if not t.md5:
        t.add('WARN', 'md5-missing', 'STREAMINFO has no MD5 signature, so decoding cannot be verified '
                                     '(re-encode with flac to add one)')
    if not t.seektable:
        t.add('INFO', 'no-seektable', 'no SEEKTABLE (slow seeking in some players)', bool(shutil.which('metaflac')))
    if not t.samples:
        t.add('WARN', 'no-length', 'STREAMINFO does not give the number of samples')


def check_tags(t):
    seen = Counter((k.upper(), v) for k, v in t.tags)
    for (key, value), n in sorted(seen.items()):
        if n > 1:
            t.add('WARN', 'tag-duplicate', f'{key} has the same value {n} times', True)
    keys = Counter(k.upper() for k, _ in t.tags)
    for k, v in t.tags:
        key = k.upper()
        if not v.strip():
            t.add('WARN', 'tag-empty', f'{key} is empty', True)
        elif v != v.strip() and key not in TEXT_TAGS:
            t.add('INFO', 'tag-whitespace', f'{key} has leading/trailing whitespace: {v!r}', True)
    for key in REQUIRED_TAGS:
        if not t.first(key):
            t.add('WARN', 'tag-missing', f'{key} is missing')
    for key in OPTIONAL_TAGS:
        number = {'TRACKTOTAL': 'TRACKNUMBER', 'DISCTOTAL': 'DISCNUMBER'}.get(key)
        if t.first(key) or (number and SLASH_RE.match(t.first(number))):
            continue  # a "3/12" number is reported (and fixed) as malformed instead
        synonym = any(keys[s] for s, canon in SYNONYMS.items() if canon == key)
        t.add('INFO', 'tag-missing-optional', f'{key} is missing', synonym)
    for key in sorted(keys):
        if key in SINGLE_TAGS and len({v.strip() for k, v in t.tags if k.upper() == key and v.strip()}) > 1:
            t.add('WARN', 'tag-multiple', f'{key} has several different values: ' +
                  ', '.join(repr(v) for v in t.get(key))[:150])
        if key in SYNONYMS:
            t.add('INFO', 'tag-synonym', f'{key} should be {SYNONYMS[key]}', True)
    date = t.first('DATE')
    if date and not valid_date(date):
        t.add('WARN', 'tag-malformed', f'DATE {date!r} is not YYYY, YYYY-MM or YYYY-MM-DD')
    for num, total in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        v = t.first(num)
        if SLASH_RE.match(v):
            t.add('INFO', 'tag-malformed', f'{num} {v!r} holds the total too (should be split into {total})', True)
        elif v and int_tag(v) is None:
            t.add('WARN', 'tag-malformed', f'{num} {v!r} is not a positive number')
        tv = t.first(total)
        if tv and int_tag(tv) is None:
            t.add('WARN', 'tag-malformed', f'{total} {tv!r} is not a positive number')
        elif tv and int_tag(v) and int_tag(v) > int_tag(tv):
            t.add('WARN', 'tag-malformed', f'{num} {v} is larger than {total} {tv}')
    isrc = t.first('ISRC')
    if isrc and not ISRC_RE.match(isrc.replace('-', '').upper()):
        t.add('WARN', 'tag-malformed', f'ISRC {isrc!r} is not a valid ISRC')
    barcode = t.first('BARCODE')
    if barcode and not (barcode.isdigit() and len(barcode) in (8, 12, 13, 14)):
        t.add('INFO', 'tag-malformed', f'BARCODE {barcode!r} is not an 8/12/13/14-digit UPC/EAN')
    check_replaygain(t)
    check_filename(t)


def valid_date(v):
    m = DATE_RE.match(v)
    if not m:
        return False
    y, mo, d = int(m[1]), int(m[2] or 1), int(m[3] or 1)
    try:
        dt.date(y, mo, d)
    except ValueError:
        return False
    return 1000 <= y <= 2100


def check_replaygain(t):
    have = {k: t.first(k) for k in RG_TAGS if t.first(k)}
    for k, v in have.items():
        if not (GAIN_RE if k.endswith('GAIN') else PEAK_RE).match(v):
            t.add('WARN', 'replaygain-malformed', f'{k} {v!r} is malformed')
    if not have:
        t.add('INFO', 'replaygain-missing', 'no ReplayGain tags')
    elif len(have) < len(RG_TAGS):
        missing = ', '.join(k.removeprefix('REPLAYGAIN_') for k in RG_TAGS if k not in have)
        t.add('WARN', 'replaygain-partial', f'ReplayGain incomplete: missing {missing}')


def check_filename(t):
    m = FILE_NUM_RE.match(os.path.basename(t.path))
    track, disc = int_tag(t.first('TRACKNUMBER').split('/')[0]), int_tag(t.first('DISCNUMBER').split('/')[0])
    if not m or not track:
        return
    n = int(m[1])
    if n >= 100 and disc and len(m[1]) == 3:
        if (n // 100, n % 100) != (disc, track):
            t.add('WARN', 'filename-mismatch', f'file name says disc {n // 100} track {n % 100}, tags say disc {disc} track {track}')
    elif n != track:
        t.add('WARN', 'filename-mismatch', f'file name says track {n}, TRACKNUMBER says {track}')


def check_pictures(t):
    if not t.pictures:
        t.add('WARN', 'cover-missing', 'no embedded cover')
        return
    fronts = [p for p in t.pictures if p.type == 3]
    if not fronts:
        t.add('INFO', 'cover-missing', 'embedded picture(s) not marked as front cover')
    elif len(fronts) > 1:
        t.add('INFO', 'cover-duplicate', f'{len(fronts)} embedded front covers')
    for i, p in enumerate(t.pictures, 1):
        if p.mime == '-->':
            continue  # picture given by URL
        info = image_info(p.data)
        if info is None:
            t.add('ERROR', 'cover-bad', f'picture #{i} is not a readable JPEG, PNG or GIF ({fmt_size(len(p.data))})')
            continue
        mime, w, h, depth, colors = info
        if p.mime.lower() != mime:
            t.add('WARN', 'cover-mime', f'picture #{i} is {mime} but its MIME type says {p.mime!r}', True)
        if (p.width, p.height, p.depth, p.colors) != (w, h, depth, colors):
            t.add('INFO', 'cover-header', f'picture #{i} header says {p.width}x{p.height}, image is {w}x{h}', True)
        if p.type == 3 and min(w, h) < COVER_MIN_PX:
            t.add('WARN', 'cover-small', f'front cover is only {w}x{h} (< {COVER_MIN_PX} px)')
        if p.type == 3 and (max(w, h) > COVER_MAX_PX or len(p.data) > COVER_MAX_BYTES):
            t.add('INFO', 'cover-big', f'front cover is {w}x{h}, {fmt_size(len(p.data))} (some players struggle '
                                       f'above {COVER_MAX_PX} px or {fmt_size(COVER_MAX_BYTES)})')


# ---------------------------------------------------------------------------------------------
# Audio checks: full decode with MD5 check, effective bit depth, spectrum


def flac_error(stderr):
    """'lost sync x3; ERROR while decoding data' from flac -t's stderr."""
    text = stderr.decode(errors='replace')
    codes = Counter(m.lower().replace('_', ' ') for m in re.findall(r'ERROR_STATUS_(\w+)', text))
    parts = [c + (f' x{n}' if n > 1 else '') for c, n in codes.items()]
    parts += [l.split(': ', 1)[-1].strip() for l in text.splitlines() if 'ERROR' in l and 'error code' not in l]
    return '; '.join(parts) or tail(text)


def check_decode(t):
    if shutil.which('flac'):
        lay = t.layout
        if lay.flac_start or lay.audio_end < lay.size:
            # flac -t loses sync on tags glued to the end: test only the FLAC stream itself
            with open(t.path, 'rb') as f:
                f.seek(lay.flac_start)
                data = f.read(lay.audio_end - lay.flac_start)
            r = subprocess.run(['flac', '-t', '-s', '-'], input=data, capture_output=True)
        else:
            r = run(['flac', '-t', '-s', t.path])
        if r.returncode:
            code = 'md5-mismatch' if b'MD5' in r.stderr else 'decode-error'
            t.add('ERROR', code, f'flac -t failed: {flac_error(r.stderr)}')
    else:
        r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-i', 'file:' + t.path, '-map', '0:a:0', '-f', 'null', '-'])
        if r.returncode or r.stderr.strip():
            t.add('ERROR', 'decode-error', f'ffmpeg decode failed: {tail(r.stderr)}')


def decode_excerpt(t):
    """60 s of PCM as signed 32-bit little-endian, from ~30% into the track."""
    start = max(0.0, t.duration * 0.3 - EXCERPT_S / 2) if t.duration > EXCERPT_S * 1.5 else 0.0
    r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-ss', f'{start:.2f}', '-t', str(EXCERPT_S),
             '-i', 'file:' + t.path, '-map', '0:a:0', '-c:a', 'pcm_s32le', '-f', 's32le', 'pipe:1'])
    if r.returncode or not r.stdout:
        raise BadFile(f'ffmpeg could not decode an excerpt: {tail(r.stderr)}')
    return r.stdout


def effective_bits(raw):
    """Bits in use: 32 minus the low bits that are zero in every sample (None for digital silence)."""
    for k in range(4):  # byte k of each little-endian sample, least significant first
        lane = raw[k::4]
        if lane.count(0) != len(lane):
            low = 0
            for v in set(lane):
                low |= v
            return 32 - (8 * k + (low & -low).bit_length() - 1)
    return None


def band_levels(raw, rate, channels):
    """Average level per 100 Hz band in dB (0 dB = full-scale sine), Welch method, Kaiser(20) window."""
    x = np.frombuffer(raw, '<i4')
    x = x[: len(x) - len(x) % channels].reshape(-1, channels)
    n = 1 << (13 + max(0, round(np.log2(rate / 48000))))  # ~5-6 Hz bins at every rate
    if len(x) < 4 * n:
        return None
    w = np.kaiser(n, 20.0)
    acc, count = np.zeros(n // 2 + 1), 0
    for c in range(channels):
        frames = sliding_window_view(x[:, c], n)[:: n // 2]
        for i in range(0, len(frames), 64):
            s = np.fft.rfft(frames[i:i + 64] / 2.0**31 * w, axis=1)
            acc += (s.real**2 + s.imag**2).sum(axis=0)
            count += len(s)
    power = acc / count * 4 / w.sum()**2
    idx = (np.fft.rfftfreq(n, 1 / rate) // BAND_HZ).astype(int)
    nb = int(rate / 2 // BAND_HZ)
    keep = idx < nb
    band = np.bincount(idx[keep], weights=power[keep], minlength=nb) / np.maximum(np.bincount(idx[keep], minlength=nb), 1)
    return 10 * np.log10(band + 1e-30)


def find_cliff(levels, rate):
    """The sharpest drop above 12 kHz after which nothing comes back: (edge Hz, drop dB)."""
    top = int((rate / 2 - 500) // BAND_HZ)
    best = (0, -1e9, 0.0, 0.0)
    for i in range(12000 // BAND_HZ, top - 3):
        below = float(levels[i - 8:i - 2].mean())
        above = float(np.percentile(levels[i + 2:top], 90))
        if below - above > best[1]:
            best = (i, below - above, below, above)
    i, drop, below, above = best
    if i == 0:
        return None
    mid, j = (below + above) / 2, top - 1
    while j > i - 10 and levels[j] <= mid:
        j -= 1
    return (j + 1) * BAND_HZ, drop


def check_audio(t, spectral=True):
    try:
        raw = decode_excerpt(t)
    except BadFile as e:
        t.add('WARN', 'analysis-failed', str(e))
        return
    t.eff_bits = effective_bits(raw)
    if t.eff_bits is not None and t.eff_bits < t.bits:
        if t.bits > 16 >= t.eff_bits:
            t.add('WARN', 'padded-bit-depth', f'{t.bits}-bit file holds {t.eff_bits}-bit audio (the low bits are always zero)')
        else:
            t.add('INFO', 'unused-bits', f'{t.bits}-bit file uses only {t.eff_bits} bits')
    if not (spectral and np is not None):
        return
    levels = band_levels(raw, t.rate, t.channels)
    cliff = find_cliff(levels, t.rate) if levels is not None else None
    if cliff is None:
        return
    edge, drop = cliff
    t.spectrum = {'edge_hz': edge, 'cliff_db': round(drop, 1)}
    if t.rate > 48000 and drop >= UPSAMPLE_CLIFF_DB:
        src = None
        if edge <= 24000:
            src = '44.1 kHz' if edge <= 22050 else '48 kHz'
        elif t.rate >= 176400 and edge <= 48000:
            src = '88.2 kHz' if edge <= 44100 else '96 kHz'
        if src:
            lossy = ', and the source may be lossy' if edge <= LOSSY_MAX_EDGE_HZ else ''
            t.add('WARN', 'upsampled', f'probably upsampled from {src}: nothing above {edge / 1000:.1f} kHz '
                                       f'({drop:.0f} dB cliff){lossy}')
            return
    if edge <= LOSSY_MAX_EDGE_HZ and drop >= LOSSY_CLIFF_DB:
        t.add('WARN', 'lossy-source', f'possible lossy source: sharp cutoff at {edge / 1000:.1f} kHz ({drop:.0f} dB cliff); '
                                      'check with flacmeta spectrogram')


# ---------------------------------------------------------------------------------------------
# Albums


@dataclasses.dataclass
class Album:
    dir: str
    tracks: list
    complete: bool
    findings: list = dataclasses.field(default_factory=list)

    def add(self, level, code, message, fixable=False):
        self.findings.append(Finding(level, code, message, None, fixable))

    def consensus(self, key, tracks=None):
        """The values every track that has `key` agrees on (a tuple), or None."""
        vals = {album_value(t, key) for t in (tracks or self.tracks) if t.first(key)}
        return vals.pop() if len(vals) == 1 else None

    def consensus_int(self, key, tracks=None):
        vals = self.consensus(key, tracks)
        return int_tag(vals[0]) or 0 if vals and len(vals) == 1 else 0


def album_value(t, key):
    """A track's values for `key`, stripped and without repeats (those are reported per track)."""
    return tuple(dict.fromkeys(v.strip() for v in t.get(key) if v.strip()))


def disc_of(t):
    d = int_tag(t.first('DISCNUMBER').split('/')[0])
    if d:
        return d
    m = re.search(r'\d+', os.path.basename(os.path.dirname(t.path))) if DISC_DIR_RE.match(os.path.basename(os.path.dirname(t.path))) else None
    return int(m[0]) if m else 1


def group_albums(tracks):
    by_dir = defaultdict(list)
    for t in tracks:
        by_dir[album_dir(t.path)].append(t)
    albums = []
    for d in sorted(by_dir):
        scanned = {t.path for t in by_dir[d]}
        albums.append(Album(d, by_dir[d], album_files_on_disk(d) <= scanned))
    return albums


def check_album(a):
    tracks = [t for t in a.tracks if t.layout]
    if not tracks:
        return
    for key in ALBUM_TAGS:
        vals = Counter('; '.join(album_value(t, key)) for t in tracks)
        if len(vals) > 1:
            fill = '' in vals and len(vals) == 2
            parts = ', '.join(f'{v!r} x{n}' if v else f'missing x{n}' for v, n in vals.most_common(4))
            a.add('WARN', 'album-inconsistent', f'{key} differs between tracks: {parts}', fill)
    for key in ('REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK'):
        vals = {t.first(key) for t in tracks if t.first(key)}
        if len(vals) > 1:
            a.add('WARN', 'replaygain-album-mismatch', f'{key} differs between tracks; rerun: flacmeta replaygain --force')
    formats = Counter(f'{t.bits}/{fmt_rate(t.rate)}' for t in tracks)
    if len(formats) > 1:
        a.add('INFO', 'album-mixed-format', 'tracks have different formats: ' + ', '.join(f'{f} x{n}' for f, n in formats.items()))
    m = YEAR_DIR_RE.search(os.path.basename(a.dir))
    date = a.consensus('DATE')
    if m and date and date[0][:4] != m[1]:
        a.add('INFO', 'folder-year-mismatch', f'folder says {m[1]}, DATE says {date[0]}')
    if not any(os.path.isfile(os.path.join(a.dir, n)) for n in COVER_FILES):
        a.add('INFO', 'cover-file-missing', 'no cover.jpg (or folder.jpg/front.jpg) in the album folder')
    if not a.complete:
        a.add('INFO', 'album-partial-scan', 'only part of this album was scanned: track numbering not checked')
        return
    discs, numbers = defaultdict(list), {}
    for t in tracks:
        discs[disc_of(t)].append(t)
    for d, dts in discs.items():
        numbers[d] = Counter(int_tag(t.first('TRACKNUMBER').split('/')[0]) for t in dts)
        numbers[d].pop(None, None)
    present = sum(len(n) for n in numbers.values())
    # Qobuz writes the whole album's count into TRACKTOTAL on every disc; other taggers write the
    # disc's count. A total larger than every disc's highest track number is taken as album-wide.
    album_total = a.consensus_int('TRACKTOTAL')
    whole_album = len(discs) > 1 and album_total and all(album_total > max(n, default=0) for n in numbers.values())
    for d, nums in sorted(numbers.items()):
        for n, c in sorted(nums.items()):
            if c > 1:
                a.add('WARN', 'track-duplicate', f'disc {d} track {n} appears {c} times')
        total = 0 if whole_album else a.consensus_int('TRACKTOTAL', discs[d])
        expected = max([total] + list(nums))
        missing = sorted(set(range(1, expected + 1)) - set(nums))
        if missing:
            label = f'disc {d}: ' if len(discs) > 1 or d != 1 else ''
            a.add('WARN', 'album-incomplete', f'{label}missing track(s) {", ".join(map(str, missing[:20]))}'
                  f'{"..." if len(missing) > 20 else ""} ({len(nums)} of {expected} present)')
    if whole_album and present < album_total:
        a.add('WARN', 'album-incomplete', f'{present} of {album_total} tracks present (TRACKTOTAL)')
    disc_total = a.consensus_int('DISCTOTAL')
    missing = sorted(set(range(1, max([disc_total] + list(discs)) + 1)) - set(discs))
    if missing:
        a.add('WARN', 'album-incomplete', f'missing disc(s) {", ".join(map(str, missing))} of {max(disc_total, max(discs))}')


# ---------------------------------------------------------------------------------------------
# check


def analyse(path, opts):
    t = load_track(path)
    if not t.layout:
        return t
    try:
        check_stream(t)
        check_tags(t)
        check_pictures(t)
        if not opts.quick:
            check_decode(t)
            check_audio(t, spectral=not opts.no_spectrum)
    except Exception as e:  # one odd file must not end a library-wide scan
        t.add('ERROR', 'check-failed', f'flacmeta could not finish checking this file: {type(e).__name__}: {e}')
    return t


def scan(paths, opts, label='Scanning'):
    files = expand(paths)
    if not files:
        sys.exit('flacmeta: no .flac files found')
    tracks, tty = [], sys.stderr.isatty()
    ex = cf.ThreadPoolExecutor(opts.jobs)
    try:
        futs = [ex.submit(analyse, p, opts) for p in files]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            tracks.append(fut.result())
            if tty and (i % 10 == 0 or i == len(files)):
                print(f'\r{label} {i}/{len(files)} files', end='', file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()
    if tty:
        print('\r' + ' ' * 40 + '\r', end='', file=sys.stderr)
    tracks.sort(key=lambda t: t.path)
    albums = group_albums(tracks)
    for a in albums:
        check_album(a)
    return albums


def collapse(findings, ntracks):
    """Print one line for a finding that every track of the album has."""
    groups = defaultdict(list)
    for f in findings:
        groups[(f.level, f.code, f.message, f.fixable)].append(f)
    out = []
    for (level, code, message, fixable), fs in groups.items():
        if ntracks > 2 and len(fs) == ntracks and fs[0].path:
            out.append(Finding(level, code, message, '*', fixable))
        else:
            out.extend(fs)
    return out


def cmd_check(opts):
    if not opts.quick:
        if np is None and not opts.no_spectrum:
            print('note: python-numpy is not installed, so lossy/upsampling checks are skipped '
                  '(sudo pacman -S --needed python-numpy)', file=sys.stderr)
        if not shutil.which('flac'):
            print('note: flac is not installed: decoding is checked with ffmpeg, without the MD5 check', file=sys.stderr)
    albums = scan(opts.paths, opts)
    root = display_root(opts.paths)
    show_levels = LEVELS if opts.verbose else LEVELS[:2]
    totals, fixable, ntracks = Counter(), 0, 0
    for a in albums:
        ntracks += len(a.tracks)
        allf = a.findings + [f for t in a.tracks for f in t.findings]
        for f in allf:
            totals[(f.level, f.code)] += 1
            fixable += f.fixable
        if opts.json:
            for t in a.tracks:
                print(json.dumps({'type': 'track', 'path': t.path, 'album': a.dir, 'rate': t.rate, 'bits': t.bits,
                                  'effective_bits': t.eff_bits, **(t.spectrum or {}),
                                  'findings': [dataclasses.asdict(f) for f in t.findings]}))
            for f in a.findings:
                print(json.dumps({'type': 'album', 'album': a.dir, **dataclasses.asdict(f)}))
            continue
        shown = [f for f in collapse(allf, len(a.tracks)) if f.level in show_levels]
        if not shown:
            continue
        fmts = sorted({f'{t.bits}/{fmt_rate(t.rate)}' for t in a.tracks if t.rate})
        print(f'{show(a.dir, root)}  [{", ".join([f"{len(a.tracks)} tracks"] + fmts)}]')
        order = {lv: i for i, lv in enumerate(LEVELS)}
        for f in sorted(shown, key=lambda f: (order[f.level], f.path or '', f.code)):
            where = 'album' if f.path is None else 'all tracks' if f.path == '*' else os.path.relpath(f.path, a.dir)
            print(f'  {f.level:<5} {where}: {f.message}{"  [fixable]" if f.fixable else ""}')
        print()
    if opts.json:
        return 1 if any(lv == 'ERROR' for lv, _ in totals) else 0
    print(f'Checked {ntracks} files in {len(albums)} albums.')
    if totals:
        for (level, code), n in sorted(totals.items(), key=lambda kv: (LEVELS.index(kv[0][0]), kv[0][1])):
            print(f'  {level:<5} {code:<26} {n}')
        if fixable:
            print(f'{fixable} findings can be fixed: flacmeta fix PATH   (preview), then add --apply')
        if not opts.verbose and any(lv == 'INFO' for lv, _ in totals):
            print('INFO findings are counted but not listed: add -v to list them.')
    else:
        print('No problems found.')
    return 1 if any(lv == 'ERROR' for lv, _ in totals) else 0


# ---------------------------------------------------------------------------------------------
# Safe rewrite: copy -> edit copy -> verify audio -> undo record -> replace


class UndoLog:
    def __init__(self, action):
        self.action, self.path, self.fh = action, None, None
        self.lock = threading.Lock()

    def write(self, record):
        with self.lock:
            if self.fh is None:
                os.makedirs(STATE_DIR, exist_ok=True)
                stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                self.path = os.path.join(STATE_DIR, f'{self.action}-{stamp}-{os.getpid()}.jsonl')
                self.fh = open(self.path, 'a', encoding='utf-8')
            self.fh.write(json.dumps({'v': 1, 'time': dt.datetime.now().isoformat(timespec='seconds'),
                                      'action': self.action, **record}) + '\n')
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        if self.fh:
            self.fh.close()


def write_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(os.path.join(STATE_DIR, 'lock'), 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('flacmeta: another flacmeta run is writing files; wait for it to finish')
    return fh


def tmp_name(path):
    d, name = os.path.split(path)
    return os.path.join(d, f'{TMP_PREFIX}{secrets.token_hex(4)}-{name}')


def make_copy(path, layout, lo=0, hi=None, prefix=b'', suffix=b''):
    """Copy path[lo:hi] (with optional bytes around it) to a hidden temp file next to it."""
    tmp = tmp_name(path)
    hi = layout.size if hi is None else hi
    if lo == 0 and hi == layout.size and not prefix and not suffix:
        r = run(['cp', '--reflink=auto', '--', path, tmp])
        if r.returncode:
            raise BadFile(f'cp failed: {tail(r.stderr)}')
    else:
        with open(path, 'rb') as src, open(tmp, 'xb') as dst:
            dst.write(prefix)
            src.seek(lo)
            left = hi - lo
            while left > 0:
                chunk = src.read(min(left, 4 << 20))
                if not chunk:
                    raise BadFile('file shrank while copying')
                dst.write(chunk)
                left -= len(chunk)
            dst.write(suffix)
    shutil.copymode(path, tmp)
    return tmp


def verify_copy(path, layout, digest, tmp):
    """The copy must hold the same STREAMINFO and byte-identical audio frames."""
    new = read_layout(tmp)
    if new.block(tmp, 0) != layout.block(path, 0):
        raise BadFile('STREAMINFO changed in the copy')
    if audio_digest(tmp, new) != digest:
        raise BadFile('audio frames differ in the copy')
    return new


def replace(path, tmp, stat0):
    st = os.stat(path)
    if (st.st_size, st.st_mtime_ns) != stat0:
        raise BadFile('file changed while flacmeta was working on it; run again')
    if STOP.is_set():
        raise KeyboardInterrupt
    os.replace(tmp, path)
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_state(path, layout=None):
    layout = layout or read_layout(path)
    f = FLAC(path)
    with open(path, 'rb') as fh:
        prefix = fh.read(layout.flac_start)
        fh.seek(layout.audio_end)
        suffix = fh.read()
    return {'tags': [list(kv) for kv in (f.tags or [])], 'pictures': [picture_header(p) for p in f.pictures],
            'seektable': f.seektable is not None,
            'prefix': base64.b64encode(prefix).decode(), 'suffix': base64.b64encode(suffix).decode()}


def rewrite(path, target, log, note=None):
    """Bring the file to `target` (tags, picture headers, seektable, glued bytes) the safe way."""
    st = os.stat(path)
    stat0 = (st.st_size, st.st_mtime_ns)
    layout = read_layout(path)
    before = file_state(path, layout)
    digest = audio_digest(path, layout)
    if target['prefix'] is None:
        target = dict(target, prefix=before['prefix'], suffix=before['suffix'])
    prefix, suffix = base64.b64decode(target['prefix']), base64.b64decode(target['suffix'])
    tmp = make_copy(path, layout, layout.flac_start, layout.audio_end, prefix, suffix) \
        if (target['prefix'], target['suffix']) != (before['prefix'], before['suffix']) else make_copy(path, layout)
    try:
        f = FLAC(tmp)
        if f.tags is None:
            f.add_tags()
        if [list(kv) for kv in f.tags] != target['tags']:
            del f.tags[:]
            f.tags.extend(tuple(kv) for kv in target['tags'])
        by_sha = {h['sha1']: h for h in target['pictures']}
        for p in f.pictures:
            h = by_sha.get(hashlib.sha1(p.data).hexdigest())
            if h:
                p.type, p.mime, p.desc = h['type'], h['mime'], h['desc']
                p.width, p.height, p.depth, p.colors = h['width'], h['height'], h['depth'], h['colors']
        f.save(deleteid3=False)
        if target['seektable'] != before['seektable']:
            cmd = ['metaflac', '--add-seekpoint=10s', tmp] if target['seektable'] else \
                ['metaflac', '--remove', '--block-type=SEEKTABLE', tmp]
            r = run(cmd)
            if r.returncode:
                raise BadFile(f'metaflac failed: {tail(r.stderr)}')
        verify_copy(path, layout, digest, tmp)
        after = file_state(tmp)
        for key in ('tags', 'pictures', 'seektable', 'prefix', 'suffix'):
            if after[key] != target[key]:
                raise BadFile(f'the copy did not come out as planned ({key})')
        if STOP.is_set():
            raise KeyboardInterrupt
        log.write({'path': path, 'note': note, 'before': before, 'after': after})
        replace(path, tmp, stat0)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_writes(jobs, items, work):
    """Run work(item) in parallel; Ctrl+C/SIGTERM lets running items clean up, then stops."""
    done, failed = 0, []
    ex = cf.ThreadPoolExecutor(jobs)
    futs = {ex.submit(work, it): it for it in items}
    try:
        for fut in cf.as_completed(futs):
            label = futs[fut][0]
            try:
                fut.result()
                done += 1
            except KeyboardInterrupt:
                pass
            except (BadFile, OSError, ValueError, subprocess.SubprocessError) as e:
                failed.append(label)
                print(f'FAILED {label}: {e}', file=sys.stderr)
    except KeyboardInterrupt:
        STOP.set()
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    ex.shutdown()
    return done, failed


# ---------------------------------------------------------------------------------------------
# fix


def plan_fix(t, album, album_year):
    """(new tags, picture header changes, strip glued tags, add seektable, change descriptions)."""
    changes, tags = [], []
    seen = set()
    for k, v in t.tags:
        key = k.upper()
        if key in SYNONYMS:
            canon = SYNONYMS[key]
            if not t.first(canon) and v.strip():
                changes.append(f'{key}={v.strip()!r} -> {canon}')
                k, key = canon, canon
            else:
                changes.append(f'remove {key}={v!r} ({canon} is set)')
                continue
        nv = v if key in TEXT_TAGS else v.strip()
        if not nv.strip():
            changes.append(f'remove empty {key}')
            continue
        if nv != v:
            changes.append(f'{key}: trim whitespace {v!r} -> {nv!r}')
        if (key, nv) in seen:
            changes.append(f'remove duplicate {key}={nv!r}')
            continue
        seen.add((key, nv))
        tags.append([k, nv])
    for num, total in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        for i, (k, v) in enumerate(tags):
            m = SLASH_RE.match(v) if k.upper() == num else None
            if m:
                tags[i] = [k, m[1]]
                changes.append(f'{num} {v!r} -> {m[1]!r}')
                if not any(kk.upper() == total for kk, _ in tags):
                    tags.append([total, m[2]])
                    changes.append(f'add {total}={m[2]!r}')
    for key in ALBUM_TAGS:
        vals = album.consensus(key)
        if vals and not any(k.upper() == key for k, _ in tags):
            tags += [[key, v] for v in vals]
            changes.append(f'add {key}={"; ".join(vals)!r} (as on the rest of the album)')
    if album_year:
        dates = album.consensus('DATE') or (t.first('DATE'),)
        year = dates[0][:4] if DATE_RE.match(dates[0]) else None
        for i, (k, v) in enumerate(tags):
            if k.upper() != 'ALBUM':
                continue
            if not year:
                changes.append('(--album-year: skipped, no usable DATE agreed on by the album)')
            elif not v.rstrip().endswith(f'({year})'):
                tags[i] = [k, f'{v.rstrip()} ({year})']
                changes.append(f'ALBUM {v!r} -> {tags[i][1]!r}')
    pictures = []
    for i, p in enumerate(t.pictures, 1):
        h = picture_header(p)
        info = image_info(p.data) if p.mime != '-->' else None
        if info:
            mime, w, hh, depth, colors = info
            if p.mime.lower() != mime:
                changes.append(f'picture #{i}: MIME {p.mime!r} -> {mime!r}')
                h['mime'] = mime
            if (p.width, p.height, p.depth, p.colors) != (w, hh, depth, colors):
                changes.append(f'picture #{i}: header {p.width}x{p.height} -> {w}x{hh}')
                h.update(width=w, height=hh, depth=depth, colors=colors)
        pictures.append(h)
    strip = t.layout.flac_start > 0 or t.layout.audio_end < t.layout.size
    if strip:
        changes.append(f'strip glued ID3/APE tags ({fmt_size(t.layout.flac_start + t.layout.size - t.layout.audio_end)})')
    seek = not t.seektable and bool(shutil.which('metaflac')) and t.samples > 0
    if seek:
        changes.append('add SEEKTABLE (a point every 10 s)')
    if all(c.startswith('(') for c in changes):
        return None, changes
    # prefix/suffix None: keep whatever bytes are glued on now
    return {'tags': tags, 'pictures': pictures, 'seektable': t.seektable or seek,
            'prefix': '' if strip else None, 'suffix': '' if strip else None}, changes


def cmd_fix(opts):
    opts.quick, opts.no_spectrum = True, True
    albums = scan(opts.paths, opts, 'Reading')
    root = display_root(opts.paths)
    plans = []
    for a in albums:
        for t in a.tracks:
            if not t.layout:
                print(f'skip {show(t.path, root)}: unreadable', file=sys.stderr)
                continue
            target, changes = plan_fix(t, a, opts.album_year)
            if changes:
                print(show(t.path, root))
                for c in changes:
                    print(f'    {c}')
            if target:
                plans.append((show(t.path, root), t, target))
    if not plans:
        print('Nothing to fix.')
        return 0
    if not opts.apply:
        print(f'\n{len(plans)} files would change. Run again with --apply to write them.')
        return 0
    lock = write_lock()
    log = UndoLog('fix')
    try:
        done, failed = run_writes(opts.jobs, plans, lambda it: rewrite(it[1].path, it[2], log))
    finally:
        log.close()
        lock.close()
    print(f'\nChanged {done} files{f", {len(failed)} failed" if failed else ""}.')
    if log.path:
        print(f'Undo log: {log.path}\n  preview undo: flacmeta undo {log.path}')
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# replaygain


def rg_done(album):
    for t in album.tracks:
        if not all(t.first(k) and (GAIN_RE if k.endswith('GAIN') else PEAK_RE).match(t.first(k)) for k in RG_TAGS):
            return False
    return all(len({t.first(k) for t in album.tracks}) == 1 for k in RG_TAGS[2:])


def non_rg(tags):
    return Counter((k.upper(), v) for k, v in tags if not k.upper().startswith('REPLAYGAIN_'))


def replaygain_album(album, log):
    """rsgain on copies of every track of the album at once (one album gain), then replace."""
    tracks = sorted(album.tracks, key=lambda t: t.path)
    prep = []
    for t in tracks:
        st = os.stat(t.path)
        layout = read_layout(t.path)
        prep.append((t, (st.st_size, st.st_mtime_ns), layout, audio_digest(t.path, layout), file_state(t.path, layout)))
    tmps = []
    try:
        for t, _, layout, _, _ in prep:
            tmps.append(make_copy(t.path, layout))
        r = run(['rsgain'] + RSGAIN_ARGS + ['--'] + tmps)
        if r.returncode:
            raise BadFile(f'rsgain failed: {tail(r.stderr or r.stdout)}')
        afters = []
        for (t, _, layout, digest, before), tmp in zip(prep, tmps):
            verify_copy(t.path, layout, digest, tmp)
            after = file_state(tmp)
            if non_rg(after['tags']) != non_rg(before['tags']) or after['pictures'] != before['pictures']:
                raise BadFile(f'rsgain changed more than ReplayGain tags in {os.path.basename(t.path)}')
            if not all(any(k.upper() == rg for k, _ in after['tags']) for rg in RG_TAGS):
                raise BadFile(f'rsgain did not write all ReplayGain tags to {os.path.basename(t.path)}')
            afters.append(after)
        if STOP.is_set():
            raise KeyboardInterrupt
        for (t, stat0, _, _, before), tmp, after in zip(prep, tmps, afters):
            log.write({'path': t.path, 'note': 'replaygain', 'before': before, 'after': after})
            replace(t.path, tmp, stat0)
    finally:
        for tmp in tmps:
            if os.path.exists(tmp):
                os.unlink(tmp)


def cmd_replaygain(opts):
    if not shutil.which('rsgain'):
        sys.exit('flacmeta: needs rsgain (chaotic-aur): sudo pacman -S --needed rsgain')
    opts.quick, opts.no_spectrum = True, True
    albums = scan(opts.paths, opts, 'Reading')
    root = display_root(opts.paths)
    todo, skipped = [], 0
    for a in albums:
        if any(not t.layout for t in a.tracks):
            print(f'skip {show(a.dir, root)}: has unreadable files', file=sys.stderr)
        elif not a.complete:
            print(f'skip {show(a.dir, root)}: only part of the album was given (album gain needs every track)', file=sys.stderr)
        elif rg_done(a) and not opts.force:
            skipped += 1
        else:
            discs = len({disc_of(t) for t in a.tracks})
            print(f'scan  {show(a.dir, root)}  ({len(a.tracks)} tracks{f", {discs} discs" if discs > 1 else ""})')
            todo.append((show(a.dir, root), a))
    if skipped:
        print(f'skip  {skipped} album(s) that already have ReplayGain tags (--force rescans them)')
    if not todo:
        return 0
    if not opts.apply:
        print(f'\n{len(todo)} albums would be scanned (rsgain, ReplayGain 2.0, -18 LUFS). Run again with --apply.')
        return 0
    lock = write_lock()
    log = UndoLog('replaygain')
    try:
        done, failed = run_writes(max(1, opts.jobs // 2), todo, lambda it: replaygain_album(it[1], log))
    finally:
        log.close()
        lock.close()
    print(f'\nTagged {done} album(s){f", {len(failed)} failed" if failed else ""}.')
    if log.path:
        print(f'Undo log: {log.path}')
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# undo


def cmd_undo(opts):
    try:
        with open(opts.log, encoding='utf-8') as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f'flacmeta: cannot read undo log: {e}')
    latest = {}
    for r in records:  # one file may appear once; keep the first "before" and the last "after"
        latest.setdefault(r['path'], {'before': r['before']})['after'] = r['after']
    plans = []
    for path, r in latest.items():
        if not os.path.isfile(path):
            print(f'skip {path}: file is gone')
            continue
        try:
            now = file_state(path)
        except (BadFile, OSError, MutagenError) as e:
            print(f'skip {path}: {e}')
            continue
        if now == r['before']:
            continue
        if now['tags'] != r['after']['tags'] and not opts.force:
            print(f'skip {path}: tags changed since this log was written (--force restores anyway)')
            continue
        print(f'restore {path}')
        plans.append((path, r['before']))
    if not plans:
        print('Nothing to undo.')
        return 0
    if not opts.apply:
        print(f'\n{len(plans)} files would be restored. Run again with --apply.')
        return 0
    lock = write_lock()
    log = UndoLog('undo')
    try:
        done, failed = run_writes(opts.jobs, plans, lambda it: rewrite(it[0], it[1], log, note=f'undo {opts.log}'))
    finally:
        log.close()
        lock.close()
    print(f'\nRestored {done} files{f", {len(failed)} failed" if failed else ""}.')
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# info, spectrogram


def cmd_info(opts):
    for path in opts.files:
        t = analyse(os.path.abspath(path), opts)
        print(f'File        {t.path}')
        if not t.layout:
            for f in t.findings:
                print(f'  {f.level} {f.message}')
            continue
        ch = {1: 'mono', 2: 'stereo'}.get(t.channels, f'{t.channels} ch')
        mins, secs = divmod(t.duration, 60)
        kbps = (t.layout.audio_end - t.layout.audio_start) * 8 / t.duration / 1000 if t.duration else 0
        print(f'Format      FLAC {t.bits}-bit {fmt_rate(t.rate)} {ch}, {int(mins)}:{secs:06.3f}, '
              f'{fmt_size(t.layout.size)}, {kbps:.0f} kb/s')
        if t.eff_bits is not None:
            print(f'Bits used   {t.eff_bits} of {t.bits} (measured on up to {EXCERPT_S} s)')
        print(f'MD5         {t.md5:032x}' if t.md5 else 'MD5         not set')
        blocks = []
        for btype, _, n in t.layout.blocks:
            name = BLOCK_NAMES.get(btype, f'type {btype}')
            blocks.append(f'{name} {n // 18} points' if btype == 3 else f'{name} {fmt_size(n)}')
        print(f'Blocks      {", ".join(blocks)}')
        if t.layout.flac_start or t.layout.audio_end < t.layout.size:
            print(f'Glued tags  {fmt_size(t.layout.flac_start)} in front, {fmt_size(t.layout.size - t.layout.audio_end)} at the end')
        for i, p in enumerate(t.pictures, 1):
            info = image_info(p.data)
            real = f', image is {info[1]}x{info[2]} {info[0]}' if info else ', unreadable image'
            print(f'Picture #{i}  type {p.type}, {p.mime}, header {p.width}x{p.height}{real}, {fmt_size(len(p.data))}')
        if t.spectrum:
            edge, drop = t.spectrum['edge_hz'] / 1000, t.spectrum['cliff_db']
            if drop >= LOSSY_CLIFF_DB:
                print(f'Spectrum    nothing above {edge:.1f} kHz ({drop:.0f} dB cliff)')
            else:
                print(f'Spectrum    no sharp cutoff (largest drop {drop:.0f} dB, at {edge:.1f} kHz)')
        print('Tags')
        for k, v in t.tags:
            if '\n' in v:
                v = f'[{v.count(chr(10)) + 1} lines] {v.splitlines()[0][:60]}'
            print(f'  {k}={v[:200]}')
        if t.findings:
            print('Findings')
            for f in t.findings:
                print(f'  {f.level:<5} {f.message}{"  [fixable]" if f.fixable else ""}')
        print()
    return 0


def cmd_spectrogram(opts):
    src = os.path.abspath(opts.file)
    out = opts.output or f'{os.path.splitext(os.path.basename(src))[0]} spectrogram.png'
    if os.path.exists(out) and not opts.force:
        sys.exit(f'flacmeta: {out} exists (--force overwrites it)')
    r = run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y', '-i', 'file:' + src, '-filter_complex',
             '[0:a:0]showspectrumpic=s=1600x800:legend=1:scale=log:win_func=bharris:drange=150[v]', '-map', '[v]',
             '-frames:v', '1', '-f', 'image2', '-c:v', 'png', '-update', '1', 'file:' + os.path.abspath(out)])
    if r.returncode:
        sys.exit(f'flacmeta: ffmpeg failed: {tail(r.stderr)}')
    print(out)
    return 0


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(prog='flacmeta', description='Check, fix and ReplayGain-tag a FLAC library.')
    p.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 4, help='parallel files (default: CPU count)')
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check', help='read-only health report')
    c.add_argument('paths', nargs='+')
    c.add_argument('-v', '--verbose', action='store_true', help='list INFO findings too')
    c.add_argument('--quick', action='store_true', help='skip decoding: tags, covers and layout only')
    c.add_argument('--no-spectrum', action='store_true', help='skip the lossy/upsampling analysis')
    c.add_argument('--json', action='store_true', help='JSON lines: every track with its measurements and findings')
    f = sub.add_parser('fix', help='preview tag/cover/layout fixes; --apply writes them')
    f.add_argument('paths', nargs='+')
    f.add_argument('--album-year', action='store_true', help="ALBUM=Curtis -> 'Curtis (1970)', year from DATE")
    f.add_argument('--apply', action='store_true', help='write the changes')
    r = sub.add_parser('replaygain', help='ReplayGain 2.0 per album with rsgain; --apply writes')
    r.add_argument('paths', nargs='+')
    r.add_argument('--force', action='store_true', help='rescan albums that already have ReplayGain tags')
    r.add_argument('--apply', action='store_true', help='write the tags')
    i = sub.add_parser('info', help='everything about one or more files')
    i.add_argument('files', nargs='+')
    s = sub.add_parser('spectrogram', help='write a spectrogram PNG')
    s.add_argument('file')
    s.add_argument('-o', '--output', help="PNG path (default: './<name> spectrogram.png')")
    s.add_argument('--force', action='store_true', help='overwrite an existing PNG')
    u = sub.add_parser('undo', help='preview restoring the files in an undo log; --apply restores')
    u.add_argument('log')
    u.add_argument('--apply', action='store_true')
    u.add_argument('--force', action='store_true', help='restore even files whose tags changed since')
    opts = p.parse_args(argv)
    opts.jobs = max(1, opts.jobs)
    opts.quick = getattr(opts, 'quick', False)
    opts.no_spectrum = getattr(opts, 'no_spectrum', False)
    for tool in ('ffmpeg',):
        if not shutil.which(tool) and opts.cmd in ('check', 'info', 'spectrogram'):
            sys.exit(f'flacmeta: needs {tool}: sudo pacman -S --needed {tool}')

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    try:
        return {'check': cmd_check, 'fix': cmd_fix, 'replaygain': cmd_replaygain, 'info': cmd_info,
                'spectrogram': cmd_spectrogram, 'undo': cmd_undo}[opts.cmd](opts)
    except KeyboardInterrupt:
        STOP.set()
        print('\nflacmeta: interrupted; files being written were left unchanged', file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 141


if __name__ == '__main__':
    sys.exit(main())
