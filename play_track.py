#!/usr/bin/env python3
"""Wrap `xmp -R FILE` so the current playback state is published to
/tmp/mod_state.json for the display daemon to render.

xmp runs inside a pseudo-tty; the wrapper fans output back to the
real terminal (so xmp's UI still appears in the screen session),
forwards stdin keypresses to xmp (so the GPIO button -> UInput
keystrokes keep working), and parses the header for title/format/
duration to write into the state file.
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
PARSE_BUF_MAX = 8192

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
        print('usage: play_track.py FILE', file=sys.stderr)
        sys.exit(2)
    filename = sys.argv[1]

    state = {
        'file': filename,
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
            os.execvp('xmp', ['xmp', '-R', filename])
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
    saw_all = False
    child_done = False

    poll_fds = [master_fd]
    if stdin_fd >= 0:
        poll_fds.append(stdin_fd)

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
                if not saw_all:
                    parse_buf.extend(data)
                    if len(parse_buf) > PARSE_BUF_MAX:
                        del parse_buf[: len(parse_buf) - PARSE_BUF_MAX]
                    changed = False
                    if state['title'] is None:
                        m = TITLE_RE.search(parse_buf)
                        if m:
                            state['title'] = m.group(1).decode(
                                'utf-8', 'replace'
                            ).strip()
                            changed = True
                    if state['format'] is None:
                        m = TYPE_RE.search(parse_buf)
                        if m:
                            state['format'] = m.group(1).decode(
                                'utf-8', 'replace'
                            ).strip()
                            changed = True
                    if state['duration_s'] is None:
                        m = DUR_RE.search(parse_buf)
                        if m:
                            state['duration_s'] = (
                                int(m.group(1)) * 60 + int(m.group(2))
                            )
                            changed = True
                    if changed:
                        write_state(state)
                    if (
                        state['title']
                        and state['format']
                        and state['duration_s']
                    ):
                        saw_all = True

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
