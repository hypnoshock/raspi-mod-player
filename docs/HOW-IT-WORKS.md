# How mod_player works — a maintainer's tour

This is the long version of the README. It explains every moving part and the *why* behind the design, so you can change things confidently later.

If you just want to install it or use it, see `../README.md`. If you want to maintain it, modify it, or debug it when it breaks at 11pm — read this.

## 1. The system at a glance

There are **two long-running processes** plus **one CLI tool**:

| Thing | Process? | What it does |
|---|---|---|
| `mod_playerd.py` | Yes — `mod_playerd.service` | Owns playback. Loads tracker modules, renders audio, reads GPIO buttons, accepts commands. |
| `mod_display.py` | Yes — `mod_display.service` | Draws the 240×240 display. |
| `modctl` | No — just a script you run | Sends one command to the daemon and prints the reply. |

The two processes don't call each other directly. They communicate through **one file** (`/tmp/mod_state.json`) that the daemon writes and the display reads. Plain old "leave a note on the fridge." This is deliberate:

- The display can crash and restart without disturbing audio.
- You can stop the display entirely (e.g. for thermals) and the daemon keeps playing.
- A future web UI doesn't need to invent its own way to talk to the display — it just reads the same file.

The daemon also exposes a **second** way to talk to it: a Unix socket (`/tmp/modplayer.sock`) that anyone can connect to and send commands. `modctl`, your GPIO buttons, and (one day) the web UI all funnel through the same socket.

```
                ┌──────────────────────────────┐
                │   mod_playerd.py             │
                │   (mod_playerd.service)      │
                │                              │
   buttons ───▶ │  GPIO 6 / 26                 │
                │                              │
   modctl ───▶  │  /tmp/modplayer.sock         │ ── pushes commands ──▶ playback
   (or any      │  (Unix domain socket)        │                            │
    client)     │                              │                            ▼
                │  libopenmpt + sounddevice ──▶│ ── USB Audio CODEC ─▶ speakers
                │                              │
                │  writes /tmp/mod_state.json  │ ── once a second
                └──────────────────────────────┘
                                                                            │
                                                                            ▼
                                                       ┌────────────────────────────┐
                                                       │ mod_display.py             │
                                                       │ (mod_display.service)      │
                                                       │  - reads the JSON file     │
                                                       │  - draws to ST7789 panel   │
                                                       └────────────────────────────┘
```

## 2. What systemd does

`systemd` is the Pi's process supervisor. It starts both daemons at boot, restarts them if they crash, and gives you a way to start/stop/inspect them by name.

The unit files in `services/` describe each daemon. After install, two files exist at `/etc/systemd/system/`:

- `mod_playerd.service`
- `mod_display.service`

The files in `services/` aren't read by systemd directly — they have `__USER__` / `__HOME__` placeholders. `install.sh` does the substitution and writes the final files to `/etc/systemd/system/`. That's why edits to `services/*.service` don't take effect until you re-run `install.sh` (or manually `sed`+`tee` the file into place and `systemctl daemon-reload`).

Useful commands:

```bash
# What's installed and is it running?
systemctl status mod_playerd
systemctl status mod_display

# Restart after a code change
sudo systemctl restart mod_playerd

# Watch logs live (Ctrl-C to exit)
journalctl -u mod_playerd -f

# Last 50 lines of logs
journalctl -u mod_playerd -n 50

# See the actual unit content systemd has loaded
systemctl cat mod_playerd
```

Worth knowing about the daemon unit:

- `User=hypnoshock` (or whatever your installer-substituted username) — the daemon runs as your user, not root. This is why `gpiozero` uses the `lgpio` backend (it talks to `/dev/gpiochip0`, which respects the `gpio` group); the old `RPi.GPIO` backend needs `/dev/mem` (root-only).
- `SupplementaryGroups=audio gpio` — extra groups the daemon process needs for the USB DAC and GPIO chip.
- `WorkingDirectory=__HOME__` — important! The `lgpio` Python library creates a notification pipe file in the current directory at startup. If cwd is `/` (the systemd default), the user can't write there and the daemon crashes. Setting cwd to `$HOME` fixes it. There's no comment about this anywhere in `lgpio`'s docs; we discovered it the hard way.
- `Restart=on-failure` — if the daemon throws an unhandled exception and exits, systemd restarts it after 2s.

