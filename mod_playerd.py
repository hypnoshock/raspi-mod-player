#!/usr/bin/env python3
"""mod_playerd — single-process tracker module player for the Pi Zero W.

Replaces the old xmp + screen + pty + uinput chain with a daemon that
owns playback directly via libopenmpt, reads GPIO 6/26 buttons, exposes
a Unix-socket control surface, and writes /tmp/mod_state.json for the
ST7789 display daemon.

Threading model:
  main        — owns the loaded Module; dispatches commands; ticks 1Hz
  renderer    — calls Module.read_stereo; pushes int16 PCM bytes
  audio out   — sounddevice RawOutputStream; pulls bytes; writes
  control     — ThreadingUnixStreamServer on /tmp/modplayer.sock
  gpiozero    — its own internal thread; callbacks push to command queue
"""
import ctypes
import json
import os
import queue
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback

# Force gpiozero to use lgpio (via /dev/gpiochip0, group=gpio) instead of
# RPi.GPIO (via /dev/mem, requires root). Must be set before gpiozero import.
os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'lgpio')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sounddevice as sd
from gpiozero import Button

from openmpt import Module, OpenMPTError
from playlist import Playlist, SOURCE_EMPTY

SAMPLERATE = 44100
FRAMES_PER_CHUNK = 1024
CHANNELS = 2
AUDIO_QUEUE_DEPTH = 4
AUDIO_DEVICE = 'USB Audio CODEC'

STATE_PATH = '/tmp/mod_state.json'
SOCKET_PATH = '/tmp/modplayer.sock'

GPIO_NEXT = 26
GPIO_PREV = 6

# Short press = track skip; hold >= BUTTON_HOLD_S = seek at SEEK_STEP_S per
# repeat tick (gpiozero fires when_held every BUTTON_HOLD_S while held).
BUTTON_HOLD_S = 0.5
SEEK_STEP_S = 5

# Pattern background snapshot — included in /tmp/mod_state.json for the
# display's faint scrolling pattern view.
PATTERN_WINDOW_ROWS = 17        # odd so there is a true centre row
PATTERN_MAX_CHANNELS = 6        # truncate wide modules; first N channels only
STATE_WRITE_THROTTLE_S = 0.05   # target ~20Hz state writes for smooth pattern scroll


# ---------------------------------------------------------------------------
# Renderer + audio threads
# ---------------------------------------------------------------------------

class _SkipChunk:
    """Sentinel pushed into the audio queue to signal 'drop everything queued
    and resume from whatever I push next' (used on seek/next/prev)."""


