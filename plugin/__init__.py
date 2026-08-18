"""
Konnect -- Action Plugin launcher.

This thin Python plugin registers a menu item in KiCAD's Tools menu.
Clicking it opens a settings dialog where users can configure paths
and start/stop the compiled Rust MCP server binary.

Installation (KiCAD PCM):
  The plugin appears in KiCAD's Plugin and Content Manager.
  After installation the binary is placed in the plugin directory.
  KiCAD loads this __init__.py automatically.
"""

import atexit
import os
import shutil
import subprocess
import sys
import threading

import pcbnew  # Available inside KiCAD

# Import the settings dialog (same directory)
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from settings_dialog import KonnectSettingsDialog, load_settings
import wx

PLUGIN_DIR = _plugin_dir
BINARY_NAME = "konnect.exe" if sys.platform == "win32" else "konnect"
BINARY_PATH = os.path.join(PLUGIN_DIR, "bin", BINARY_NAME)
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

_server_process = None
_server_thread = None

# Windows GUI parents (pcbnew) attach a new console to their subprocesses.
# That console makes stdin look like a TTY, which triggers konnect's
# double-click install wizard instead of MCP server mode. CREATE_NO_WINDOW
# suppresses the console; stdin=PIPE stays intact.
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.cache")),
    "konnect", "cache",
)
_PID_FILE = os.path.join(_CACHE_DIR, "server.pid")


def _stage(source_path):
    """Copy file to LOCALAPPDATA cache, return cached path.

    OneDrive-redirected Documents (common in enterprise Windows) tag both
    the plugin's konnect.exe AND settings.json with IO_REPARSE_TAG_CLOUD,
    which trips ERROR_ACCESS_DENIED on execute and on serde config read.
    Staging to %LOCALAPPDATA% (never OneDrive-synced) sidesteps it.
    """
    if sys.platform != "win32":
        return source_path
    os.makedirs(_CACHE_DIR, exist_ok=True)
    dst = os.path.join(_CACHE_DIR, os.path.basename(source_path))
    shutil.copy2(source_path, dst)
    return dst


def _owner_path():
    """Sibling of the PID file recording which session spawned that server.

    Derived from _PID_FILE rather than kept as its own global so that both
    files always live in the same directory -- including under tests, which
    repoint _PID_FILE at a temporary directory.
    """
    return _PID_FILE + ".owner"


def _pid_alive(pid):
    """Best-effort "is this PID still running", used to tell an orphaned
    server from one a live session is using.

    Unknown counts as alive: the caller only kills what it believes is dead,
    and killing a server somebody is working through is worse than leaving a
    stale one holding the port, which surfaces as a visible start failure.

    os.kill(pid, 0) is not a probe on Windows (see _kill_tracked), so query
    the task list there instead.
    """
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            listing = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                creationflags=_POPEN_FLAGS,
            ).stdout
        except OSError:
            return True
        return '"%d"' % pid in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: alive, owned by another user. Anything else: don't guess dead.
        return True
    return True


def _read_owner():
    """Return (server_pid, owner_pid) from the owner record, or None.

    The record names the server it belongs to, so a file left behind by an
    older server -- or by a version that never wrote one -- is detected and
    ignored rather than mistaken for the current server's owner.
    """
    try:
        with open(_owner_path()) as f:
            server_pid, owner_pid = f.read().split()
        return int(server_pid), int(owner_pid)
    except (OSError, ValueError):
        return None