## 3. Unix sockets — the part you wanted explained

The daemon listens on a **Unix domain socket** at `/tmp/modplayer.sock`. This is the part to take your time with — if you're new to Linux IPC it's the bit that feels most magical, but it's actually quite small once you understand the moving pieces.

### 3.1 What a socket actually is

A **socket** is a two-way communication channel between two processes. Think of it as a tube: one process writes bytes in, the other reads them out, and vice versa.

You've probably met one kind of socket already: a TCP socket like `(127.0.0.1, 8080)`. That's a **network socket** — identified by an IP address and a port number. Two processes connect to each other across the network (even if "the network" is just localhost talking to itself).

A **Unix domain socket** is the same idea but the connection is identified by a **filesystem path** instead of an IP+port. The path looks like an ordinary file:

```
$ ls -la /tmp/modplayer.sock
srw-rw-rw- 1 hypnoshock hypnoshock 0 May 16 00:05 /tmp/modplayer.sock
                            ^
                            the 's' at the start means "socket"
```

It's not really a file — you can't `cat` it. It's a name in the filesystem that the kernel uses to find the socket. Two advantages over a TCP socket on localhost:

1. **Permissions** — you can use ordinary file permissions (`chmod`) to decide who's allowed to connect. We use `0666` so any user on the Pi can connect.
2. **It's faster** — no TCP/IP stack, no checksums, no port allocation. The kernel hands bytes from one process to the other directly.

The trade-off: only processes on the same machine can talk to a Unix socket. For network access (e.g. a future web UI hosted elsewhere), you'd need a TCP socket. That'll be a future change, and the daemon's command dispatcher is structured so the same verbs can be reused over either transport.

### 3.2 The five things every server socket does

Any server that listens on a socket — Unix or TCP — does the same five things:

1. **Create** the socket (`socket()`)
2. **Bind** it to an address (a file path for Unix sockets, an IP+port for TCP). This reserves the name.
3. **Listen** (`listen()`) — tell the kernel you're ready to accept connections, and how many waiting clients you'll allow to queue up.
4. **Accept** (`accept()`) connections from clients, one by one. Each `accept` returns a *new* socket dedicated to that one client's conversation.
5. **Read and write** bytes on that per-client socket. When done, close it. Loop back to step 4.

The daemon does all of this — but you won't see most of it because we use Python's `socketserver` module which wraps the boilerplate.

### 3.3 How the daemon's socket setup looks in the code

Inside `mod_playerd.py`:

```python
class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, cmd_q):
        if os.path.exists(path):
            os.unlink(path)              # delete any leftover socket file
        super().__init__(path, _CmdHandler)
        os.chmod(path, 0o666)             # any user can connect
        self.cmd_q = cmd_q
```

What's happening here:

- `UnixStreamServer` is the "create + bind + listen" piece from steps 1–3. You give it a path and a handler class; it does the rest. (`Stream` means TCP-like byte stream, in contrast to `Datagram` which is UDP-like discrete messages. For our line-based protocol, stream is what we want.)
- `ThreadingMixIn` makes step 4–5 multi-threaded: every accepted connection runs in its own thread, so a slow client can't block other clients. (For our use case — at most a couple of clients per second — single-threaded would also be fine, but threading costs nothing here.)
- `os.unlink(path)` matters because the socket file persists in the filesystem after the daemon dies. If you don't delete it before re-binding, you get `OSError: [Errno 98] Address already in use`. (`allow_reuse_address` doesn't help for Unix sockets the way it does for TCP.)
- `os.chmod(0o666)` makes the socket world-writable. Without this, only the daemon's user could send commands.

The server runs in its own thread:

```python
self._server = _UnixServer(SOCKET_PATH, self.cmd_q)
self._server_t = threading.Thread(
    target=self._server.serve_forever, name='control', daemon=True)
self._server_t.start()
```

`serve_forever` is the accept loop (step 4) — it blocks waiting for connections and, when one arrives, hands it off to a worker thread that runs the handler.

### 3.4 What the handler does per connection

This is the class that runs once per client connection:

```python
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
```

Reading this top to bottom:

- `self.rfile` and `self.wfile` are file-like objects that `StreamRequestHandler` sets up for us on top of the raw socket. `rfile` is the input side, `wfile` is the output side. Treating sockets like files is a Unix convention — you read and write bytes; the kernel handles the rest.
- `self.rfile.readline()` blocks until either a newline arrives or the client closes the connection. The client (`modctl`) always sends exactly one line.
- We split the line into a verb and arguments — `"seek +10\n"` becomes `verb="seek", args="+10"`.
- **The handler doesn't touch the player itself.** Instead, it builds a one-shot reply queue and pushes `(verb, args, reply_q)` onto the daemon's central command queue. The handler thread then blocks waiting for the main thread to put a reply on `reply_q`.
- This indirection matters because the main thread is the only one that owns the loaded `Module` pointer. If multiple threads tried to call libopenmpt's seek functions concurrently, we'd get crashes. Routing everything through one queue means there's only ever one cook in the kitchen.
- When the reply arrives, we write it back through `wfile` and the handler returns. `socketserver` closes the connection automatically.

### 3.5 What the client (modctl) does

`modctl` is a separate script — not defined by the daemon, not imported from it. It's a thin client that anyone could re-implement in 20 lines of any language. Here's its core:

```python
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5.0)
s.connect(SOCKET_PATH)
s.sendall(msg.encode('utf-8'))
s.shutdown(socket.SHUT_WR)        # "I'm done sending — but still listening"
chunks = []
while True:
    data = s.recv(4096)
    if not data:
        break
    chunks.append(data)
s.close()
sys.stdout.write(b''.join(chunks).decode('utf-8', 'replace'))
```

Step by step:

- `socket.AF_UNIX` says "this is a Unix domain socket, not TCP." If you wanted to talk to a TCP server you'd use `AF_INET` and `(host, port)` instead of a path.
- `SOCK_STREAM` says "byte stream, not datagram" — same as the server.
- `connect(SOCKET_PATH)` is the client side of accept/listen — it triggers a connection that the server's accept loop picks up.
- `sendall` writes all the bytes (e.g. `b"seek +10\n"`). `sendall` keeps trying until everything is sent or it errors out — vs `send` which may write only part.
- `shutdown(SHUT_WR)` is a Unix subtlety worth knowing. It says "I'm done writing, but I still want to read." This is how the server knows the request is complete. Without it, the server's `readline()` returns because of the newline, but the server's *handler* wouldn't necessarily know there isn't a second line coming.
- `recv(4096)` reads up to 4096 bytes. Returns `b''` (empty bytes) when the other end closes the connection. We loop until that happens, accumulating chunks.

That's it. modctl is dumb on purpose:

- It knows **nothing** about commands. It takes your argv (`modctl seek +10`), joins it with spaces, slaps a newline on the end, and sends it.
- It knows **nothing** about replies. It just prints whatever comes back.
- The daemon is the source of truth for what verbs exist. To add a new command, you only touch the daemon. `modctl xyz` will then just work.

This is why "where is modctl defined?" doesn't really have a satisfying answer — there's no command definition anywhere. The contract is:

1. **The daemon's `Daemon.DISPATCH` dictionary** (in `mod_playerd.py`) defines which verbs do what.
2. **modctl** is just a pipe between your shell argv and that dictionary.
3. If you typed `modctl banana`, the daemon would receive `banana\n`, look it up in `DISPATCH`, find nothing, and reply `err: unknown verb 'banana'`. modctl would print that, and you'd see the error.

You can prove this to yourself without modctl:

```bash
echo 'status' | nc -U /tmp/modplayer.sock      # if you have netcat-openbsd
# → prints the JSON status line
```

Or in raw Python from any shell:

```python
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/tmp/modplayer.sock')
s.sendall(b'next\n')
s.shutdown(socket.SHUT_WR)
print(s.recv(4096).decode())   # → 'ok\n'
```

The web UI later will do the same thing, just from JavaScript via a small bridge.

## 4. The daemon's threading model

`mod_playerd.py` is one Python process, but it runs **four logical threads** plus some helpers that `socketserver` spawns:

| Thread | Owns | Blocks on |
|---|---|---|
| **Main** | The loaded `Module` pointer, the playlist, the state file. | The command queue (`queue.get(timeout=1.0)`). |
| **Renderer** | A reusable PCM buffer. | The audio queue (`put` blocks when full). |
| **Audio out** | The `sounddevice.RawOutputStream`. | The audio queue (`get` blocks when empty). |
| **Control server** | The Unix socket. | `accept()`. Spawns a handler thread per connection. |
| **gpiozero internal** | (managed by gpiozero) | Polls the GPIO chip for edges. Calls our button callbacks. |

The whole point of this split is that the only thing the audio thread does is move bytes from a queue into the stream. It can't get blocked by GPIO debouncing, libopenmpt rendering, a slow `modctl` client, or a `/tmp/mod_state.json` write. So as long as the renderer can keep ~92 ms of audio queued up (4 chunks × 1024 frames at 44.1 kHz), playback can't stutter.

### 4.1 The command queue

Everything that wants to *do* something goes through one `queue.Queue`:

```python
self.cmd_q = queue.Queue()
```

Producers: the socket handler thread, the GPIO callbacks, the renderer's "end of track" notification.

Consumer: the main thread, in `Daemon.run()`:

```python
while not self._stop:
    try:
        verb, args, reply_q = self.cmd_q.get(timeout=1.0)
    except queue.Empty:
        self._write_state()
        self._poll_floppy()
        continue
    ...
```

This loop is the heart of the daemon. It blocks for up to 1 second waiting for a command. The 1-second timeout doubles as the periodic tick — when no command arrives, it falls through and updates the state file + checks if a USB floppy was just plugged in.

When a command does arrive, it's a tuple of `(verb, args, reply_q)`. The `reply_q` is `None` for fire-and-forget commands (GPIO buttons, end-of-track notifications) and a single-slot queue when the caller wants a response (socket handler).

### 4.2 The command dispatch table

```python
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
```

Each entry is a tiny lambda that takes the daemon and the argument string, and returns a reply string (or `None` for internal commands like `_track_end` that have no caller waiting).

**To add a new command, you add one row to this dict and write one method.** That's the entire surface for new functionality. Examples of things you could add easily:

- `'restart'` → `seek 0` (rewind to the start of the current track).
- `'volume +5'` → keep a multiplier in the daemon, apply it to the int16 buffer before pushing to sounddevice.
- `'queue <path>'` → insert a file into the playlist immediately after the current one.
- `'tracks'` → return the full playlist as JSON.

The leading underscore in `'_track_end'` is a convention I used to mark "internal — not a user command." There's no enforcement; the dispatch is name-based and case-folded.

## 5. The state file — how the display sees what's playing

The display has no socket. It just reads `/tmp/mod_state.json` once every ~125 ms.

The daemon writes the file:

- Immediately on every track change or seek (`_write_state(force=True)`).
- Once per second otherwise, from the main loop's idle tick.

```json
{
  "file":       "/home/hypnoshock/mod_player/mods/boost.mod",
  "title":      "boost",
  "format":     "ProTracker MOD (M.K.)",
  "duration_s": 317,
  "elapsed_s":  42.30,
  "started_at": 1715432142.7,
  "paused":     false,
  "source":     "fallback"
}
```

Two design notes that matter:

- **`started_at` is a back-derivation, not a real start time.** The daemon computes it every tick as `time.time() - elapsed_s`. We do this so `mod_display.py`'s existing wall-clock arithmetic (`elapsed = now - started_at`) keeps working without modification. When the playhead is moving normally, `started_at` is stable. When you seek, `elapsed_s` jumps and `started_at` jumps the opposite direction. When you pause, `elapsed_s` stops and `started_at` drifts forward at the same rate as wall time — net result: the display's computed `elapsed` is frozen. It looks unhinged when you stare at the raw JSON, but it produces the right answer on the display.
- **Atomic writes.** We write to a temp file and rename it, never modify the live file in place. `os.rename` is atomic on the same filesystem, so the display either sees the old file or the new file, never a half-written one.

## 6. GPIO buttons

`gpiozero` does most of the work. Wiring is GPIO 6 to GND for "prev," GPIO 26 to GND for "next." Internal pull-ups keep the lines high; a button press pulls them low.

```python
self._btn_next = Button(
    GPIO_NEXT, bounce_time=0.1,
    hold_time=BUTTON_HOLD_S, hold_repeat=True)
```

