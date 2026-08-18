"""Regression tests for the start_server preflight (follow-up to #199).

The preflight exists to clear an *orphan*: a server whose KiCAD session is
gone and which is still holding the HTTP port. Since #199 made the PID record
survive another session's exit, the record can also name the **running** server
of a second KiCAD session that is open right now -- and killing that one takes
a working server out from under somebody with no message anywhere.

These tests use real child processes rather than invented PID numbers, because
what is under test is precisely the liveness decision.

Run with:  python -m unittest discover -s plugin/tests
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from test_pid_lifecycle import _load_plugin


def _spawn():
    """A real, live process that does nothing until told to stop."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"])


class PreflightReap(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_cache = self.plugin._CACHE_DIR
        self._orig_pid = self.plugin._PID_FILE
        self.plugin._CACHE_DIR = self.tmp.name
        self.plugin._PID_FILE = os.path.join(self.tmp.name, "server.pid")
        self._procs = []

    def tearDown(self):
        for proc in self._procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        self.plugin._CACHE_DIR = self._orig_cache
        self.plugin._PID_FILE = self._orig_pid

    # ── helpers ──────────────────────────────────────────────────────────────

    def live(self):
        proc = _spawn()
        self._procs.append(proc)
        return proc

    def dead_pid(self):
        """A PID that is definitely not running: spawned, killed, reaped."""
        proc = _spawn()
        proc.kill()
        proc.wait(timeout=5)
        return proc.pid

    def record(self, server_pid, owner_pid=None):
        """Write what some other session would have left behind."""
        with open(self.plugin._PID_FILE, "w") as f:
            f.write(str(server_pid))
        if owner_pid is not None:
            with open(self.plugin._owner_path(), "w") as f:
                f.write("%d %d" % (server_pid, owner_pid))

    def still_running(self, proc):
        time.sleep(0.4)
        proc.poll()
        return proc.returncode is None

    # ── the regression ───────────────────────────────────────────────────────

    def test_preflight_spares_a_live_sibling_sessions_server(self):
        """The case the #199 merge note flagged as newly reachable."""
        sibling_session = self.live()
        sibling_server = self.live()
        self.record(sibling_server.pid, owner_pid=sibling_session.pid)

        in_use = self.plugin._reap_orphan_server()

        self.assertTrue(
            self.still_running(sibling_server),
            "a server another live session is using must survive the preflight",
        )
        self.assertTrue(
            in_use, "the preflight must report the server as in use, not reaped"
        )
        self.assertTrue(
            os.path.exists(self.plugin._PID_FILE),
            "the sibling's record must stay, or it goes untracked again (#103)",
        )

    def test_preflight_reaps_a_server_whose_session_is_gone(self):
        """The orphan the preflight was written for: still must be reaped."""
        orphan = self.live()
        self.record(orphan.pid, owner_pid=self.dead_pid())

        in_use = self.plugin._reap_orphan_server()

        self.assertFalse(
            self.still_running(orphan),
            "a server whose session is gone is an orphan and must be killed",
        )
        self.assertFalse(in_use)
        self.assertFalse(os.path.exists(self.plugin._PID_FILE))
        self.assertFalse(os.path.exists(self.plugin._owner_path()))

    def test_preflight_reaps_a_record_with_no_owner_half(self):
        """Written by a version before this change; its session is long gone."""
        orphan = self.live()
        self.record(orphan.pid)  # bare integer, the old format

        self.assertFalse(self.plugin._reap_orphan_server())
        self.assertFalse(
            self.still_running(orphan),
            "an install upgraded in place must still get its orphan reaped",
        )

    def test_preflight_reaps_our_own_stale_server(self):
        """Same session starting again: our own leftover is not somebody's."""
        ours = self.live()
        self.record(ours.pid, owner_pid=os.getpid())

        self.assertFalse(self.plugin._reap_orphan_server())
        self.assertFalse(self.still_running(ours))

    def test_preflight_clears_a_dead_server_recorded_by_a_live_session(self):
        """A crashed server must not be mistaken for one in use, or no session
        could ever start a new one."""
        sibling_session = self.live()
        self.record(self.dead_pid(), owner_pid=sibling_session.pid)

        self.assertFalse(self.plugin._reap_orphan_server())
        self.assertFalse(
            os.path.exists(self.plugin._PID_FILE),
            "a record naming a dead server must be cleared, not preserved",
        )

    def test_an_owner_record_naming_a_different_server_is_ignored(self):
        """Left over from an earlier server; it says nothing about this one."""
        orphan = self.live()
        stale_owner_session = self.live()
        with open(self.plugin._PID_FILE, "w") as f:
            f.write(str(orphan.pid))
        with open(self.plugin._owner_path(), "w") as f:
            f.write("%d %d" % (orphan.pid + 100000, stale_owner_session.pid))

        self.assertFalse(self.plugin._reap_orphan_server())
        self.assertFalse(self.still_running(orphan))

    # ── wiring: the preflight's answer has to reach start_server ─────────────

    def test_start_server_does_not_spawn_over_a_live_sibling(self):
        sibling_session = self.live()
        sibling_server = self.live()
        self.record(sibling_server.pid, owner_pid=sibling_session.pid)

        spawned = []
        self.plugin.BINARY_PATH = sys.executable  # exists, so the guard passes
        self.plugin._run_server = lambda: spawned.append(True)

        self.assertTrue(self.plugin.start_server())
        self.assertEqual(
            spawned, [], "a second server would only fail to bind the same port"
        )
        self.assertTrue(self.still_running(sibling_server))

    # ── record hygiene ───────────────────────────────────────────────────────

    def test_writing_and_clearing_keep_the_two_records_together(self):
        self.plugin._write_pid_file(4242)
        self.assertEqual(
            self.plugin._read_owner(),
            (4242, os.getpid()),
            "the owner half must name both the server and this session",
        )
        self.plugin._clear_pid_file(4242)
        self.assertFalse(os.path.exists(self.plugin._PID_FILE))
        self.assertFalse(
            os.path.exists(self.plugin._owner_path()),
            "a stale owner half would outlive the server it describes",
        )

    def test_clearing_another_sessions_record_leaves_its_owner_half(self):
        self.plugin._write_pid_file(1111)          # session B, still running
        self.plugin._clear_pid_file(2222)          # session A's server exits
        self.assertEqual(self.plugin._read_owner(), (1111, os.getpid()))


if __name__ == "__main__":
    unittest.main()
