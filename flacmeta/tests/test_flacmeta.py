"""End-to-end tests: build a small Qobuz-style library with ffmpeg/mutagen, run flacmeta on it.

Run: python -m unittest discover -s tests -v   (from the flacmeta folder)
Needs ffmpeg, flac/metaflac, mutagen, numpy; the replaygain test also needs rsgain.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import flacmeta as fm  # noqa: E402
from mutagen.flac import FLAC, Picture  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(HERE), 'flacmeta.py')
SOXR = 'resampler=soxr:precision=28'


def ff(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args], check=True)


def noise(path, rate=44100, secs=12, fmt='s16', af=None, src_rate=None):
    """Pink noise; `src_rate` makes it at that rate first and resamples (a fake hi-res file)."""
    filters = [f'aresample={rate}:{SOXR}'] if src_rate else []
    filters += [af] if af else []
    ff('-f', 'lavfi', '-i', f'anoisesrc=color=pink:r={src_rate or rate}:d={secs}:a=0.3', '-ac', '2',
       *(['-af', ','.join(filters)] if filters else []), '-c:a', 'flac', '-sample_fmt', fmt, 'file:' + path)


def tag(path, tags, cover=None, cover_dims=(0, 0)):
    f = FLAC(path)
    f.clear_pictures()
    f.delete()
    f.add_tags() if f.tags is None else None
    for k, v in tags:
        f.tags.append((k, v))
    if cover:
        p = Picture()
        p.type, p.mime, p.data = 3, 'image/jpeg', read(cover)
        p.width, p.height = cover_dims
        f.add_picture(p)
    f.save()


def qobuz_tags(title, n, total, album='Curtis', date='1970-09-01', disc=1, disctotal=1, artist='Curtis Mayfield'):
    return [('TITLE', title), ('ARTIST', artist), ('ALBUMARTIST', artist), ('ALBUM', album), ('DATE', date),
            ('TRACKNUMBER', str(n)), ('TRACKTOTAL', str(total)), ('DISCNUMBER', str(disc)),
            ('DISCTOTAL', str(disctotal)), ('GENRE', 'Soul'), ('LABEL', 'Curtom'), ('ISRC', f'USRH1070{n:04d}'),
            ('LYRICS', '[00:01.00]line one\n[00:02.00]line two\n')]


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def digest(path):
    return fm.audio_digest(path, fm.read_layout(path))


def snapshot(root):
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            with open(p, 'rb') as f:
                out[p] = (os.stat(p).st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    return out


def cli(*args, env=None):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr


class Library:
    """Built once; tests that write copy it first."""
    root = None

    @classmethod
    def build(cls):
        if cls.root:
            return cls.root
        base = tempfile.mkdtemp(prefix='flacmeta-test-')
        cover = os.path.join(base, 'cover600.jpg')
        small = os.path.join(base, 'cover200.jpg')
        ff('-f', 'lavfi', '-i', 'color=c=red:s=600x600', '-frames:v', '1', 'file:' + cover)
        ff('-f', 'lavfi', '-i', 'color=c=blue:s=200x200', '-frames:v', '1', 'file:' + small)
        lib = os.path.join(base, 'Albums')

        # Clean album, apart from what each track carries
        a = os.path.join(lib, 'Curtis Mayfield', 'Curtis (1970)')
        os.makedirs(a)
        shutil.copy(cover, os.path.join(a, 'cover.jpg'))
        for n, title in enumerate(['If There\'s Hell Below', 'The Other Side', 'Move On Up'], 1):
            p = os.path.join(a, f'{n:02d} - {title}.flac')
            noise(p)
            tags = qobuz_tags(title, n, 3)
            if n == 2:
                tags = [(k, '2/3' if k == 'TRACKNUMBER' else v) for k, v in tags if k != 'TRACKTOTAL']
                tags += [('GENRE', 'Soul'), ('COMMENT', '  '), ('LABEL', ' Curtom')]
                tags = [t for t in tags if t != ('LABEL', 'Curtom')]
            if n == 3:
                tags = [t for t in tags if t[0] not in ('ALBUMARTIST', 'DISCTOTAL')] + [('TOTALDISCS', '1')]
            tag(p, tags, cover)
            subprocess.run(['metaflac', '--add-seekpoint=10s', p], check=True)
        # glue an ID3v2 tag in front of track 1 and an ID3v1 tag behind track 3
        p1 = os.path.join(a, '01 - If There\'s Hell Below.flac')
        data = read(p1)
        id3 = b'ID3\x03\x00\x00\x00\x00\x00\x20' + b'\x00' * 32
        with open(p1, 'wb') as f:
            f.write(id3 + data)
        p3 = os.path.join(a, '03 - Move On Up.flac')
        with open(p3, 'ab') as f:
            f.write(b'TAG' + b'Move On Up'.ljust(30, b'\x00') + b'\x00' * 95)

        # Multi-disc album, Qobuz style: TRACKTOTAL counts the whole album
        m = os.path.join(lib, 'Various', 'Multi (2001)')
        for d in (1, 2):
            os.makedirs(os.path.join(m, f'Disc {d:02d}'))
            for n in (1, 2):
                p = os.path.join(m, f'Disc {d:02d}', f'{d}{n:02d} - Song {d}{n}.flac')
                noise(p, secs=8)
                tag(p, qobuz_tags(f'Song {d}{n}', n, 4, album='Multi', date='2001', disc=d, disctotal=2,
                                  artist='Various'), cover, (600, 600))
        shutil.copy(cover, os.path.join(m, 'cover.jpg'))

        # Album with missing tracks 3 and 5, and an odd folder name
        g = os.path.join(lib, '-dash: Ünï', 'Gappy (2010)')
        os.makedirs(g)
        for n in (1, 2, 4):
            p = os.path.join(g, f'{n:02d} - it\'s "{n}".flac')
            noise(p, secs=6)
            tag(p, qobuz_tags(f'Song {n}', n, 5, album='Gappy', date='2010'), small, (200, 200))

        # Audio problems
        x = os.path.join(lib, 'Tests', 'Audio (2020)')
        os.makedirs(x)
        noise(os.path.join(x, '01 - genuine.flac'), rate=96000, fmt='s32')
        noise(os.path.join(x, '02 - upsampled.flac'), rate=96000, fmt='s32', src_rate=44100)
        noise(os.path.join(x, '03 - padded.flac'), fmt='s32',
              af='aformat=sample_fmts=s16:channel_layouts=stereo,aformat=sample_fmts=s32')
        mp3 = os.path.join(base, 'lossy.mp3')
        noise(os.path.join(base, 'cd.flac'))
        ff('-i', 'file:' + os.path.join(base, 'cd.flac'), '-c:a', 'libmp3lame', '-b:a', '128k', 'file:' + mp3)
        ff('-i', 'file:' + mp3, '-c:a', 'flac', '-sample_fmt', 's16', 'file:' + os.path.join(x, '04 - lossy.flac'))
        noise(os.path.join(x, '05 - corrupt.flac'))
        for n, name in enumerate(sorted(os.listdir(x)), 1):
            p = os.path.join(x, name)
            tag(p, qobuz_tags(name[5:-5], n, 5, album='Audio', date='2020'), cover, (600, 600))
            subprocess.run(['metaflac', '--add-seekpoint=10s', p], check=True)
        bad = os.path.join(x, '05 - corrupt.flac')
        lay = fm.read_layout(bad)
        with open(bad, 'r+b') as f:
            f.seek((lay.audio_start + lay.size) // 2)
            f.write(b'\x55' * 64)
        cls.root = lib
        return lib


def findings(lib, *extra):
    code, out, err = cli('check', '-v', '--json', *extra, lib)
    by_file = {}
    albums = {}
    for line in out.splitlines():
        r = json.loads(line)
        if r['type'] == 'track':
            by_file[os.path.relpath(r['path'], lib)] = r
        else:
            albums.setdefault(os.path.relpath(r['album'], lib), []).append(r)
    return code, by_file, albums


def codes(rec):
    return {f['code'] for f in rec['findings']}


class TestUnits(unittest.TestCase):
    def test_effective_bits(self):
        import struct
        pack = lambda vals: b''.join(struct.pack('<i', v) for v in vals)
        self.assertEqual(fm.effective_bits(pack([1 << 16, -(3 << 16), 5 << 17])), 16)
        self.assertEqual(fm.effective_bits(pack([1 << 8, 7 << 8])), 24)
        self.assertEqual(fm.effective_bits(pack([3, 0])), 32)
        self.assertIsNone(fm.effective_bits(pack([0, 0, 0])))

    def test_image_info(self):
        lib = Library.build()
        jpg = read(os.path.join(lib, 'Curtis Mayfield', 'Curtis (1970)', 'cover.jpg'))
        self.assertEqual(fm.image_info(jpg)[:3], ('image/jpeg', 600, 600))
        with tempfile.TemporaryDirectory() as d:
            png = os.path.join(d, 'x.png')
            ff('-f', 'lavfi', '-i', 'color=c=green:s=320x240', '-frames:v', '1', 'file:' + png)
            self.assertEqual(fm.image_info(read(png))[:3], ('image/png', 320, 240))
        self.assertIsNone(fm.image_info(b'not an image'))

    def test_valid_date(self):
        for ok in ('2007', '2007-05', '2007-05-31'):
            self.assertTrue(fm.valid_date(ok), ok)
        for bad in ('07', '2007-13', '2007-02-30', '2007/05/01', '0000'):
            self.assertFalse(fm.valid_date(bad), bad)


class TestCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library.build()
        cls.code, cls.files, cls.albums = findings(cls.lib)

    def f(self, rel):
        return self.files[rel]

    def test_audio_problems(self):
        x = 'Tests/Audio (2020)/'
        self.assertNotIn('upsampled', codes(self.f(x + '01 - genuine.flac')))
        self.assertNotIn('lossy-source', codes(self.f(x + '01 - genuine.flac')))
        self.assertIn('upsampled', codes(self.f(x + '02 - upsampled.flac')))
        self.assertIn('44.1 kHz', ' '.join(f['message'] for f in self.f(x + '02 - upsampled.flac')['findings']))
        self.assertIn('padded-bit-depth', codes(self.f(x + '03 - padded.flac')))
        self.assertEqual(self.f(x + '03 - padded.flac')['effective_bits'], 16)
        self.assertIn('lossy-source', codes(self.f(x + '04 - lossy.flac')))
        self.assertIn('decode-error', codes(self.f(x + '05 - corrupt.flac')))
        self.assertEqual(self.code, 1)  # an ERROR was found
        for name in ('01 - genuine.flac', '03 - padded.flac'):
            self.assertFalse(codes(self.f(x + name)) & {'upsampled', 'lossy-source', 'decode-error'}, name)

    def test_glued_tags_and_covers(self):
        c = 'Curtis Mayfield/Curtis (1970)/'
        self.assertIn('id3-glued', codes(self.f(c + "01 - If There's Hell Below.flac")))
        self.assertIn('id3-glued', codes(self.f(c + '03 - Move On Up.flac')))
        self.assertNotIn('id3-glued', codes(self.f(c + '02 - The Other Side.flac')))
        self.assertIn('cover-header', codes(self.f(c + '02 - The Other Side.flac')))
        self.assertIn('cover-small', codes(self.f('-dash: Ünï/Gappy (2010)/01 - it\'s "1".flac')))
        for name in ("01 - If There's Hell Below.flac", '03 - Move On Up.flac'):  # glued tags are not decode errors
            self.assertFalse(codes(self.f(c + name)) & {'decode-error', 'md5-mismatch'}, name)

    def test_tags(self):
        c = 'Curtis Mayfield/Curtis (1970)/'
        t2 = codes(self.f(c + '02 - The Other Side.flac'))
        self.assertTrue({'tag-duplicate', 'tag-empty', 'tag-whitespace', 'tag-malformed'} <= t2, t2)
        t3 = codes(self.f(c + '03 - Move On Up.flac'))
        self.assertTrue({'tag-missing', 'tag-synonym'} <= t3, t3)
        album = [f for f in self.albums[c.rstrip('/')] if f['code'] == 'album-inconsistent']
        self.assertEqual(sorted(f['message'].split()[0] for f in album), ['ALBUMARTIST', 'DISCTOTAL'])
        self.assertTrue(all(f['fixable'] for f in album))  # GENRE repeated in one track is not an album issue

    def test_album_numbering(self):
        multi = self.albums.get('Various/Multi (2001)', [])
        self.assertNotIn('album-incomplete', {f['code'] for f in multi})
        gappy = [f for f in self.albums['-dash: Ünï/Gappy (2010)'] if f['code'] == 'album-incomplete']
        self.assertEqual(len(gappy), 1)
        self.assertIn('missing track(s) 3, 5', gappy[0]['message'])

    def test_partial_scan_skips_numbering(self):
        _, _, albums = findings(self.lib, '--quick')
        _, _, part = findings(os.path.join(self.lib, '-dash: Ünï', 'Gappy (2010)', '01 - it\'s "1".flac'), '--quick')
        codes_ = {f['code'] for fs in part.values() for f in fs}
        self.assertIn('album-partial-scan', codes_)
        self.assertNotIn('album-incomplete', codes_)

    def test_text_report(self):
        code, out, err = cli('check', '--no-spectrum', self.lib)
        self.assertIn('Curtis Mayfield/Curtis (1970)  [3 tracks', out)
        self.assertIn('ERROR decode-error', out.replace('  ', ' '))
        self.assertIn('findings can be fixed', out)


class TestWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='flacmeta-w-')
        self.lib = os.path.join(self.tmp, 'Albums')
        shutil.copytree(Library.build(), self.lib)
        self.state = os.path.join(self.tmp, 'state')
        self.env = dict(os.environ, XDG_STATE_HOME=self.state)
        self.album = os.path.join(self.lib, 'Curtis Mayfield', 'Curtis (1970)')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def leftovers(self):
        return [os.path.join(d, n) for d, _, ns in os.walk(self.lib) for n in ns if n.startswith(fm.TMP_PREFIX)]

    def test_fix_preview_writes_nothing(self):
        before = snapshot(self.lib)
        code, out, err = cli('fix', '--album-year', self.lib, env=self.env)
        self.assertEqual(code, 0, err)
        self.assertIn('would change', out)
        self.assertIn("ALBUM 'Curtis' -> 'Curtis (1970)'", out)
        self.assertEqual(snapshot(self.lib), before)
        self.assertFalse(os.path.exists(self.state))

    def test_fix_apply_then_undo(self):
        files = sorted(os.path.join(self.album, n) for n in os.listdir(self.album) if n.endswith('.flac'))
        audio = {p: digest(p) for p in files}
        states = {p: fm.file_state(p) for p in files}
        code, out, err = cli('fix', '--album-year', '--apply', self.album, env=self.env)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self.leftovers())
        for p in files:
            self.assertEqual(digest(p), audio[p], p)  # audio frames untouched
            self.assertEqual(subprocess.run(['flac', '-t', '-s', p]).returncode, 0, p)
            lay = fm.read_layout(p)
            self.assertEqual((lay.flac_start, lay.audio_end), (0, lay.size), p)  # ID3 stripped
            f = FLAC(p)
            self.assertEqual(f['ALBUM'], ['Curtis (1970)'])
            self.assertEqual((f.pictures[0].width, f.pictures[0].height, f.pictures[0].depth), (600, 600, 24))
            self.assertIsNotNone(f.seektable)
        t2 = FLAC(files[1])
        self.assertEqual((t2['TRACKNUMBER'], t2['TRACKTOTAL'], t2['LABEL']), (['2'], ['3'], ['Curtom']))
        self.assertEqual(t2['GENRE'], ['Soul'])
        self.assertNotIn('COMMENT', t2)
        t3 = FLAC(files[2])
        self.assertEqual((t3['ALBUMARTIST'], t3['DISCTOTAL']), (['Curtis Mayfield'], ['1']))
        self.assertNotIn('TOTALDISCS', t3)
        self.assertTrue(t3['LYRICS'][0].endswith('\n'))  # multi-line text is left alone
        # rerun: nothing left to do, and check finds nothing fixable
        code, out, _ = cli('fix', '--album-year', self.album, env=self.env)
        self.assertIn('Nothing to fix', out)
        _, files_, albums = findings(self.album, '--quick')
        fixable = [f for r in files_.values() for f in r['findings'] if f['fixable']]
        fixable += [f for fs in albums.values() for f in fs if f['fixable']]
        self.assertEqual(fixable, [])
        # undo: preview changes nothing, --apply restores tags, pictures and the glued ID3 bytes
        logs = os.listdir(os.path.join(self.state, 'flacmeta'))
        log = os.path.join(self.state, 'flacmeta', [n for n in logs if n.startswith('fix-')][0])
        before = snapshot(self.lib)
        code, out, err = cli('undo', log, env=self.env)
        self.assertIn('3 files would be restored', out)
        self.assertEqual(snapshot(self.lib), before)
        code, out, err = cli('undo', log, '--apply', env=self.env)
        self.assertEqual(code, 0, out + err)
        for p in files:
            self.assertEqual(fm.file_state(p), states[p], p)
            self.assertEqual(digest(p), audio[p], p)
        self.assertFalse(self.leftovers())

    def test_failed_step_leaves_file_alone(self):
        shim = os.path.join(self.tmp, 'shim')
        os.makedirs(shim)
        with open(os.path.join(shim, 'metaflac'), 'w') as f:
            f.write('#!/bin/sh\necho "metaflac: simulated failure" >&2\nexit 1\n')
        os.chmod(os.path.join(shim, 'metaflac'), 0o755)
        x = os.path.join(self.lib, 'Tests', 'Audio (2020)')
        target = os.path.join(x, '01 - genuine.flac')
        subprocess.run(['metaflac', '--remove', '--block-type=SEEKTABLE', target], check=True)
        before = snapshot(x)
        env = dict(self.env, PATH=shim + os.pathsep + os.environ['PATH'])
        code, out, err = cli('fix', '--apply', target, env=env)
        self.assertEqual(code, 1)
        self.assertIn('simulated failure', err)
        self.assertEqual(snapshot(x), before)
        self.assertFalse(self.leftovers())

    def test_sigterm_mid_run(self):
        x = os.path.join(self.lib, 'Many (2000)')
        os.makedirs(x)
        src = os.path.join(self.album, '02 - The Other Side.flac')
        for n in range(40):
            shutil.copy(src, os.path.join(x, f'{n:02d} - copy.flac'))
        audio = digest(src)
        shim = os.path.join(self.tmp, 'slow')  # a cp that takes 0.3 s, so the signal lands mid-run
        os.makedirs(shim)
        with open(os.path.join(shim, 'cp'), 'w') as f:
            f.write(f'#!/bin/sh\nsleep 0.3\nexec {shutil.which("cp")} "$@"\n')
        os.chmod(os.path.join(shim, 'cp'), 0o755)
        env = dict(self.env, PATH=shim + os.pathsep + os.environ['PATH'])
        p = subprocess.Popen([sys.executable, SCRIPT, '-j', '2', 'fix', '--album-year', '--apply', x],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        time.sleep(2)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, err)
        time.sleep(0.5)  # the cp shim that was running finishes on its own
        self.assertFalse(self.leftovers())
        changed = 0
        for n in os.listdir(x):
            f = FLAC(os.path.join(x, n))
            self.assertIn(f['ALBUM'], (['Curtis'], ['Curtis (1970)']))
            changed += f['ALBUM'] == ['Curtis (1970)']
            self.assertEqual(digest(os.path.join(x, n)), audio)
        self.assertTrue(0 < changed < 40, changed)
        log = [n for n in os.listdir(os.path.join(self.state, 'flacmeta')) if n.startswith('fix-')][0]
        with open(os.path.join(self.state, 'flacmeta', log)) as f:
            self.assertEqual(sum(1 for _ in f), changed)  # one undo record per file actually replaced
        # a rerun finishes the job
        code, out, err = cli('fix', '--album-year', '--apply', x, env=self.env)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(FLAC(os.path.join(x, n))['ALBUM'] == ['Curtis (1970)'] for n in os.listdir(x)))

    @unittest.skipUnless(shutil.which('rsgain'), 'rsgain not installed')
    def test_replaygain(self):
        m = os.path.join(self.lib, 'Various', 'Multi (2001)')
        files = sorted(os.path.join(d, n) for d, _, ns in os.walk(m) for n in ns if n.endswith('.flac'))
        audio = {p: digest(p) for p in files}
        other = {p: fm.non_rg(FLAC(p).tags) for p in files}
        before = snapshot(m)
        code, out, err = cli('replaygain', m, env=self.env)
        self.assertIn('scan  Multi (2001)  (4 tracks, 2 discs)', out)
        self.assertEqual(snapshot(m), before)
        code, out, err = cli('replaygain', '--apply', m, env=self.env)
        self.assertEqual(code, 0, out + err)
        gains = set()
        for p in files:
            f = FLAC(p)
            for k in fm.RG_TAGS:
                self.assertIn(k, f, p)
            gains.add(f['REPLAYGAIN_ALBUM_GAIN'][0])
            self.assertEqual(digest(p), audio[p])
            self.assertEqual(fm.non_rg(f.tags), other[p])
        self.assertEqual(len(gains), 1)  # one album gain across both discs
        code, out, err = cli('replaygain', m, env=self.env)
        self.assertIn('skip  1 album(s) that already have ReplayGain tags', out)
        self.assertFalse(self.leftovers())


class TestOther(unittest.TestCase):
    def test_info_and_spectrogram(self):
        lib = Library.build()
        p = os.path.join(lib, 'Tests', 'Audio (2020)', '02 - upsampled.flac')
        code, out, err = cli('info', p)
        self.assertEqual(code, 0, err)
        self.assertIn('FLAC 32-bit 96 kHz' if 'FLAC 32-bit' in out else 'FLAC 24-bit 96 kHz', out)
        self.assertIn('upsampled', out)
        with tempfile.TemporaryDirectory() as d:
            png = os.path.join(d, 'out.png')
            code, out, err = cli('spectrogram', p, '-o', png)
            self.assertEqual(code, 0, err)
            self.assertEqual(read(png)[:8], b'\x89PNG\r\n\x1a\n')
            code, out, err = cli('spectrogram', p, '-o', png)
            self.assertNotEqual(code, 0)  # no silent overwrite


if __name__ == '__main__':
    unittest.main()
