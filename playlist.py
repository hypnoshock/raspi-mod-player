"""Floppy / fallback playlist source state machine.

Mirrors the behaviour of the old mod_monitor.sh: when /dev/sda appears
and has tracker files, switch to it; otherwise fall back to a fixed
local mods directory. Reshuffles on source change or on wrap.
"""
import os
import random
import subprocess

EXTS = {'.mod', '.xm', '.s3m', '.it'}

FLOPPY_DEV = '/dev/sda'
FLOPPY_MOUNT = os.path.expanduser('~/floppy')
FALLBACK_DIR = os.path.expanduser('~/mod_player/mods')

SOURCE_FLOPPY = 'floppy'
SOURCE_FALLBACK = 'fallback'
SOURCE_EMPTY = 'empty'


def _find_tracker_files(root):
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in EXTS:
                out.append(os.path.join(dirpath, name))
    return out


def _is_mounted(path):
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path:
                    return True
    except OSError:
        pass
    return False


class Playlist:
    """Owned by the main thread. Not thread-safe."""

    def __init__(self):
        self.source = None       # SOURCE_* string
        self.files = []
        self.index = 0
        # Reseed shuffle each run so reboots don't replay the same order.
        self._rng = random.Random()

    def detect_source(self):
        """Return desired source given current disk state. Idempotent."""
        if os.path.exists(FLOPPY_DEV):
            if not _is_mounted(FLOPPY_MOUNT):
                os.makedirs(FLOPPY_MOUNT, exist_ok=True)
                # Same shape as the old mod_monitor.sh sudoers usage.
                try:
                    subprocess.run(
                        ['sudo', '/bin/mount', FLOPPY_DEV, FLOPPY_MOUNT],
                        check=False, timeout=10,
                    )
                except subprocess.TimeoutExpired:
                    return SOURCE_FALLBACK
            if _is_mounted(FLOPPY_MOUNT) and _find_tracker_files(FLOPPY_MOUNT):
                return SOURCE_FLOPPY
        else:
            # Disk gone — try to unmount if we still have it mounted.
            if _is_mounted(FLOPPY_MOUNT):
                subprocess.run(
                    ['sudo', '/bin/umount', FLOPPY_MOUNT],
                    check=False, timeout=10,
                )
        if os.path.isdir(FALLBACK_DIR) and _find_tracker_files(FALLBACK_DIR):
            return SOURCE_FALLBACK
        return SOURCE_EMPTY

    def reload(self, source):
        """Switch to a new source. Returns True if anything changed."""
        if source == self.source:
            return False
        self.source = source
        if source == SOURCE_FLOPPY:
            self.files = _find_tracker_files(FLOPPY_MOUNT)
        elif source == SOURCE_FALLBACK:
            self.files = _find_tracker_files(FALLBACK_DIR)
        else:
            self.files = []
        self._rng.shuffle(self.files)
        self.index = 0
        return True

    def current(self):
        if not self.files:
            return None
        return self.files[self.index]

    def advance(self, delta=1):
        """Move by +/-delta. Wraps and reshuffles on forward wrap."""
        if not self.files:
            return None
        self.index += delta
        if self.index >= len(self.files):
            self._rng.shuffle(self.files)
            self.index = 0
        elif self.index < 0:
            self.index = len(self.files) - 1
        return self.current()