def _write_pid_file(pid):
    """Record the PID of the server this session just spawned, and ours.

    The owner half goes in a sibling file so that the format of server.pid
    stays a bare integer: an install upgraded in place keeps working, and a
    reader that predates this change is unaffected.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_PID_FILE, "w") as f:
        f.write(str(pid))
    with open(_owner_path(), "w") as f:
        f.write("%d %d" % (pid, os.getpid()))


def _clear_pid_file(pid):
    """Remove the PID file only while it still records ``pid``.

    Every KiCAD session writes the same file. Removing it unconditionally
    lets the session whose server exits last delete a record written by a
    session whose server is still running: the survivor becomes untracked,
    so no later start_server preflight can ever reap it (#103).
    """
    try:
        with open(_PID_FILE) as f:
            recorded = int(f.read().strip())
    except (OSError, ValueError):
        return
    if recorded != pid:
        return
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass
    _drop_owner(pid)


def _drop_owner(pid=None):
    """Remove the owner record, unless it belongs to a different server.

    ``pid=None`` means "whatever is recorded", used on the paths that kill the
    recorded server outright.
    """
    owner = _read_owner()
    if owner is not None and pid is not None and owner[0] != pid:
        return
    try:
        os.remove(_owner_path())
    except OSError:
        pass


def _terminate_owned_server():
    """Kill the server this session spawned, on KiCAD exit.

    _run_server sits on a daemon thread, so interpreter shutdown drops the
    thread without touching the child. The server then outlives KiCAD,
    holding the HTTP port and serving from a possibly pre-update binary --
    an http-transport server never reads stdin, so parent death is invisible
    to it and nothing else ends it.

    Best-effort by nature: a hard kill of KiCAD runs no atexit handler, which
    is why the preflight reap stays.
    """
    process = _server_process
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    except OSError:
        return
    _clear_pid_file(process.pid)


atexit.register(_terminate_owned_server)


def _kill_tracked():
    """Terminate the PID recorded in the PID file (best-effort), clear the file.

    os.kill(pid, 0) is unsafe on Windows -- it invokes TerminateProcess for any
    signal value, so it would actually kill the process instead of probing.
    Use taskkill/kill by PID; if the PID is stale, the kill is a harmless no-op.
    """
    try:
        with open(_PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_POPEN_FLAGS,
            )
        else:
            os.kill(pid, 9)
    except OSError:
        pass
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass
    _drop_owner()


def _reap_orphan_server():
    """Clear a server left behind by a previous session. Report whether a live
    one was found instead.

    The preflight's job is the orphan named in its own comment -- a server
    whose KiCAD session is gone and which is still holding the HTTP port. It
    used to kill whatever PID was recorded, which since the compare-and-delete
    fix (#103) can be the *running* server of a second KiCAD session open right
    now: that session loses its server mid-use, with no message anywhere.

    So kill only what is not in use: either the server itself is gone, or the
    session that spawned it is. A record with no owner half predates this
    change, and its session is long gone, so it is reaped as before.

    Returns True when the recorded server is alive and owned by another live
    session, meaning the caller must leave both it and the record alone.
    """
    try:
        with open(_PID_FILE) as f:
            server_pid = int(f.read().strip())
    except (OSError, ValueError):
        return False

    owner = _read_owner()
    if (
        owner is not None
        and owner[0] == server_pid
        and owner[1] != os.getpid()
        and _pid_alive(owner[1])
        and _pid_alive(server_pid)
    ):
        return True

    _kill_tracked()
    return False


def _run_server():
    """Run the MCP server subprocess. Exit means exit -- no retry loop.

    A retry loop respawns konnect after Stop Server kills it (wait() returns
    normally on external kill, so nothing distinguishes "crashed" from
    "stopped"). If konnect crashes on startup, the user clicks Start again.
    """
    global _server_process
    pid = None
    try:
        args = [_stage(BINARY_PATH)]
        if os.path.exists(SETTINGS_PATH):
            args += ["--config", _stage(SETTINGS_PATH)]
        # pcbnew has no valid stderr; inheriting gives konnect a broken
        # handle and tracing_subscriber errors on init. DEVNULL is a
        # valid sink.
        _server_process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_POPEN_FLAGS,
        )
        pid = _server_process.pid
        _write_pid_file(pid)
        _server_process.wait()
    except Exception as e:
        print(f"[Konnect] Server error: {e}", file=sys.stderr)
    finally:
        _server_process = None
        if pid is not None:
            _clear_pid_file(pid)


def start_server():
    """Start the MCP server in a background thread."""
    global _server_thread

    if not os.path.exists(BINARY_PATH):
        wx.MessageBox(
            f"Konnect binary not found at:\n{BINARY_PATH}\n\n"
            "Please reinstall the plugin.",
            "Konnect",
            wx.OK | wx.ICON_ERROR,
        )
        return False

    # Orphan from a previous KiCAD session may still be holding the port.
    # A server another live session is using is not an orphan: leave it, and
    # do not spawn a second one that would only fail to bind the same port.
    if _reap_orphan_server():
        return True

    if _server_thread and _server_thread.is_alive():
        return True

    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()
    return True


def stop_server():
    """Stop the MCP server subprocess."""
    _kill_tracked()


def is_server_running():
    """Report whether we've launched a server we haven't stopped.

    PID file existence as a proxy -- survives module re-imports. If the
    server crashed on its own, the next start_server preflight cleans up.
    """
    return os.path.exists(_PID_FILE)


# ─── KiCAD Action Plugin entry point ─────────────────────────────────────────

class KonnectPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Konnect"
        self.category = "AI Tools"
        self.description = (
            "Configure and control the Konnect -- enables AI assistants "
            "like Claude to design PCBs and schematics via the Model Context Protocol."
        )
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(PLUGIN_DIR, "resources", "icon.png")

    def Run(self):
        """Open the settings dialog. Server start/stop is handled via dialog buttons."""
        # Get the KiCAD main window as parent for the dialog
        parent = wx.GetTopLevelWindows()[0] if wx.GetTopLevelWindows() else None

        dlg = KonnectSettingsDialog(
            parent=parent,
            plugin_dir=PLUGIN_DIR,
            binary_path=BINARY_PATH,
            server_running=is_server_running(),
        )

        result = dlg.ShowModal()

        if result == wx.ID_YES:
            # User clicked "Start Server"
            start_server()
        elif result == wx.ID_NO:
            # User clicked "Stop Server"
            stop_server()

        dlg.Destroy()


# Register the plugin with KiCAD
KonnectPlugin().register()
