from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("deploy_worker", Path(__file__).with_name("deploy_worker.py"))
assert SPEC is not None and SPEC.loader is not None
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.remote = self.project / ".local" / "remote"
        self.remote.mkdir(parents=True)

    def make_payload_project(self) -> None:
        backend = self.project / "backend"
        backend.mkdir()
        (backend / "remote_worker.py").write_text("def create_worker(): pass\n", encoding="utf-8")
        (backend / "test_secrets.py").write_text("DO_NOT_UPLOAD", encoding="utf-8")
        (backend / "cache.bin").write_bytes(b"DO_NOT_UPLOAD")
        (self.project / "requirements.lock.txt").write_text("fastapi==1.0\n", encoding="utf-8")
        scripts = self.project / "deploy"
        scripts.mkdir()
        for name in deployment.DEPLOY_FILES:
            (scripts / name).write_bytes(b"example\r\n")
        (self.remote / "worker-token").write_text("a" * 64 + "\n", encoding="utf-8")
        (self.remote / "id_ed25519.pub").write_text("ssh-ed25519 AAAA datamark-worker\n", encoding="utf-8")
        (self.remote / "id_ed25519").write_text("PRIVATE_KEY_MUST_STAY_LOCAL", encoding="utf-8")
        (self.remote / "draft.db").write_bytes(b"LOCAL_DRAFT")

    def test_payload_excludes_private_keys_and_local_data(self) -> None:
        self.make_payload_project()
        payload = deployment.build_payload(self.project, self.remote, "20260928T000000-123456789abc")
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.getnames()), {
                "backend/remote_worker.py", "requirements.lock.txt",
                "deploy/Dockerfile", "deploy/compose.yaml", "deploy/healthcheck.py", "deploy/install-worker.sh",
                "payload/worker-token", "payload/worker-key.pub", "payload/release-id",
            })
            self.assertEqual(archive.getmember("payload/worker-token").mode, 0o600)
            self.assertNotIn(b"\r", archive.extractfile("deploy/install-worker.sh").read())
        self.assertNotIn(b"PRIVATE_KEY_MUST_STAY_LOCAL", payload)
        self.assertNotIn(b"LOCAL_DRAFT", payload)

    def test_windows_credentials_and_shell_files_are_uploaded_with_lf(self) -> None:
        self.make_payload_project()
        token_file = self.remote / "worker-token"
        windows_token = b"a" * 64 + b"\r\n"
        token_file.write_bytes(windows_token)
        public_key_file = self.remote / "id_ed25519.pub"
        windows_public_key = b"ssh-ed25519 AAAA datamark-worker\r\n"
        public_key_file.write_bytes(windows_public_key)
        release_id = "20260928T000000-123456789abc"
        payload = deployment.build_payload(self.project, self.remote, release_id)
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            self.assertEqual(archive.extractfile("payload/worker-token").read(), b"a" * 64 + b"\n")
            self.assertEqual(archive.extractfile("payload/worker-key.pub").read(), b"ssh-ed25519 AAAA datamark-worker\n")
            self.assertEqual(archive.extractfile("payload/release-id").read(), release_id.encode("ascii") + b"\n")
            for name in deployment.DEPLOY_FILES:
                self.assertEqual(archive.extractfile("deploy/" + name).read(), b"example\n")
        self.assertEqual(token_file.read_bytes(), windows_token)
        self.assertEqual(public_key_file.read_bytes(), windows_public_key)

    def test_remote_shell_command_preserves_literal_variables_and_quotes(self) -> None:
        command = deployment.remote_install_command()
        self.assertNotIn("\r", command)
        arguments = shlex.split(command)
        self.assertEqual(arguments[:2], ["bash", "-c"])
        self.assertEqual(len(arguments), 3)
        self.assertIn('tar -xf - -C "$stage"', arguments[2])
        self.assertIn('bash "$stage/deploy/install-worker.sh" "$stage"', arguments[2])

    def test_install_logs_directly_and_accepts_only_current_ready_line(self) -> None:
        release_id = "20260928T000000-123456789abc"
        process = MagicMock()
        process.wait.return_value = 0

        def launch(command, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.PIPE)
            self.assertIsInstance(kwargs["stdout"], io.FileIO)
            self.assertIs(kwargs["stdout"], kwargs["stderr"])
            kwargs["stdout"].write(f"DATAMARK_DEPLOY_READY {release_id}\n".encode("ascii"))
            self.assertIn(b"DATAMARK_DEPLOY_READY", (self.remote / "deploy.log").read_bytes())
            return process

        with patch.object(deployment.subprocess, "Popen", side_effect=launch), \
             patch("builtins.print"):
            deployment.run_install("ssh", self.remote, b"tar payload", release_id)
        process.wait.assert_called_once_with(timeout=1200)
        process.stdin.write.assert_called_once_with(b"tar payload")
        process.stdin.close.assert_called_once()
        process.stdout.read.assert_not_called()

    def test_install_rejects_a_ready_marker_from_a_previous_connection(self) -> None:
        release_id = "20260928T000000-123456789abc"
        (self.remote / "deploy.log").write_bytes(f"DATAMARK_DEPLOY_READY {release_id}\n".encode("ascii"))
        process = MagicMock()
        process.wait.return_value = 0
        with patch.object(deployment.subprocess, "Popen", return_value=process), \
             patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                deployment.run_install("ssh", self.remote, b"tar payload", release_id)

    def test_install_rejects_an_embedded_ready_marker(self) -> None:
        release_id = "20260928T000000-123456789abc"
        process = MagicMock()
        process.wait.return_value = 0

        def launch(command, **kwargs):
            kwargs["stdout"].write(f"echo DATAMARK_DEPLOY_READY {release_id}\n".encode("ascii"))
            return process

        with patch.object(deployment.subprocess, "Popen", side_effect=launch), \
             patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                deployment.run_install("ssh", self.remote, b"tar payload", release_id)

    def test_install_timeout_stops_its_ssh_process(self) -> None:
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 1200), 0]
        process.poll.return_value = None
        with patch.object(deployment.subprocess, "Popen", return_value=process), \
             patch("builtins.print"):
            with self.assertRaises(subprocess.TimeoutExpired):
                deployment.run_install("ssh", self.remote, b"tar payload", "20260928T000000-123456789abc")
        process.terminate.assert_called_once()
        self.assertEqual(process.wait.call_args_list[0].kwargs, {"timeout": 1200})

    def test_missing_worker_prevents_any_payload(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "remote_worker.py"):
            deployment.build_payload(self.project, self.remote, "ignored")

    def test_trust_requires_existing_known_host_and_preserves_hashed_records(self) -> None:
        source = self.project / "known_hosts"
        source.write_text("placeholder", encoding="utf-8")
        destination = self.remote / "known_hosts"
        record = "|1|salt|hash ssh-ed25519 AAAATESTKEY"
        completed = subprocess.CompletedProcess([], 0, f"# Host found\n{record}\n", "")
        with patch.object(deployment.subprocess, "run", return_value=completed) as run:
            deployment.copy_trusted_host("ssh-keygen", source, destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), record + "\n")
        self.assertEqual(run.call_args.args[0], ["ssh-keygen", "-F", "192.168.2.126", "-f", str(source)])

    def test_unknown_or_revoked_host_does_not_create_trust(self) -> None:
        source = self.project / "known_hosts"
        source.write_text("placeholder", encoding="utf-8")
        destination = self.remote / "known_hosts"
        for result in (subprocess.CompletedProcess([], 1, "", ""),
                       subprocess.CompletedProcess([], 0, "@revoked host ssh-ed25519 AAAA\n", "")):
            with patch.object(deployment.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError):
                    deployment.copy_trusted_host("ssh-keygen", source, destination)
        self.assertFalse(destination.exists())

    def test_configuration_contains_only_secret_paths_and_expected_mappings(self) -> None:
        config = deployment.worker_configuration(self.remote)
        self.assertEqual(config["port"], 18120)
        self.assertEqual(config["local_port"], 18121)
        self.assertEqual(config["host_key_alias"], "192.168.2.126")
        self.assertEqual(config["mappings"][0], {"local": "\\\\Relty\\homes", "remote": "/mnt/nas/homes"})
        self.assertNotIn("token", config)
        self.assertTrue(Path(config["identity_file"]).is_absolute())

    def test_failed_remote_install_does_not_enable_local_worker(self) -> None:
        fake_script = self.project / "deploy" / "deploy_worker.py"
        fake_script.parent.mkdir()
        config_path = self.remote / "worker.json"
        old_config = {"enabled": False, "previous": "preserve"}
        config_path.write_text(json.dumps(old_config), encoding="utf-8")
        with patch.object(deployment, "__file__", str(fake_script)), \
             patch.object(deployment.sys, "argv", ["deploy_worker.py"]), \
             patch.object(deployment.shutil, "which", return_value="ssh"), \
             patch.object(deployment, "prepare_credentials", return_value=self.remote), \
             patch.object(deployment, "build_payload", return_value=b"payload"), \
             patch.object(deployment, "run_install", side_effect=RuntimeError("remote build failed")), \
             patch.object(deployment, "verify_tunnel") as verify:
            result = deployment.main()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), old_config)
        self.assertEqual(json.loads((self.remote / "deploy-status.json").read_text(encoding="utf-8"))["state"], "failed")
        verify.assert_not_called()

    def test_configuration_is_enabled_only_after_tunnel_verification(self) -> None:
        fake_script = self.project / "deploy" / "deploy_worker.py"
        fake_script.parent.mkdir()
        config_path = self.remote / "worker.json"

        def verify(*args: object) -> None:
            self.assertFalse(config_path.exists())

        with patch.object(deployment, "__file__", str(fake_script)), \
             patch.object(deployment.sys, "argv", ["deploy_worker.py"]), \
             patch.object(deployment.shutil, "which", return_value="ssh"), \
             patch.object(deployment, "prepare_credentials", return_value=self.remote), \
             patch.object(deployment, "build_payload", return_value=b"payload"), \
             patch.object(deployment, "run_install"), \
             patch.object(deployment, "verify_tunnel", side_effect=verify):
            result = deployment.main()
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(config_path.read_text(encoding="utf-8"))["enabled"])

    def test_failed_tunnel_does_not_enable_local_worker(self) -> None:
        fake_script = self.project / "deploy" / "deploy_worker.py"
        fake_script.parent.mkdir()
        config_path = self.remote / "worker.json"
        with patch.object(deployment, "__file__", str(fake_script)), \
             patch.object(deployment.sys, "argv", ["deploy_worker.py"]), \
             patch.object(deployment.shutil, "which", return_value="ssh"), \
             patch.object(deployment, "prepare_credentials", return_value=self.remote), \
             patch.object(deployment, "build_payload", return_value=b"payload"), \
             patch.object(deployment, "run_install"), \
             patch.object(deployment, "verify_tunnel", side_effect=RuntimeError("tunnel rejected")):
            result = deployment.main()
        self.assertEqual(result, 1)
        self.assertFalse(config_path.exists())

    def test_project_environment_is_scoped_and_restored(self) -> None:
        original = dict(os.environ)
        previous = deployment.configure_environment(self.project)
        try:
            for name in ("TEMP", "TMP", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
                self.assertTrue(Path(os.environ[name]).is_relative_to(self.project))
            self.assertEqual(os.environ["PYTHONNOUSERSITE"], "1")
            self.assertEqual(os.environ["PIP_REQUIRE_VIRTUALENV"], "1")
        finally:
            deployment.restore_environment(previous)
        self.assertEqual(dict(os.environ), original)

    def test_worker_parallelism_stays_within_the_existing_container_budget(self) -> None:
        compose = Path(__file__).with_name("compose.yaml").read_text(encoding="utf-8")
        self.assertIn('      DATAMARK_WORKER_SLOTS: "2"\n', compose)
        self.assertIn('      DATAMARK_FFMPEG_ENCODER_THREADS: "2"\n', compose)
        self.assertIn('    cpus: 4.0\n', compose)
        self.assertIn('    mem_limit: 6g\n', compose)
        self.assertIn('      - "127.0.0.1:18120:18120"\n', compose)

    def tunnel_mocks(self):
        (self.remote / "worker-token").write_text("a" * 64, encoding="utf-8")
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ("127.0.0.1", 41123)
        process = MagicMock()
        process.poll.return_value = None
        return reservation, process

    def test_stale_listener_log_never_sends_token(self) -> None:
        reservation, process = self.tunnel_mocks()
        (self.remote / "tunnel-check.log").write_bytes(
            b"debug1: Local forwarding listening on 127.0.0.1 port 41123.\n")
        process.poll.side_effect = [None, 1, 1]
        with patch.object(deployment.socket, "socket", return_value=reservation), \
             patch.object(deployment.subprocess, "Popen", return_value=process), \
             patch.object(deployment.time, "sleep"), \
             patch.object(deployment.urllib.request, "build_opener") as make_opener:
            with self.assertRaisesRegex(RuntimeError, "dedicated SSH tunnel failed"):
                deployment.verify_tunnel("ssh", self.remote)
        make_opener.return_value.open.assert_not_called()
        self.assertEqual(make_opener.call_args.args[0].proxies, {})

    def test_owned_listener_is_required_before_authenticated_health(self) -> None:
        reservation, process = self.tunnel_mocks()

        def launch(command, **kwargs):
            self.assertIn("-v", command)
            kwargs["stdout"].write(b"debug1: Local forwarding listening on 127.0.0.1 port 41123.\n")
            kwargs["stdout"].flush()
            return process

        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"status":"ok","application":"datamark-worker","protocol":1}')
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(deployment.socket, "socket", return_value=reservation), \
             patch.object(deployment.subprocess, "Popen", side_effect=launch), \
             patch.object(deployment.urllib.request, "build_opener", return_value=opener):
            deployment.verify_tunnel("ssh", self.remote)
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer " + "a" * 64)
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
