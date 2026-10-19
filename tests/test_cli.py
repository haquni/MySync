"""Process-level acceptance tests for the MySync CLI (TASK-011)."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from mysync.application.service import DiscoveredDevice
from mysync.cli import CliError, _select_device


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.bind(("127.0.0.1", 0))
        return int(udp_socket.getsockname()[1])


class CliAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.environment = os.environ.copy()
        self.environment["PYTHONPATH"] = str(SOURCE_ROOT)
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.environment["PYTHONWARNINGS"] = "error::ResourceWarning"
        self.environment["PYTHONUNBUFFERED"] = "1"

    def command(self, config, *arguments):
        return [
            sys.executable,
            "-m",
            "mysync",
            "--config",
            str(config),
            *arguments,
        ]

    def run_cli(self, config, *arguments, timeout=10.0):
        return subprocess.run(
            self.command(config, *arguments),
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def initialize(self, config, root, name):
        result = self.run_cli(config, "init", str(root), "--name", name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(config.is_file())
        return result

    @staticmethod
    def set_udp_port(config, port):
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["udp_port"] = port
        config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def start_server(self, config, *, once=True):
        arguments = ["serve", "--bind", "127.0.0.1"]
        if once:
            arguments.append("--once")
        process = subprocess.Popen(
            self.command(config, *arguments),
            cwd=PROJECT_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3.0)

    def wait_for_device(self, client_config, expected_name, timeout=5.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.run_cli(
                client_config,
                "devices",
                "--host",
                "127.0.0.1",
                "--timeout",
                "0.15",
                timeout=3.0,
            )
            if last.returncode == 0 and expected_name in last.stdout:
                return last
            time.sleep(0.05)
        self.fail(
            f"device {expected_name!r} was not discovered; "
            f"stdout={getattr(last, 'stdout', '')!r}, stderr={getattr(last, 'stderr', '')!r}"
        )

    @staticmethod
    def write(root, relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_two_process_cli_smoke_and_idempotent_second_sync(self):
        left_root = self.base / "left root"
        right_root = self.base / "right root"
        left_config = self.base / "left.json"
        right_config = self.base / "right.json"
        self.initialize(left_config, left_root, "Linux Client")
        self.initialize(right_config, right_root, "Windows Server")
        udp_port = free_udp_port()
        self.set_udp_port(left_config, udp_port)
        self.set_udp_port(right_config, udp_port)

        large = bytes(range(256)) * (1024 * 1024 // 256 + 129)
        self.write(left_root, "left/пустой.txt", b"")
        self.write(left_root, "left/large.bin", large)
        self.write(right_root, "right/日本語.bin", b"\x00\xffserver")
        self.write(left_root, "conflict.txt", b"left")
        self.write(right_root, "conflict.txt", b"right")
        (left_root / "empty-left").mkdir()
        (right_root / "empty-right").mkdir()

        server = self.start_server(right_config)
        devices = self.wait_for_device(left_config, "Windows Server")
        self.assertIn("127.0.0.1", devices.stdout)
        sync = self.run_cli(
            left_config,
            "sync",
            "--device",
            "Windows Server",
            "--host",
            "127.0.0.1",
            "--timeout",
            "0.3",
            timeout=15.0,
        )
        self.assertEqual(sync.returncode, 0, sync.stderr)
        server_stdout, server_stderr = server.communicate(timeout=10.0)
        self.assertEqual(server.returncode, 0, server_stderr)
        self.assertIn("Synchronized with", server_stdout)
        self.assertEqual((right_root / "left/large.bin").read_bytes(), large)
        self.assertEqual((left_root / "right/日本語.bin").read_bytes(), b"\x00\xffserver")
        self.assertEqual((left_root / "conflict.txt").read_bytes(), b"left")
        self.assertEqual((right_root / "conflict.txt").read_bytes(), b"right")
        self.assertTrue((left_root / "empty-right").is_dir())
        self.assertTrue((right_root / "empty-left").is_dir())
        self.assertIn("Conflicts (unchanged)", sync.stdout)

        status = self.run_cli(left_config, "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Device: Linux Client", status.stdout)
        self.assertIn("Files: 4", status.stdout)

        second_server = self.start_server(right_config)
        self.wait_for_device(left_config, "Windows Server")
        second = self.run_cli(
            left_config,
            "sync",
            "--host",
            "127.0.0.1",
            "--timeout",
            "0.3",
            timeout=10.0,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_server.communicate(timeout=8.0)
        self.assertEqual(second_server.returncode, 0)
        self.assertNotIn("Received file:", second.stdout)
        self.assertNotIn("Sent file:", second.stdout)

    def test_long_running_server_accepts_valid_session_after_rejected_one(self):
        client_root = self.base / "client"
        server_root = self.base / "server"
        client_config = self.base / "client.json"
        server_config = self.base / "server.json"
        self.initialize(client_config, client_root, "Client")
        self.initialize(server_config, server_root, "Server")
        udp_port = free_udp_port()
        self.set_udp_port(client_config, udp_port)
        self.set_udp_port(server_config, udp_port)
        self.write(client_root, "survives.txt", b"content")
        server = self.start_server(server_config, once=False)
        self.wait_for_device(client_config, "Server")
        server_payload = json.loads(server_config.read_text(encoding="utf-8"))

        with socket.create_connection(
            ("127.0.0.1", server_payload["tcp_port"]), timeout=2.0
        ) as broken:
            broken.sendall(b"\x00\x00")

        sync = self.run_cli(
            client_config,
            "sync",
            "--host",
            "127.0.0.1",
            "--timeout",
            "0.3",
            timeout=10.0,
        )
        self.assertEqual(sync.returncode, 0, sync.stderr)
        self.assertEqual((server_root / "survives.txt").read_bytes(), b"content")
        server.terminate()
        stdout, stderr = server.communicate(timeout=5.0)
        self.assertIn("Rejected session:", stderr)
        self.assertIn("Synchronized with", stdout)

    def test_init_errors_missing_corrupt_config_and_unknown_command(self):
        config = self.base / "config.json"
        root = self.base / "root"
        self.initialize(config, root, "PC")
        repeated = self.run_cli(config, "init", str(root), "--name", "PC")
        self.assertEqual(repeated.returncode, 1)
        self.assertIn("already exists", repeated.stderr)

        missing = self.run_cli(self.base / "missing.json", "status")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("configuration not found", missing.stderr)

        config.write_text("{broken", encoding="utf-8")
        corrupt = self.run_cli(config, "status")
        self.assertEqual(corrupt.returncode, 1)
        self.assertIn("Invalid JSON configuration", corrupt.stderr)

        unknown = self.run_cli(self.base / "missing.json", "unknown")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("invalid choice", unknown.stderr)

    def test_config_rejects_boolean_schema_version(self):
        config = self.base / "config.json"
        root = self.base / "root"
        self.initialize(config, root, "PC")
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["schema_version"] = True
        config.write_text(json.dumps(payload), encoding="utf-8")
        boolean_version = self.run_cli(config, "status")
        self.assertEqual(boolean_version.returncode, 1)
        self.assertIn("Unsupported schema version", boolean_version.stderr)

    def test_config_rejects_duplicate_fields(self):
        config = self.base / "config.json"
        root = self.base / "root"
        self.initialize(config, root, "PC")
        valid = config.read_text(encoding="utf-8")
        duplicate = valid.replace(
            '"root_path":', '"root_path": "/unexpected", "root_path":', 1
        )
        config.write_text(duplicate, encoding="utf-8")
        duplicated_field = self.run_cli(config, "status")
        self.assertEqual(duplicated_field.returncode, 1)
        self.assertIn("duplicate", duplicated_field.stderr.lower())

    def test_stalled_peer_does_not_block_the_only_server_session_forever(self):
        client_root = self.base / "client-stall"
        server_root = self.base / "server-stall"
        client_config = self.base / "client-stall.json"
        server_config = self.base / "server-stall.json"
        self.initialize(client_config, client_root, "Client")
        self.initialize(server_config, server_root, "Server")
        udp_port = free_udp_port()
        self.set_udp_port(client_config, udp_port)
        self.set_udp_port(server_config, udp_port)
        self.write(client_root, "queued.txt", b"must eventually sync")
        server = self.start_server(server_config, once=False)
        self.wait_for_device(client_config, "Server")
        server_payload = json.loads(server_config.read_text(encoding="utf-8"))
        stalled = socket.create_connection(
            ("127.0.0.1", server_payload["tcp_port"]), timeout=2.0
        )
        self.addCleanup(stalled.close)
        try:
            sync = self.run_cli(
                client_config,
                "sync",
                "--host",
                "127.0.0.1",
                "--timeout",
                "0.2",
                timeout=3.0,
            )
        finally:
            stalled.close()
        self.assertEqual(sync.returncode, 0, sync.stderr)
        self.assertEqual((server_root / "queued.txt").read_bytes(), b"must eventually sync")

    def test_init_rejects_file_as_root_with_clear_error(self):
        ordinary = self.base / "ordinary.txt"
        ordinary.write_text("data", encoding="utf-8")
        bad = self.run_cli(
            self.base / "bad.json", "init", str(ordinary), "--name", "PC"
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("root is not a directory", bad.stderr)

    def test_config_option_is_supported_after_command(self):
        config = self.base / "after.json"
        root = self.base / "after-root"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mysync",
                "init",
                str(root),
                "--name",
                "After",
                "--config",
                str(config),
            ],
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_device_selection_handles_none_multiple_and_ambiguous_names(self):
        first = DiscoveredDevice(str(uuid4()), "Same", "127.0.0.1", 10001)
        second = DiscoveredDevice(str(uuid4()), "same", "127.0.0.2", 10002)
        with self.assertRaises(CliError):
            _select_device((), None)
        with self.assertRaises(CliError):
            _select_device((first, second), None)
        with self.assertRaises(CliError):
            _select_device((first, second), "same")
        self.assertEqual(_select_device((first, second), first.device_id), first)

    def test_import_has_no_network_or_thread_activity(self):
        script = """
import socket
import threading
def fail(*args, **kwargs):
    raise AssertionError("activity during import")
socket.socket = fail
socket.create_connection = fail
threading.Thread.start = fail
import mysync.cli
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