The interesting parameters:

- `bounce_time=0.1` — physical buttons "bounce" (make/break contact rapidly) for a few milliseconds when pressed. This setting tells gpiozero to ignore any second edge within 100 ms. Without it, one press registers as many.
- `hold_time=0.5` — how long the button has to be down before `when_held` starts firing.
- `hold_repeat=True` — fire `when_held` repeatedly while the button stays down, at intervals of `hold_time`.

The short-press-vs-hold logic is in `_wire_button`:

```python
def on_pressed():
    setattr(self, held_attr, False)            # reset the flag

def on_held():
    setattr(self, held_attr, True)             # mark "this was a hold"
    self.cmd_q.put(('seek', f'{seek_step:+d}', None))   # ±5

def on_released():
    if not getattr(self, held_attr):           # if we never went into hold mode
        self.cmd_q.put((track_verb, '', None)) # → it was a short press → skip track
```

So the sequence is:
- Press down → `on_pressed` clears the flag.
- If 500 ms passes → `on_held` fires, sets the flag, and seeks. Keeps firing every 500 ms while held.
- Release < 500 ms → `on_released` sees the flag is still false, fires `next`/`prev`.
- Release > 500 ms → `on_released` sees the flag is true, does nothing (the seeks already happened).

The `lgpio` backend (forced via `GPIOZERO_PIN_FACTORY=lgpio` at the top of the file) talks to `/dev/gpiochip0` — a kernel interface that respects the `gpio` group, so the daemon doesn't need root.

## 7. libopenmpt via ctypes — playing audio without Python bindings

`libopenmpt` is a C library that plays tracker modules. There's no maintained Python binding in the Pi's apt repos, so `openmpt.py` uses **ctypes** to call it directly.

### 7.1 What ctypes is

ctypes is a Python stdlib module that lets you load any C shared library and call its functions. You tell ctypes which functions you want, what argument types they take, and what type they return. ctypes handles the conversion in both directions.

```python
from ctypes import CDLL, c_void_p, c_double, c_int16, POINTER
_lib = CDLL('libopenmpt.so.0')

_lib.openmpt_module_get_duration_seconds.argtypes = [c_void_p]
_lib.openmpt_module_get_duration_seconds.restype = c_double
```

Now `_lib.openmpt_module_get_duration_seconds(mod_ptr)` is a regular Python call that returns a Python float. ctypes does the assembly-level argument shuffling for you.

### 7.2 The trap you must avoid

If you don't declare `restype`, ctypes assumes it's `int`. For an `int` return, that's fine. For any other type — especially `double` — you silently get truncated garbage.

```python
# WRONG — duration silently truncated to int → 317.0 becomes 317, but worse,
# on ARMv6, double-return ABI puts the result in different registers than
# int-return, so you don't even get the truncated value, you get whatever
# was in the int register, which is usually a small bogus number.
mod_duration = _lib.openmpt_module_get_duration_seconds(mod_ptr)
```

Every function in `openmpt.py` has its `restype` declared explicitly. If you add a new function from libopenmpt, **set its restype first**, even before you test it. The failure mode is silent crashes or garbage values that look almost plausible.

### 7.3 The wrapper class

`openmpt.Module` is a thin RAII-style wrapper around the libopenmpt pointer:

- `__init__(path)` reads the file into bytes, calls `openmpt_module_create_from_memory2`, stores the returned pointer.
- `read_stereo(rate, frames, buf)` fills `buf` with PCM data. Returns frames written; 0 means end of track.
- `seek(seconds)` returns the actual snapped position (libopenmpt rounds to row boundaries).
- `duration()` / `position()` / `title` / `type_long` — straight passthroughs to the C calls.
- `close()` calls `openmpt_module_destroy`. `__del__` calls it too as a backstop.

The wrapper is **not thread-safe**. Only the main thread should touch a `Module` instance. The renderer reads from it because the main thread loaded it and won't change it out from under the renderer until a track switch — and a track switch flushes the audio queue first.

## 8. The audio pipeline

```
libopenmpt rendering
        │ int16 stereo PCM, 1024 frames per chunk
        ▼
   render thread
        │ pushes chunk
        ▼
  audio queue (maxsize=4 chunks ≈ 92 ms buffered)
        │ pops chunk
        ▼
   audio thread
        │ stream.write(chunk)
        ▼
  sounddevice (PortAudio)
        │
        ▼
       ALSA → USB Audio CODEC → speakers
```