class Player:
    """Owns the Module pointer and the renderer/audio threads. Methods are
    called from the main thread only."""

    def __init__(self, on_track_end):
        self._on_track_end = on_track_end
        self._module = None
        self._paused = False
        # Cache formatted pattern rows per pattern_id for the current track.
        # Patterns are static within a track; formatting once and slicing
        # windows is much cheaper than calling libopenmpt 60+ times per tick.
        self._pattern_cache = {}
        # Row-start tracking for smooth display interpolation between updates.
        self._last_row_key = None   # (pattern, row)
        self._row_start_at = 0.0    # wall-clock time when current row began
        self._render_buf = (ctypes.c_int16 * (FRAMES_PER_CHUNK * CHANNELS))()
        self._audio_q = queue.Queue(maxsize=AUDIO_QUEUE_DEPTH)
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLERATE,
            blocksize=FRAMES_PER_CHUNK,
            device=AUDIO_DEVICE,
            channels=CHANNELS,
            dtype='int16',
        )
        self._stream.start()
        self._render_t = threading.Thread(
            target=self._render_loop, name='render', daemon=True)
        self._audio_t = threading.Thread(
            target=self._audio_loop, name='audio', daemon=True)
        self._render_t.start()
        self._audio_t.start()

    # --- public API (main thread only) -------------------------------------

    def load(self, path):
        """Switch to a new module. Old one is destroyed."""
        try:
            new_mod = Module(path)
        except (OpenMPTError, OSError) as e:
            print(f'[player] failed to load {path}: {e}', file=sys.stderr)
            self._module = None
            self._pattern_cache = {}
            return False
        self._flush_audio()
        old = self._module
        self._module = new_mod
        self._pattern_cache = {}
        self._last_row_key = None
        self._row_start_at = 0.0
        if old is not None:
            old.close()
        return True

    def unload(self):
        self._flush_audio()
        if self._module is not None:
            self._module.close()
            self._module = None
        self._pattern_cache = {}
        self._last_row_key = None
        self._row_start_at = 0.0

    def seek(self, seconds):
        if self._module is None:
            return 0.0
        self._flush_audio()
        return self._module.seek(max(0.0, seconds))

    def pause(self):
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    def is_paused(self):
        return self._pause_event.is_set()

    def position(self):
        return self._module.position() if self._module else 0.0

    def duration(self):
        return self._module.duration() if self._module else 0.0

    def title(self):
        return self._module.title if self._module else ''

    def type_long(self):
        return self._module.type_long if self._module else ''

    def pattern_snapshot(self, window, max_channels):
        """Return a window of formatted note rows centred on the current
        playhead, plus surrounding metadata, or None if no module loaded.

        Includes `row_start_at` (wall clock when this row began) and
        `row_duration_s` (estimated seconds per row) so the display can
        smoothly interpolate the scroll position between state updates."""
        m = self._module
        if m is None:
            return None
        try:
            cur_pat = m.current_pattern()
            cur_row = m.current_row()
            cur_order = m.current_order()
            num_rows = m.pattern_num_rows(cur_pat)
            num_channels = m.num_channels()
            speed = m.current_speed()
            bpm = m.current_bpm()
        except Exception:
            return None
        if num_rows <= 0 or num_channels <= 0:
            return None

        # Row-start timestamp tracking. When row (or pattern) changes,
        # stamp the wall-clock time so the display knows when this row
        # began and can interpolate its scroll position.
        row_key = (cur_pat, cur_row)
        if row_key != self._last_row_key:
            self._row_start_at = time.time()
            self._last_row_key = row_key
        # Row duration formula: 2.5 * speed / BPM seconds (classic tracker).
        if bpm > 0 and speed > 0:
            row_duration_s = 2.5 * speed / bpm
        else:
            row_duration_s = 0.05  # fallback; display will clamp

        chs = min(num_channels, max_channels)
        cached = self._pattern_cache.get(cur_pat)
        if cached is None or len(cached) != num_rows or (cached and len(cached[0]) != chs):
            cached = [
                [m.format_cell_note(cur_pat, r, c) for c in range(chs)]
                for r in range(num_rows)
            ]
            self._pattern_cache[cur_pat] = cached

        half = window // 2
        lo = cur_row - half
        hi = lo + window
        rows = []
        for r in range(lo, hi):
            if 0 <= r < num_rows:
                rows.append(cached[r])
            else:
                rows.append([''] * chs)
        return {
            'order':          cur_order,
            'pattern':        cur_pat,
            'row':            cur_row,
            'num_rows':       num_rows,
            'num_channels':   num_channels,
            'rows':           rows,
            'current_idx':    half,
            'row_start_at':   self._row_start_at,
            'row_duration_s': row_duration_s,
        }

    def shutdown(self):
        self._stop_event.set()
        self._pause_event.clear()
        self._flush_audio()
        # Give the audio + render threads a beat to notice the stop flag
        # before we yank the stream out from under them (otherwise PortAudio
        # complains loudly during a concurrent write).
        time.sleep(0.2)
        try:
            self._audio_t.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._render_t.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    # --- internals ---------------------------------------------------------

    def _flush_audio(self):
        """Drain pending PCM; on next render we'll start fresh."""
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass
        # Mark a skip so any chunk currently being written gets a bookmark.
        try:
            self._audio_q.put_nowait(_SkipChunk)
        except queue.Full:
            pass

    def _render_loop(self):
        while not self._stop_event.is_set():
            if self._module is None or self._pause_event.is_set():
                time.sleep(0.05)
                continue
            try:
                n = self._module.read_stereo(
                    SAMPLERATE, FRAMES_PER_CHUNK, self._render_buf)
            except Exception:
                traceback.print_exc()
                time.sleep(0.1)
                continue
            if n == 0:
                # End of track. Tell main and stop pushing audio until it
                # swaps in a new module.
                self._on_track_end()
                # Wait until module changes or stop is requested. Cheap poll.
                cur = self._module
                while (not self._stop_event.is_set()
                       and self._module is cur
                       and self._module is not None):
                    time.sleep(0.05)
                continue
            chunk = bytes(self._render_buf)[: n * CHANNELS * 2]
            try:
                self._audio_q.put(chunk, timeout=1.0)
            except queue.Full:
                # Audio thread stuck — drop the chunk rather than block forever.
                pass

    def _audio_loop(self):
        while not self._stop_event.is_set():
            try:
                chunk = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is _SkipChunk or not chunk:
                continue
            try:
                self._stream.write(chunk)
            except Exception:
                traceback.print_exc()


