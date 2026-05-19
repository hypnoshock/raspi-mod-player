"""Floppy / fallback playlist source state machine.

Mirrors the behaviour of the old mod_monitor.sh: when /dev/sda appears
and has tracker files, switch to it; otherwise fall back to a fixed
local mods directory. Reshuffles on source change or on wrap.
"""
import glob
import os
import random
import subprocess

EXTS = {'.mod', '.xm', '.s3m', '.it', '.mptm', '.mtm', '.mo3', '.mt2', '.med', '.ams', '.dbm', '.dmf', '.dtm', '.ult', '.symmod', '.okt'}

FLOPPY_MOUNT = os.path.expanduser('~/floppy')
FALLBACK_DIR = os.path.expanduser('~/mod_player/mods')

# How many polite umount failures we tolerate before falling back to
# `umount -l`. A stale mount happens when a USB stick is yanked while
# something is still touching it; the kernel won't release the mount
# entry, which means the next stick the user plugs in shows up as
# /dev/sdb instead of /dev/sda and we never see it.
UMOUNT_LAZY_AFTER = 3


def _find_usb_block_device():
    """Return the path of a USB mass-storage block device to mount, or None.

    The kernel hands out /dev/sda, /dev/sdb, ... in plug order, so a stale
    mount of an earlier-plugged stick can push a fresh stick to /dev/sdb.
    Walk all /dev/sd[a-z] devices and pick the first one present. Prefer the
    first partition (/dev/sdX1) since modern sticks are partitioned; fall
    back to the whole disk if there's no partition table."""
    for whole in sorted(glob.glob('/dev/sd[a-z]')):
        if not os.path.exists(whole):
            continue
        partitions = sorted(glob.glob(whole + '[0-9]*'))
        return partitions[0] if partitions else whole
    return None

SOURCE_FLOPPY = 'floppy'
SOURCE_FALLBACK = 'fallback'
SOURCE_EMPTY = 'empty'


def _is_tracker_file(name):
    # Skip dotfiles, including macOS AppleDouble resource forks (`._foo.mod`)
    # which share the extension but aren't valid modules and freeze libopenmpt
    # on load.
    if name.startswith('.'):
        return False
    return os.path.splitext(name)[1].lower() in EXTS


def _prune_dotdirs(dirnames):
    """Mutate dirnames in place to drop hidden directories. macOS USB sticks
    carry `.Trashes`, `.Spotlight-V100`, `.fseventsd`, etc. that contain real
    .mod files we don't want to play (deleted-to-trash or system metadata)."""
    dirnames[:] = [d for d in dirnames if not d.startswith('.')]


def _find_tracker_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        _prune_dotdirs(dirnames)
        for name in filenames:
            if _is_tracker_file(name):
                out.append(os.path.join(dirpath, name))
    return out


def _has_any_tracker_file(root):
    """Cheap 'does this tree contain a usable module?' check — stops on the
    first match. _find_tracker_files() walks every directory and stats every
    file (~hundreds of syscalls on a real floppy); detect_source() only needs
    a yes/no, and is called on a 2-second poll, so the difference shows up as
    measurable steady-state CPU."""
    for _dirpath, dirnames, filenames in os.walk(root):
        _prune_dotdirs(dirnames)
        for name in filenames:
            if _is_tracker_file(name):
                return True
    return False


def _mounted_source(path):
    """Return the source device currently mounted at `path`, or None."""
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path:
                    return parts[0]
    except OSError:
        pass
    return None


def _is_mounted(path):
    return _mounted_source(path) is not None


class Playlist:
    """Owned by the main thread. Not thread-safe."""

    def __init__(self):
        self.source = None       # SOURCE_* string
        self.files = []
        self.index = 0
        # Bumped on every reshuffle so the daemon can flag "Shuffling" in
        # the state for the display to show.
        self.shuffles = 0
        # Polite-umount failure counter; escalates to `umount -l` after
        # UMOUNT_LAZY_AFTER consecutive failures.
        self._umount_fails = 0
        # Reseed shuffle each run so reboots don't replay the same order.
        self._rng = random.Random()

    def detect_source(self):
        """Return desired source given current disk state. Idempotent."""
        device = _find_usb_block_device()
        currently_mounted = _mounted_source(FLOPPY_MOUNT)
        if device is not None:
            # If something *else* is mounted there (e.g. the previous stick
            # got yanked and its mount entry is stale), force-detach it
            # before mounting the new stick. A polite umount would fail with
            # "target is busy"; lazy umount always succeeds.
            if currently_mounted is not None and currently_mounted != device:
                self._unmount(lazy=True)
                currently_mounted = _mounted_source(FLOPPY_MOUNT)
            if currently_mounted is None:
                os.makedirs(FLOPPY_MOUNT, exist_ok=True)
                try:
                    subprocess.run(
                        ['sudo', '/bin/mount', device, FLOPPY_MOUNT],
                        check=False, timeout=10,
                    )
                except subprocess.TimeoutExpired:
                    return SOURCE_FALLBACK
            if _is_mounted(FLOPPY_MOUNT) and _has_any_tracker_file(FLOPPY_MOUNT):
                self._umount_fails = 0  # we have a clean mount now
                return SOURCE_FLOPPY
        else:
            # No USB stick visible. Unmount any stale entry so a freshly
            # plugged stick gets /dev/sda again and we see it next poll.
            if currently_mounted is not None:
                self._unmount(lazy=self._umount_fails >= UMOUNT_LAZY_AFTER)
        if os.path.isdir(FALLBACK_DIR) and _has_any_tracker_file(FALLBACK_DIR):
            return SOURCE_FALLBACK
        return SOURCE_EMPTY

    def _unmount(self, lazy=False):
        cmd = ['sudo', '/bin/umount']
        if lazy:
            cmd.append('-l')
        cmd.append(FLOPPY_MOUNT)
        try:
            result = subprocess.run(cmd, check=False, timeout=10)
        except subprocess.TimeoutExpired:
            self._umount_fails += 1
            return
        if result.returncode == 0:
            self._umount_fails = 0
        else:
            self._umount_fails += 1

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
        self.shuffles += 1
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
            self.shuffles += 1
            self.index = 0
        elif self.index < 0:
            self.index = len(self.files) - 1
        return self.current()
