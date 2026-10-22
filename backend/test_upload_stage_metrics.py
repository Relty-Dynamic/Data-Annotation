"""Timing and digest reuse preserve real progress and file integrity."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from backend.upload_artifact import UploadArtifact
from backend.upload_progress import UploadProgress


class StageMetricsTests(unittest.TestCase):
    def test_overlapping_validation_uses_wall_time_and_total_does_not_sum_stages(self):
        now=[0.]
        value=UploadProgress(2,clock=lambda:now[0])
        value.begin('verify',0);value.begin('compress',0)
        now[0]=3;value.begin('verify',1)
        now[0]=5;value.complete('verify',0);value.advance('compress',0,50)
        rows={row['stage']:row for row in value.snapshot()['stages']}
        self.assertEqual(rows['verify']['progress'],50)
        self.assertEqual(rows['compress']['progress'],25)
        now[0]=7;value.complete('verify',1);value.finish('error')
        now[0]=70
        snapshot=value.snapshot();rows={row['stage']:row for row in snapshot['stages']}
        self.assertEqual(snapshot['elapsed_ms'],7000)
        self.assertEqual(rows['verify']['elapsed_ms'],7000)
        self.assertEqual(rows['compress']['state'],'stopped')
        self.assertEqual(rows['compress']['elapsed_ms'],7000)
        self.assertEqual(rows['verify']['state'],'done')
        value.begin('upload', 0);value.advance('compress', 0, 100);value.complete('compress', 0)
        self.assertEqual(value.snapshot(), snapshot)

    def test_digest_reuse_requires_same_file_and_detects_change_even_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'normal.mp4';source.write_bytes(b'finalized fixture')
            proof=UploadArtifact.read(source)
            with patch('backend.upload_artifact.sha256_file',side_effect=AssertionError('unnecessary second hash')):
                self.assertEqual(proof.digest_for(source),proof.sha256)
            replacement=Path(temp)/'replacement.mp4';replacement.write_bytes(b'altered-- fixture')
            os.utime(replacement,ns=(proof.stamp[3],proof.stamp[3]))
            os.replace(replacement,source)
            with self.assertRaises(HTTPException):proof.digest_for(source)
            with self.assertRaises(HTTPException):proof.digest_for(Path(temp)/'other.mp4')
