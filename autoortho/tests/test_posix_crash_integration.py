"""Exercise real POSIX fatal-signal capture without FUSE or a simulator."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from posix_crash import PosixCrashCollector


@unittest.skipIf(sys.platform == "win32", "POSIX signal behavior")
class PosixCrashIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ao-posix-crash-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_worker(self, body):
        collector = PosixCrashCollector(self.directory, collect_system=False)
        env = os.environ.copy()
        collector.prepare_environment(env)
        script = (
            "import sys, resource; sys.path.insert(0, " + repr(str(APP)) + "); "
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
            "from posix_crash import enable_worker_fault_logging, capture_worker_images; "
            "assert enable_worker_fault_logging();\n" + body
        )
        child = subprocess.Popen([sys.executable, "-c", script], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = child.communicate(timeout=25)
        except BaseException:
            child.kill()
            child.communicate()
            raise
        collector.start(child.pid, None)
        collector.record_exit(child.returncode)
        collector.close()
        return collector, child.returncode, stderr.decode(errors="replace")

    def test_native_segfault_preserves_stacks_and_signal(self):
        collector, code, stderr = self.run_worker('''
import ctypes, threading, time
def background_worker():
    time.sleep(30)
threading.Thread(target=background_worker, daemon=True).start()
capture_worker_images()
ctypes.string_at(0)
''')
        self.assertEqual(code, -signal.SIGSEGV, stderr)
        prefix = str(collector.prefix)
        fault = Path(prefix + ".fault.log").read_text()
        self.assertIn("Fatal Python error: Segmentation fault", fault)
        self.assertIn("background_worker", fault)
        self.assertIn("string_at", fault)
        if sys.version_info >= (3, 14):
            self.assertIn("C stack", fault)
        summary = json.loads(Path(prefix + ".crash.json").read_text())
        self.assertEqual(summary["signal"], "SIGSEGV")
        self.assertTrue(summary["native_fault"])
        metadata = json.loads(Path(prefix + ".worker.json").read_text())
        self.assertIn("native_files", metadata)
        self.assertEqual(metadata["core_settings"]["rlimit_core"], [0, 0])
        images = json.loads(Path(prefix + ".images.json").read_text())
        self.assertTrue(images["images"])
        self.assertTrue(Path(prefix + ".complete").is_file())

    def test_abort_preserves_original_exit_signal(self):
        collector, code, stderr = self.run_worker("import os; os.abort()\n")
        self.assertEqual(code, -signal.SIGABRT, stderr)
        self.assertIn("Aborted", Path(str(collector.prefix) + ".fault.log").read_text())

    def test_normal_exit_discards_empty_fault_bundle(self):
        collector, code, stderr = self.run_worker("pass\n")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_sigkill_is_reported_without_claiming_a_native_stack(self):
        collector, code, stderr = self.run_worker("import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n")
        self.assertEqual(code, -signal.SIGKILL, stderr)
        summary = json.loads(Path(str(collector.prefix) + ".crash.json").read_text())
        self.assertEqual(summary["signal"], "SIGKILL")
        self.assertFalse(summary["native_fault"])


if __name__ == "__main__":
    unittest.main()
