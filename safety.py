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
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
