"""Per-user instance lease and DPAPI-protected browser session discovery."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path


def crypt(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Instance discovery requires Windows DPAPI")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


class Instance:
    def __init__(self, root, elevated=False):
        key = hashlib.sha256(os.path.normcase(os.path.abspath(root)).encode()).hexdigest()[:16]
        base = Path(os.environ.get("DEEPCLEAN_RUNTIME_DIR") or os.path.join(
            os.environ.get("LOCALAPPDATA", str(Path.home())), "DeepClean", "runtime"))
        self.directory = base / (key + ("-admin" if elevated else "-user"))
        self.file = None

    def acquire(self):
        import msvcrt
        self.directory.mkdir(parents=True, exist_ok=True)
        from safety import plain_path
        if not plain_path(str(self.directory)):
            raise RuntimeError("实例状态目录不能是目录联接或符号链接")
        self.file = open(self.directory / "instance.lock", "a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def publish(self, port, token):
        data = crypt(json.dumps(dict(port=port, token=token)).encode())
        temporary = self.directory / "session.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, self.directory / "session.bin")

    def session(self):
        data = json.loads(crypt((self.directory / "session.bin").read_bytes(), decrypt=True))
        if not isinstance(data.get("port"), int) or not 1 <= data["port"] <= 65535:
            raise ValueError("invalid instance port")
        return data

    def close(self):
        if self.file:
            try:
                (self.directory / "session.bin").unlink(missing_ok=True)
            finally:
                self.file.close()
                self.file = None
