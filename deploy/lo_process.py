"""Block network socket creation before executing LibreOffice (Linux only)."""

import ctypes
import os
import platform
import sys


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]


def block_network():
    machine = platform.machine()
    values = {"x86_64": (0xC000003E, 41, 53), "aarch64": (0xC00000B7, 198, 199)}
    if machine not in values:
        raise OSError("Unsupported sandbox architecture")
    architecture, socket_number, socketpair_number = values[machine]
    # seccomp_data: syscall number at 0, architecture at 4, argument 0 at 16.
    # Only AF_UNIX sockets are permitted. The filter is inherited across exec
    # and child processes, and rejects x32 syscall numbers on x86_64.
    rules = (Filter * 12)(
        Filter(0x20, 0, 0, 4), Filter(0x15, 1, 0, architecture),
        Filter(0x06, 0, 0, 0x80000000), Filter(0x20, 0, 0, 0),
        Filter(0x45, 5, 0, 0x40000000), Filter(0x15, 1, 0, socket_number),
        Filter(0x15, 0, 4, socketpair_number), Filter(0x20, 0, 0, 16),
        Filter(0x15, 2, 0, 1), Filter(0x06, 0, 0, 0x00050001),
        Filter(0x06, 0, 0, 0x00050001), Filter(0x06, 0, 0, 0x7FFF0000),
    )
    program = Program(len(rules), rules)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(38, 1, None, 0, 0) or libc.prctl(22, 2, ctypes.cast(ctypes.pointer(program), ctypes.c_void_p), 0, 0):
        raise OSError("Could not enforce network sandbox")


if __name__ == "__main__":
    try:
        block_network()
    except OSError:
        sys.exit(120)
    os.execv(sys.argv[1], sys.argv[1:])
