"""Real timestamp/audio parity across NAS cache and desktop upload encoding."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.mock_s3 import MockUploadManager
from backend.previews import PreviewSpec, run_ffmpeg
from backend.service import ProjectService


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / '.tools/ffmpeg/bin' / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
FFPROBE = ROOT / '.tools/ffmpeg/bin' / ('ffprobe.exe' if os.name == 'nt' else 'ffprobe')


class CompactParityTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = str(FFMPEG) if FFMPEG.is_file() else shutil.which('ffmpeg')
        self.ffprobe = str(FFPROBE) if FFPROBE.is_file() else shutil.which('ffprobe')
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest('FFmpeg and FFprobe required')
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'.tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = ProjectService(self.root/'state', enable_remote=False)
        self.service.tool = lambda name: Path(self.ffmpeg if name == 'ffmpeg' else self.ffprobe)
        self.service.cached_preview_for_spec = lambda spec: None
        self.addCleanup(self.service.sessions.close)
        self.addCleanup(self.service.previews.close)

    def run_case(self, *, audio):
        source = self.root/'source.mp4'
        args = [self.ffmpeg,'-v','error','-f','lavfi','-t','2','-i','testsrc=size=96x64:rate=10']
        if audio:
            args += ['-itsoffset','0.2','-f','lavfi','-t','2','-i','sine=frequency=440:sample_rate=48000',
                     '-map','0:v:0','-map','1:a:0','-c:a','aac']
        args += ['-c:v','libx264','-pix_fmt','yuv420p','-output_ts_offset','5',str(source)]
        subprocess.run(args,check=True,capture_output=True)
        metadata = self.service.probe(source)
        self.assertGreaterEqual(metadata['media_start_seconds'],5)
        metadata.update(path=str(source),stamp=self.service.source_stamp(source))
        nas = self.root/'nas.mp4'
        upload = self.root/'upload.mp4'
        spec = PreviewSpec('fixture',source,metadata,nas,self.root/'error.json')
        with patch('backend.session_cache.run_ffmpeg',wraps=run_ffmpeg) as nas_render, \
             patch('backend.mock_s3.run_ffmpeg',wraps=run_ffmpeg) as upload_render:
            self.service.sessions._render_compact(spec,nas,lambda value:None,threading.Event())
            MockUploadManager._compress_video(self.service,source,upload,lambda value:None)
        self.assertEqual(nas_render.call_args.args[0][:-1],upload_render.call_args.args[0][:-1])
        def streams(path):
            result = subprocess.run([self.ffprobe,'-v','error','-show_entries',
                'stream=codec_type,codec_name,width,height,start_time,duration,nb_frames',
                '-of','json',str(path)],check=True,capture_output=True,text=True)
            return {item['codec_type']:item for item in json.loads(result.stdout)['streams']}
        left,right = streams(nas),streams(upload)
        self.assertEqual(left,right)
        self.assertEqual(left['video']['codec_name'],'h264')
        self.assertEqual((left['video']['width'],left['video']['height']),(480,270))
        self.assertEqual(float(left['video']['start_time']),0)
        self.assertAlmostEqual(float(left['video']['duration']),2,places=3)
        if audio:
            self.assertEqual(left['audio']['codec_name'],'aac')
            self.assertGreater(float(left['audio']['start_time']),0.1)
            self.assertLess(float(left['audio']['start_time']),0.25)
        else:
            self.assertNotIn('audio',left)
        def decoded(path):
            result=subprocess.run([self.ffmpeg,'-v','error','-i',str(path),'-map','0','-f','framemd5','-'],
                                  check=True,capture_output=True,text=True)
            return [line for line in result.stdout.splitlines() if not line.startswith('#')]
        # Compare every decoded frame's stream, PTS, duration and sample size.
        # Lossy video packet/pixel hashes are not a cross-run encoding contract.
        timing=lambda path:[line.rsplit(',',1)[0] for line in decoded(path)]
        self.assertEqual(timing(nas),timing(upload))

    def test_nonzero_start_with_delayed_audio_matches_nas(self):
        self.run_case(audio=True)

    def test_nonzero_start_without_audio_matches_nas(self):
        self.run_case(audio=False)