**Why the queue?** Because the audio thread must never block. If you called `read_stereo` directly inside the audio callback, any libopenmpt hiccup (track switch, GC pause, file load) would cause an audible underrun. Splitting render from playback means the audio thread can chew through the pre-rendered buffer while the renderer recovers.

**Why size 4?** Bigger = more latency between commands and audio (a press of "next" takes longer to be audible because there's queued audio in front of it). Smaller = less margin for renderer hiccups. 4 chunks × 23 ms = 92 ms felt like the right compromise.

**Why ALSA direct, not PulseAudio?** Two reasons:
- The Pi runs PipeWire-PA, which means audio is mixed across user sessions. For a single-application device with one daemon as the only producer, mixing is unnecessary overhead.
- More importantly: PulseAudio's per-user session model fights with system-level systemd services. To talk to PA from a system service, you need to forward `PULSE_RUNTIME_PATH`, ensure the user session is active, and deal with permission edge cases. ALSA-direct just opens `hw:CARD=CODEC,DEV=0` and goes.

The flush-on-seek logic in `Player._flush_audio` is what makes seeks feel snappy:

```python
def _flush_audio(self):
    try:
        while True:
            self._audio_q.get_nowait()
    except queue.Empty:
        pass
    try:
        self._audio_q.put_nowait(_SkipChunk)
    except queue.Full:
        pass
```

Drain the queue, push a sentinel so the audio thread skips any in-flight chunk. Without this, after a `next`, you'd hear ~100 ms of the *previous* track before the new one kicks in.

## 9. The playlist + floppy state machine

`playlist.py` is small but does two jobs.

**Job 1: find tracker files.** Walks a directory tree looking for `.mod`, `.xm`, `.it`, `.s3m` (case-insensitive). Returns a flat list.

**Job 2: decide where files come from.** Three states:

- `FALLBACK` — using `~/mod_player/mods`. Default.
- `FLOPPY` — using `~/floppy`. Active when `/dev/sda` exists and has tracker files.
- `EMPTY` — nothing playable available; daemon idles.

Every second, the daemon's main loop calls `playlist.detect_source()`. If the answer changed, the playlist reloads (which also reshuffles), the current track is interrupted, and playback starts on the new source.

The mount/umount uses `sudo /bin/mount` — `install.sh` adds a sudoers entry granting your user passwordless mount/umount permissions. If you ever wonder "how does a non-root daemon mount USB sticks?" — that's how.

Shuffle is `random.shuffle` on the file list. New shuffle when the source changes, and when the index wraps past the end (a "full pass").

## 10. The display daemon

`mod_display.py` is conceptually simple but has a complicated display driver inside.

The loop:

```
forever:
    state = json.load('/tmp/mod_state.json')
    if state is missing or "stale":
        draw_idle()
    else:
        draw_track(state)
    sleep 125 ms
```

"Stale" means `now > started_at + duration + 30`. This is the safety against the display showing a track that never advanced (e.g. daemon crashed, no one writing the file).

The ST7789 driver class is hand-rolled because this specific generic AliExpress panel only responds to SPI mode 3 — the standard libraries hard-code mode 0. The full story is in `DRIVER-NOTES.md`; the gory hardware bring-up details are in `240x240-ips.md`. Don't touch the driver unless you've read those.

## 11. The control flow of a single command

Walking through what happens when you type `modctl next`:

1. **Your shell** runs `/usr/local/bin/modctl next` (symlink → `~/mod_player/modctl`).
2. **modctl** opens a Unix socket connection to `/tmp/modplayer.sock`, sends `b"next\n"`, half-closes for writing.
3. The **kernel** wakes the daemon's control server thread (blocked in `accept()`).
4. The control server thread spawns a **handler thread** for this connection. (Threading mixin.)
5. The handler thread calls `rfile.readline()` → gets `b"next\n"`.
6. The handler thread builds a single-slot reply queue, puts `("next", "", reply_q)` onto the **command queue**, then blocks on `reply_q.get(timeout=5.0)`.
7. The **main thread**, which was blocked in `cmd_q.get(timeout=1.0)`, wakes up with the tuple.
8. Main thread looks up `"next"` in `DISPATCH`, calls `_do_next()`.
9. `_do_next` asks the playlist for the next file path, calls `self._play_current(path)`.
10. `_play_current` calls `self.player.load(new_path)` → flushes the audio queue, destroys the old `Module`, creates a new one.
11. The **renderer thread**, which was blocked on `audio_q.put(chunk, timeout=1.0)`, sees the queue drain and pushes a new chunk from the new track. The **audio thread** picks it up immediately.
12. `_do_next` returns `"ok\n"`. Main thread puts it on `reply_q`.
13. Handler thread wakes, writes `b"ok\n"` to `wfile`, returns. `socketserver` closes the connection.
14. modctl's `recv()` returns the `b"ok\n"` (plus EOF), prints it, exits.

Same flow for socket commands, GPIO press, and the renderer's own end-of-track callback — they're all just things that push tuples onto the command queue.

## 12. Common maintenance tasks

### Editing the daemon code

```bash
nano ~/mod_player/mod_playerd.py
sudo systemctl restart mod_playerd
journalctl -u mod_playerd -n 20 --no-pager   # check it came up clean
```

### Editing a systemd unit

Edit `~/mod_player/services/*.service` (the placeholder version) and re-run:

```bash
sudo ./install.sh
```

Or, for a quick one-off edit without re-running the whole installer:

```bash
sudo nano /etc/systemd/system/mod_playerd.service
sudo systemctl daemon-reload
sudo systemctl restart mod_playerd
```

But remember the in-repo file will then drift from the installed one — fix this next time you re-run the installer, or copy your changes back to `services/`.

### Adding a new command

1. Add the verb to the `DISPATCH` dict in `mod_playerd.py`.
2. Add a `_do_<verb>` method.
3. Restart the daemon.
4. Use `modctl <verb>` — no modctl change needed.

### Debugging "the daemon won't start"

```bash
sudo journalctl -u mod_playerd -n 100 --no-pager
```

If the import or setup phase fails, you'll see a Python traceback. Look for:

- `FileNotFoundError: '.lgd-nfy-N'` — `WorkingDirectory=` is missing/wrong in the unit.
- `RuntimeError: Failed to add edge detection` — gpiozero fell back to RPi.GPIO; ensure `GPIOZERO_PIN_FACTORY=lgpio` is being set.
- `OSError: [Errno 98] Address already in use` on the socket — daemon died without cleanup; another instance may still be running, or `/tmp/modplayer.sock` is stale (the daemon should unlink on startup, but `pkill -f mod_playerd` and try again).
- `PortAudioError ... No output device matching ...` — the device name in `AUDIO_DEVICE` doesn't match what's plugged in. Run `~/mod_player/.venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"` to see what PortAudio sees.

### Debugging "the display won't update"

```bash
cat /tmp/mod_state.json    # is the daemon writing it?
stat /tmp/mod_state.json   # is the mtime updating every second?
sudo journalctl -u mod_display -n 50
```

If the JSON is fresh but the display is stale, the display daemon is the problem. If the JSON is stale, the daemon isn't writing it — start with the daemon's journal.

### Pulling the SD card and looking at it on another machine

The repo is at `/home/hypnoshock/mod_player`. The installed systemd units are at `/etc/systemd/system/mod_playerd.service` and `.../mod_display.service`. State lives at `/tmp/mod_state.json` (gone after reboot). Logs live in the systemd journal under `/var/log/journal/`.

## 13. Where to read more

- **Hardware/wiring**: `240x240-ips.md` (the display panel) and the `Hardware` section of the top-level `README.md`.
- **Why we ship our own ST7789 driver**: `DRIVER-NOTES.md`.
- **The display-side architecture (older, partially overlaps with this doc)**: `DISPLAY-FEATURE.md`. Worth keeping for the timing budget table and ST7789 init sequence.
- **Python stdlib references that helped me write this**: `socketserver`, `socket` (AF_UNIX section), `queue.Queue`, `threading`, `ctypes`. All on docs.python.org.
- **libopenmpt API**: <https://lib.openmpt.org/libopenmpt/doc/> — only the `openmpt_module_*` family is used here.
