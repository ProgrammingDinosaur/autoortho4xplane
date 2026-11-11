"""Real Windows collector checks; no simulator, FUSE or imagery required.

Run with stdlib unittest so release verification needs no application packages.
The deliberate failure validates capture, not a reproduction of a user crash.
"""

import ctypes
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import unittest
import uuid

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from windows_crash import WindowsCrashCollector, READY_ENV, native_directory
from native_build import verify_bundle


@unittest.skipUnless(sys.platform == "win32", "Requires Windows debug APIs")
class WindowsCrashIntegration(unittest.TestCase):
    def setUp(self):
        self.directory = Path.cwd() / "crash-smoke-evidence" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.stderr = (self.directory / "collector.log").open("ab", buffering=0)
        self.addCleanup(self.stderr.close)

    def launch(self, body):
        collector = WindowsCrashCollector(self.directory)
        env = os.environ.copy()
        env[READY_ENV] = collector.event_name
        script = (
            "import sys; sys.path.insert(0, " + repr(str(APP)) + "); "
            "from windows_crash import wait_for_collector; wait_for_collector();\n" + body
        )
        child = subprocess.Popen([sys.executable, "-c", script], env=env,
                                 stdout=self.stderr, stderr=self.stderr)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.addCleanup(collector.close)
        collector.start(child.pid, self.stderr)
        self.assertIsNotNone(collector.process)
        return child, collector

    def test_fatal_fastfail_records_exception_and_native_dump(self):
        child, collector = self.launch('''
import ctypes
class Record(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("nested", ctypes.c_void_p), ("address", ctypes.c_void_p),
                ("count", ctypes.c_uint32), ("parameters", ctypes.c_size_t * 15)]
record = Record()
record.code = 0xC0000409
record.count = 1
record.parameters[0] = 7
fail = ctypes.WinDLL("kernel32").RaiseFailFastException
fail.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
fail.restype = None
fail(ctypes.byref(record), None, 0)
''')
        self.assertEqual(child.wait(timeout=30) & 0xFFFFFFFF, 0xC0000409)
        collector.close()
        reports = list(self.directory.glob("*.crash.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["pid"], child.pid)
        self.assertEqual(report["exception_code"].lower(), "0xc0000409")
        self.assertEqual(report["parameters"][0], "0x7")
        self.assertTrue(report["dump_written"], report)
        self.assertEqual(report["context_error"], 0, report)
        dump = next(self.directory.glob("*.dmp")).read_bytes()
        self.assertEqual(dump[:4], b"MDMP")
        count, directory = struct.unpack_from("<II", dump, 8)
        streams = {struct.unpack_from("<III", dump, directory + i * 12)[0]:
                   struct.unpack_from("<III", dump, directory + i * 12)[2] for i in range(count)}
        self.assertTrue({3, 4, 6}.issubset(streams), (streams, report))  # threads, modules, exception
        self.assertEqual(struct.unpack_from("<I", dump, streams[6] + 8)[0], 0xC0000409)
        self.assertTrue(list(self.directory.glob("*.modules.jsonl")))
        self.assertTrue(list(self.directory.glob("*.build.json")))
        self.assertTrue(list(self.directory.glob("*.complete")))

    def test_normal_exit_leaves_no_crash_bundle(self):
        child, collector = self.launch("pass\n")
        self.assertEqual(child.wait(timeout=25), 0)
        collector.close()
        self.assertEqual(list(self.directory.glob("worker-*")), [])

    def test_handled_first_chance_exception_is_not_a_crash(self):
        child, collector = self.launch('''
import ctypes
try:
    ctypes.WinDLL("kernel32").RaiseException(0xE0123456, 0, 0, None)
except OSError:
    pass
''')
        self.assertEqual(child.wait(timeout=25), 0)
        collector.close()
        self.assertEqual(list(self.directory.glob("*.dmp")), [])

    def test_collector_exit_does_not_kill_worker(self):
        child, collector = self.launch("import time; time.sleep(2)\n")
        self.assertEqual(collector.kernel.WaitForSingleObject(collector.event, 15000), 0)
        collector.process.terminate()
        self.assertEqual(child.wait(timeout=10), 0)

    def test_bundled_library_runtime_versions_match_source(self):
        directory = native_directory()
        verify_bundle(directory, APP.parent)
        with os.add_dll_directory(str(directory)):
            library = ctypes.CDLL(str(directory / "aopipeline.dll"))
            for name in ("aocache", "aodecode", "aodds"):
                source = (APP / "aopipeline" / (name + ".c")).read_text(encoding="utf-8")
                expected = re.search(r'#define ' + name.upper() + r'_VERSION "([^"]+)"', source).group(1)
                version = getattr(library, name + "_version")
                version.restype = ctypes.c_char_p
                self.assertTrue(version().decode().startswith(name + " " + expected))


if __name__ == "__main__":
    unittest.main()
