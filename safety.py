"""Shared conservative path checks. Does not claim atomic Shell deletion."""
import os
import stat


def normalized(path):
    return os.path.normcase(os.path.abspath(path))


def within(path, root):
    try:
        return os.path.commonpath([normalized(path), normalized(root)]) == normalized(root)
    except (ValueError, TypeError):
        return False


def plain_path(path):
    """Reject any existing symlink/reparse component, including ancestors."""
    if not path or not os.path.isabs(path) or "%" in path:
        return False
    current = os.path.abspath(path)
    while True:
        try:
            st = os.lstat(current)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def fingerprint(path):
    st = os.lstat(path)
    return stat_fingerprint(st)


def stat_fingerprint(st):
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def delete_verified_file(path, expected):
    """Windows: exclusively open, verify, and delete the same file handle.

    Caller must enforce category/root/age/approval policy. No fallback to path deletion.
    """
    if os.name != "nt" or not plain_path(path):
        return False
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # DELETE | FILE_READ_ATTRIBUTES; exclusive sharing; open the reparse object itself.
    handle = create(path, 0x10000 | 0x80, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    fd = None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        if stat_fingerprint(os.fstat(fd)) != expected:
            return False
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = final(handle, buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            return False
        resolved = buffer.value
        # Strip \\?\ only for drive-letter / UNC forms. Volume GUID paths must keep
        # the prefix; after a matching fingerprint they still identify the same handle.
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif len(resolved) >= 6 and resolved.startswith("\\\\?\\") and resolved[5] == ":":
            resolved = resolved[4:]
        if normalized(resolved) != normalized(path):
            # Volume GUID (or other non-DOS) final path: identity already verified above.
            if not (resolved.startswith("\\\\?\\Volume{") or resolved.startswith("Volume{")):
                return False
        disposition = kernel.SetFileInformationByHandle
        disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        disposition.restype = wintypes.BOOL
        delete = wintypes.BOOL(True)
        return bool(disposition(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)))
    except (OSError, ValueError):
        return False
    finally:
        if fd is not None:
            os.close(fd)
        else:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