# ---------------------------------------------------------------------------
# Control socket
# ---------------------------------------------------------------------------

class _CmdHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        verb, _, args = line.decode('utf-8', 'replace').strip().partition(' ')
        if not verb:
            return
        reply_q = queue.Queue(maxsize=1)
        self.server.cmd_q.put((verb.lower(), args.strip(), reply_q))
        try:
            reply = reply_q.get(timeout=5.0)
        except queue.Empty:
            reply = 'err: timeout\n'
        try:
            self.wfile.write(reply.encode('utf-8'))
        except OSError:
            pass


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, cmd_q):
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, _CmdHandler)
        os.chmod(path, 0o666)
        self.cmd_q = cmd_q


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.cmd_q = queue.Queue()
        self.playlist = Playlist()
        self.player = Player(on_track_end=lambda: self.cmd_q.put(
            ('_track_end', '', None)))
        self._stop = False
        self._last_state_write = 0.0
        self._last_floppy_poll = 0.0

        # GPIO buttons. Short press → track skip (on release, if not held).
        # Hold ≥ BUTTON_HOLD_S → fire seek every BUTTON_HOLD_S until released.
        self._btn_next = Button(
            GPIO_NEXT, bounce_time=0.1,
            hold_time=BUTTON_HOLD_S, hold_repeat=True)
        self._btn_prev = Button(
            GPIO_PREV, bounce_time=0.1,
            hold_time=BUTTON_HOLD_S, hold_repeat=True)
        self._next_was_held = False
        self._prev_was_held = False
        self._wire_button(self._btn_next, 'next', +SEEK_STEP_S, '_next_was_held')
        self._wire_button(self._btn_prev, 'prev', -SEEK_STEP_S, '_prev_was_held')

        self._server = _UnixServer(SOCKET_PATH, self.cmd_q)
        self._server_t = threading.Thread(
            target=self._server.serve_forever, name='control', daemon=True)
        self._server_t.start()

    def _wire_button(self, button, track_verb, seek_step, held_attr):
        def on_pressed():
            setattr(self, held_attr, False)

        def on_held():
            setattr(self, held_attr, True)
            self.cmd_q.put(('seek', f'{seek_step:+d}', None))

        def on_released():
            if not getattr(self, held_attr):
                self.cmd_q.put((track_verb, '', None))

        button.when_pressed = on_pressed
        button.when_held = on_held
        button.when_released = on_released

    # --- command dispatch --------------------------------------------------

    def _do_next(self):
        path = self.playlist.advance(+1)
        self._play_current(path)
        return 'ok\n'

    def _do_prev(self):
        path = self.playlist.advance(-1)
        self._play_current(path)
        return 'ok\n'

    def _do_pause(self):
        self.player.pause()
        self._write_state()
        return 'ok\n'

    def _do_resume(self):
        self.player.resume()
        self._write_state()
        return 'ok\n'

    def _do_seek(self, args):
        if not args:
            return 'err: seek needs an argument\n'
        try:
            n = float(args)
        except ValueError:
            return f'err: bad seek arg {args!r}\n'
        if args.startswith(('+', '-')):
            target = self.player.position() + n
        else:
            target = n
        actual = self.player.seek(target)
        self._write_state(force=True)
        return f'ok {actual:.3f}\n'

    def _do_status(self):
        return json.dumps(self._state_dict()) + '\n'

    def _do_quit(self):
        self._stop = True
        return 'ok\n'

    def _do_track_end(self):
        # Renderer hit end-of-track. Advance.
        path = self.playlist.advance(+1)
        self._play_current(path)
        return None

    DISPATCH = {
        'next':       lambda self, _args: self._do_next(),
        'prev':       lambda self, _args: self._do_prev(),
        'pause':      lambda self, _args: self._do_pause(),
        'resume':     lambda self, _args: self._do_resume(),
        'seek':       lambda self, args: self._do_seek(args),
        'status':     lambda self, _args: self._do_status(),
        'quit':       lambda self, _args: self._do_quit(),
        '_track_end': lambda self, _args: self._do_track_end(),
    }

    # --- state writer ------------------------------------------------------

    def _state_dict(self):
        elapsed = self.player.position()
        d = {
            'file':       self.playlist.current(),
            'title':      self.player.title(),
            'format':     self.player.type_long(),
            'duration_s': int(self.player.duration()),
            'elapsed_s':  round(elapsed, 2),
            # Back-derive started_at so mod_display.py's existing wall-clock
            # math just works without modification.
            'started_at': time.time() - elapsed,
            'paused':     self.player.is_paused(),
            'source':     self.playlist.source,
        }
        pat = self.player.pattern_snapshot(
            PATTERN_WINDOW_ROWS, PATTERN_MAX_CHANNELS)
        if pat is not None:
            d['pattern'] = pat
        return d

    def _write_state(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_state_write < STATE_WRITE_THROTTLE_S:
            return
        sd = self._state_dict()
        self._last_state_write = now
        tmp = STATE_PATH + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(sd, f)
            os.rename(tmp, STATE_PATH)
        except OSError:
            pass

    # --- playback control --------------------------------------------------

    def _play_current(self, path):
        if path is None:
            self.player.unload()
        else:
            ok = self.player.load(path)
            # If loading failed, skip forward.
            if not ok:
                nxt = self.playlist.advance(+1)
                if nxt and nxt != path:
                    self.player.load(nxt)
        self._write_state(force=True)

    # --- floppy polling ----------------------------------------------------

    def _poll_floppy(self):
        now = time.monotonic()
        if now - self._last_floppy_poll < 2.0:
            return
        self._last_floppy_poll = now
        desired = self.playlist.detect_source()
        if desired != self.playlist.source:
            self.playlist.reload(desired)
            if desired == SOURCE_EMPTY:
                self.player.unload()
            else:
                self._play_current(self.playlist.current())

    # --- main loop ---------------------------------------------------------

    def run(self):
        # Initial source select + first track.
        desired = self.playlist.detect_source()
        self.playlist.reload(desired)
        if desired != SOURCE_EMPTY:
            self._play_current(self.playlist.current())
        else:
            self._write_state(force=True)

        while not self._stop:
            try:
                # Timeout doubles as the tick interval. STATE_WRITE_THROTTLE_S
                # sets the cap on write frequency; this matches so the loop
                # wakes up often enough for the throttle to actually fire.
                verb, args, reply_q = self.cmd_q.get(timeout=STATE_WRITE_THROTTLE_S)
            except queue.Empty:
                self._write_state()
                self._poll_floppy()
                continue
            try:
                handler = self.DISPATCH.get(verb)
                if handler is None:
                    reply = f'err: unknown verb {verb!r}\n'
                else:
                    reply = handler(self, args)
            except Exception:
                traceback.print_exc()
                reply = 'err: internal\n'
            if reply is not None and reply_q is not None:
                try:
                    reply_q.put_nowait(reply)
                except queue.Full:
                    pass
            self._write_state()
            self._poll_floppy()

        self.shutdown()

    def shutdown(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        self.player.shutdown()


def main():
    daemon = Daemon()

    def on_term(_signum, _frame):
        daemon._stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    daemon.run()


if __name__ == '__main__':
    main()
