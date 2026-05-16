"""Minimal ctypes wrapper around libopenmpt for playback + seek + metadata.

Pi Zero W has libopenmpt.so.0 installed but no Python bindings packaged.
This module covers exactly the subset mod_playerd.py needs: load module from
file, render int16 stereo frames, seek by seconds, fetch title/type metadata.
"""
import ctypes
from ctypes import (
    CDLL,
    POINTER,
    c_char_p,
    c_double,
    c_int,
    c_int16,
    c_int32,
    c_size_t,
    c_void_p,
)
from ctypes.util import find_library

_lib_path = find_library('openmpt') or 'libopenmpt.so.0'
_lib = CDLL(_lib_path)

# Restypes MUST be set explicitly — default c_int will silently truncate
# doubles to int on ARMv6, which manifests as totally bogus position values.

_lib.openmpt_module_create_from_memory2.argtypes = [
    c_void_p, c_size_t,
    c_void_p, c_void_p,       # logfunc, loguser
    c_void_p, c_void_p,       # errfunc, erruser
    POINTER(c_int),           # error
    POINTER(c_char_p),        # error_message
    c_void_p,                 # ctls
]
_lib.openmpt_module_create_from_memory2.restype = c_void_p

_lib.openmpt_module_destroy.argtypes = [c_void_p]
_lib.openmpt_module_destroy.restype = None

_lib.openmpt_module_read_interleaved_stereo.argtypes = [
    c_void_p, c_int32, c_size_t, POINTER(c_int16),
]
_lib.openmpt_module_read_interleaved_stereo.restype = c_size_t

_lib.openmpt_module_get_duration_seconds.argtypes = [c_void_p]
_lib.openmpt_module_get_duration_seconds.restype = c_double

_lib.openmpt_module_get_position_seconds.argtypes = [c_void_p]
_lib.openmpt_module_get_position_seconds.restype = c_double

_lib.openmpt_module_set_position_seconds.argtypes = [c_void_p, c_double]
_lib.openmpt_module_set_position_seconds.restype = c_double

_lib.openmpt_module_get_metadata.argtypes = [c_void_p, c_char_p]
_lib.openmpt_module_get_metadata.restype = c_void_p  # so we can free it

_lib.openmpt_free_string.argtypes = [c_void_p]
_lib.openmpt_free_string.restype = None

# Pattern introspection — for the display's scrolling background view.

_lib.openmpt_module_get_current_pattern.argtypes = [c_void_p]
_lib.openmpt_module_get_current_pattern.restype = c_int32

_lib.openmpt_module_get_current_row.argtypes = [c_void_p]
_lib.openmpt_module_get_current_row.restype = c_int32

_lib.openmpt_module_get_current_order.argtypes = [c_void_p]
_lib.openmpt_module_get_current_order.restype = c_int32

_lib.openmpt_module_get_num_channels.argtypes = [c_void_p]
_lib.openmpt_module_get_num_channels.restype = c_int32

_lib.openmpt_module_get_pattern_num_rows.argtypes = [c_void_p, c_int32]
_lib.openmpt_module_get_pattern_num_rows.restype = c_int32

_lib.openmpt_module_format_pattern_row_channel_command.argtypes = [
    c_void_p, c_int32, c_int32, c_int32, c_int,
]
_lib.openmpt_module_format_pattern_row_channel_command.restype = c_void_p

_lib.openmpt_module_get_current_speed.argtypes = [c_void_p]
_lib.openmpt_module_get_current_speed.restype = c_int32

_lib.openmpt_module_get_current_estimated_bpm.argtypes = [c_void_p]
_lib.openmpt_module_get_current_estimated_bpm.restype = c_double

# libopenmpt command enum (from libopenmpt.h)
COMMAND_NOTE = 0
COMMAND_INSTRUMENT = 1
COMMAND_VOLUMEEFFECT = 2
COMMAND_EFFECT = 3
COMMAND_VOLUME = 4
COMMAND_PARAMETER = 5


class OpenMPTError(Exception):
    pass


class Module:
    """One loaded tracker module. Not thread-safe — only the renderer touches it."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            data = f.read()
        self._data = data  # keep alive; libopenmpt copies on init but be safe
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        err = c_int(0)
        msg = c_char_p()
        self._mod = _lib.openmpt_module_create_from_memory2(
            ctypes.cast(buf, c_void_p), len(data),
            None, None, None, None,
            ctypes.byref(err), ctypes.byref(msg), None,
        )
        if not self._mod:
            detail = msg.value.decode('utf-8', 'replace') if msg.value else 'unknown'
            raise OpenMPTError(f'failed to load {path}: {detail} (code {err.value})')
        self.path = path

    def close(self):
        if self._mod:
            _lib.openmpt_module_destroy(self._mod)
            self._mod = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read_stereo(self, samplerate, frames, buffer):
        """Render `frames` stereo frames into buffer (an int16 array of len ≥ frames*2).
        Returns frames actually written (0 means end-of-track)."""
        return _lib.openmpt_module_read_interleaved_stereo(
            self._mod, samplerate, frames, buffer,
        )

    def duration(self):
        return float(_lib.openmpt_module_get_duration_seconds(self._mod))

    def position(self):
        return float(_lib.openmpt_module_get_position_seconds(self._mod))

    def seek(self, seconds):
        """Seek to absolute seconds. Returns actual position reached (snaps to row)."""
        return float(_lib.openmpt_module_set_position_seconds(self._mod, seconds))

    def metadata(self, key):
        ptr = _lib.openmpt_module_get_metadata(self._mod, key.encode('utf-8'))
        if not ptr:
            return ''
        try:
            return ctypes.string_at(ptr).decode('utf-8', 'replace')
        finally:
            _lib.openmpt_free_string(ptr)

    @property
    def title(self):
        return self.metadata('title')

    @property
    def type_long(self):
        return self.metadata('type_long')

    # --- pattern introspection --------------------------------------------

    def current_pattern(self):
        return int(_lib.openmpt_module_get_current_pattern(self._mod))

    def current_row(self):
        return int(_lib.openmpt_module_get_current_row(self._mod))

    def current_order(self):
        return int(_lib.openmpt_module_get_current_order(self._mod))

    def num_channels(self):
        return int(_lib.openmpt_module_get_num_channels(self._mod))

    def current_speed(self):
        return int(_lib.openmpt_module_get_current_speed(self._mod))

    def current_bpm(self):
        return float(_lib.openmpt_module_get_current_estimated_bpm(self._mod))

    def pattern_num_rows(self, pattern):
        return int(_lib.openmpt_module_get_pattern_num_rows(self._mod, pattern))

    def format_cell(self, pattern, row, channel, command=COMMAND_NOTE):
        """Format a single column of one pattern cell as a string.
        For COMMAND_NOTE this returns 3-char strings like 'C-4' or '---'."""
        ptr = _lib.openmpt_module_format_pattern_row_channel_command(
            self._mod, pattern, row, channel, command)
        if not ptr:
            return ''
        try:
            return ctypes.string_at(ptr).decode('utf-8', 'replace')
        finally:
            _lib.openmpt_free_string(ptr)

    def format_cell_note(self, pattern, row, channel):
        return self.format_cell(pattern, row, channel, COMMAND_NOTE)
