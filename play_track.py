#!/usr/bin/env python3
"""Wrap a single `xmp -R --loop-all FILES...` invocation so its UI still
renders to the screen session and button keystrokes still reach it,
while parsing xmp's output to publish per-track state at
/tmp/mod_state.json.

This is intentionally a single xmp instance handling its own playlist:
xmp owns navigation (n/p/q/arrow keys) so the GPIO -> UInput buttons
work exactly as they did before the display feature existed. The wrapper
only observes xmp's output — it never spawns multiple xmp processes.

State file format:

    {
      "file":        "/path/to/track.mod",
      "title":       "boost",
      "format":      "Protracker M.K.",
      "duration_s":  317,
      "started_at":  1715432104.31
    }

`started_at` is reset whenever xmp prints a fresh `Loading <file> (N of M)`
line, so the display's wall-clock progress bar stays aligned with track
changes (including user-initiated next/prev jumps).
"""
import errno
import fcntl
import json
import os
import pty
import re
import select
import sys
import termios
import time

STATE_PATH = '/tmp/mod_state.json'
PARSE_BUF_MAX = 16384

LOAD_RE = re.compile(rb'^\s*Loading\s+(.+?)\s+\(\d+\s+of\s+\d+\)\s*\r?$', re.M)
TITLE_RE = re.compile(rb'^\s*Module name\s*:\s*(.+?)\r?$', re.M)
TYPE_RE = re.compile(rb'^\s*Module type\s*:\s*(.+?)\r?$', re.M)
DUR_RE = re.compile(rb'^\s*Duration\s*:\s*(\d+)min(\d+)s', re.M)


def write_state(state):
    tmp = STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.rename(tmp, STATE_PATH)
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        sys.stderr.write('usage: play_track.py FILE [FILE...]\n')
        sys.exit(2)
    files = sys.argv[1:]

    state = {
        'file': files[0],
        'title': None,
        'format': None,
        'duration_s': None,
        'started_at': time.time(),
    }
    write_state(state)

    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else -1
    old_tc = None
    if stdin_fd >= 0:
        try:
            old_tc = termios.tcgetattr(stdin_fd)
        except termios.error:
            old_tc = None

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvp('xmp', ['xmp', '-R', '--loop-all', *files])
        except OSError as e:
            sys.stderr.write(f'failed to exec xmp: {e}\n')
            os._exit(127)

    try:
        if stdin_fd >= 0:
            ws = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b'\0' * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
    except OSError:
        pass

    if old_tc is not None:
        try:
            new_tc = termios.tcgetattr(stdin_fd)
            new_tc[3] &= ~(termios.ICANON | termios.ECHO)
            new_tc[6][termios.VMIN] = 1
            new_tc[6][termios.VTIME] = 0
            termios.tcsetattr(stdin_fd, termios.TCSANOW, new_tc)
        except termios.error:
            pass

    stdout_fd = 1
    parse_buf = bytearray()
    # Track which file is currently being parsed so we know when the next
    # Module name/type/Duration lines belong to a different track.
    current_file = files[0]

    def reset_for_new_track(new_file):
        nonlocal current_file
        current_file = new_file
        state['file'] = new_file
        state['title'] = None
        state['format'] = None
        state['duration_s'] = None
        state['started_at'] = time.time()
        write_state(state)

    poll_fds = [master_fd]
    if stdin_fd >= 0:
        poll_fds.append(stdin_fd)

    child_done = False
    try:
        while not child_done:
            try:
                r, _, _ = select.select(poll_fds, [], [], 0.25)
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno in (errno.EBADF, errno.EINTR):
                    break
                raise

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        data = b''
                    else:
                        raise
                if not data:
                    break
                try:
                    os.write(stdout_fd, data)
                except OSError:
                    pass

                parse_buf.extend(data)
                if len(parse_buf) > PARSE_BUF_MAX:
                    del parse_buf[: len(parse_buf) - PARSE_BUF_MAX]

                # Track changes — process from oldest to newest so we don't
                # miss any if multiple Loading lines arrive in one chunk.
                changed = False
                last_load = None
                for m in LOAD_RE.finditer(parse_buf):
                    last_load = m.group(1).decode('utf-8', 'replace').strip()
                if last_load and last_load != current_file:
                    reset_for_new_track(last_load)
                    changed = True
                    # Re-scan for fresh title/type/duration AFTER the Loading line.
                    # Drop everything before this Loading line so we don't pick
                    # up stale Module name lines from prior tracks.
                    load_pos = parse_buf.rfind(b'Loading')
                    if load_pos > 0:
                        del parse_buf[:load_pos]

                if state['title'] is None:
                    m = TITLE_RE.search(parse_buf)
                    if m:
                        state['title'] = m.group(1).decode('utf-8', 'replace').strip()
                        changed = True
                if state['format'] is None:
                    m = TYPE_RE.search(parse_buf)
                    if m:
                        state['format'] = m.group(1).decode('utf-8', 'replace').strip()
                        changed = True
                if state['duration_s'] is None:
                    m = DUR_RE.search(parse_buf)
                    if m:
                        state['duration_s'] = int(m.group(1)) * 60 + int(m.group(2))
                        changed = True
                if changed:
                    write_state(state)

            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 256)
                except OSError:
                    data = b''
                if data:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        pass

            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid != 0:
                    child_done = True
            except ChildProcessError:
                child_done = True

        try:
            while True:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(stdout_fd, data)
                except OSError:
                    break
        except OSError:
            pass

    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if old_tc is not None and stdin_fd >= 0:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSANOW, old_tc)
            except termios.error:
                pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == '__main__':
    main()
