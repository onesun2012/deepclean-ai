# -*- coding: utf-8 -*-
"""
深度C盘清理 —— 本地 C 盘垃圾扫描 / 清理工具（网页界面版）

- 纯 Python 标准库实现，无任何第三方依赖
- 后端只监听 127.0.0.1，界面为本地单文件网页
- 启动: python app.py   （或双击 启动.bat；完整功能请用 以管理员启动.bat）

安全设计：
- 清理只删除“扫描阶段记录下来的、属于预定义类别目录”的文件，不会碰其它路径
- 正被占用 / 无权限 / 保留时间不足的文件自动跳过，并在清理报告中体现
- 回收站通过系统 Shell 接口清空（仅当前用户、仅 C 盘）
- 休眠文件 / 虚拟内存 / 系统还原点 / WinSxs 等系统级项目默认不勾选，且需要管理员权限
- 未锁定的危险分项必须显式确认才会进入清理计划（网页 confirm_danger / CLI --confirm-danger）
- 网页服务校验 Host 与请求来源（Origin / Sec-Fetch-Site），只接受本机同源请求
"""
import ctypes
import glob as _glob
import json
import os
import re
import shutil
import secrets
from safety import within, plain_path, fingerprint, delete_verified_file
from urllib.parse import urlsplit
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.request
from instance import Instance
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WIN = sys.platform == "win32"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# PyInstaller 打包后静态资源在解包目录 sys._MEIPASS 中
_BASE = getattr(sys, "_MEIPASS", APP_DIR)
STATIC_DIR = os.path.join(_BASE, "static")

_drive_letter = (os.environ.get("SystemDrive") or "C").strip("\\:")[:1] or "C"
DRIVE = _drive_letter
DRIVE_ROOT = DRIVE + ":\\"
WINDIR = os.environ.get("WINDIR", DRIVE_ROOT + "Windows")
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
API_TOKEN = secrets.token_urlsafe(32)
OPERATION_LOCK = threading.Lock()
PLAN_LOCK = threading.Lock()
HTTP_PLANS = {}
MAX_FILES_PER_CAT = 400000  # 单类别统计上限，防止极端目录拖垮内存
MAX_SCAN_ENTRIES = 150000
MAX_MANIFEST_BYTES = 96 * 1024 * 1024  # Conservative estimated manifest budget, not an RSS limit.


LOG_FILE = os.path.join(APP_DIR, "clearc.log")
# PyInstaller 单文件模式下 APP_DIR 在临时解包目录里，日志放到 exe 旁边才找得到
if getattr(sys, "frozen", False):
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "clearc.log")


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass  # pythonw 下没有控制台
    try:
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _env(k, default):
    v = os.environ.get(k)
    return v if v else default


def LA():  # %LOCALAPPDATA%
    return _env("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))


def RA():  # %APPDATA%
    return _env("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))


def E(t):
    """展开环境变量与用户目录"""
    return os.path.expandvars(os.path.expanduser(t))


def expand_roots(templates):
    """把含通配符的模板展开成实际存在的目录/文件列表"""
    out = []
    for t in templates:
        p = E(t)
        if any(c in p for c in "*?["):
            out += [x for x in _glob.glob(p) if os.path.isdir(x) or os.path.isfile(x)]
        else:
            out.append(p)
    return out


# ----------------------------------------------------------------------------
# 规则加载：清理规则全部来自 rules/*.json（公开可审计，欢迎 PR 补充工具）
# bucket = 清理分项（界面勾选单元）；tool = 工具（展示聚合单元）
# risk: safe 安全可删 / rebuildable 可重建 / migrate 建议迁移 / danger 危险·锁定
# 安全红线：locked(danger) 与 migrate 分项永远不会被清理；会话记录类全部 danger 锁定
# ----------------------------------------------------------------------------
RULES_DIR = os.path.join(_BASE, "rules")


def load_rules():
    tools, buckets = [], []
    if os.path.isdir(RULES_DIR):
        for fn in sorted(os.listdir(RULES_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(RULES_DIR, fn), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                log("规则文件加载失败 %s: %s" % (fn, e))
                raise ValueError("规则文件无效: " + fn) from e
            tools += data.get("tools", [])
            buckets += data.get("buckets", [])
    else:
        log("未找到规则目录: " + RULES_DIR)
    tool_ids = [t["id"] for t in tools]
    bucket_ids = [b["id"] for b in buckets]
    if len(tool_ids) != len(set(tool_ids)) or len(bucket_ids) != len(set(bucket_ids)):
        raise ValueError("规则 ID 重复")
    for b in buckets:
        if (b.get("tool") not in tool_ids or b.get("risk") not in ("safe", "rebuildable", "migrate", "danger")
                or not isinstance(b.get("paths", []), list)
                or any(not isinstance(p, str) or not p for p in b.get("paths", []))
                or not isinstance(b.get("min_age_min", 0), (int, float))
                or b.get("min_age_min", 0) < 0
                or (b.get("risk") == "danger" and not b.get("locked"))):
            raise ValueError("规则字段无效: " + str(b.get("id")))
    return tools, buckets


RULE_TOOLS, RULE_BUCKETS = load_rules()

TOOLS = {t["id"]: t for t in RULE_TOOLS}
CATEGORIES = []
for _b in RULE_BUCKETS:
    _t = TOOLS.get(_b.get("tool"), {})
    _cat = dict(
        id=_b["id"], tool=_b.get("tool", ""), category=_t.get("category", "system"),
        risk=_b.get("risk", "safe"),
        name=_b.get("labelZh", _b["id"]), nameEn=_b.get("labelEn", _b["id"]),
        desc=_b.get("hintZh", ""), descEn=_b.get("hintEn", ""),
        min_age_min=_b.get("min_age_min", 0),
        read_admin=_b.get("read_admin", False), clean_admin=_b.get("clean_admin", False),
        special_size=_b.get("special_size"), special_clean=_b.get("special_clean"),
        sysfiles=_b.get("sysfiles", []),
        roots=_b.get("paths", []),
    )
    if _b.get("locked"):
        _cat["locked"] = True
    if _b.get("default_off"):
        _cat["default_off"] = True   # 即使是可重建级也不参与默认勾选（如回收站）
    if _b.get("moveable"):
        _cat["moveable"] = True
        _cat["move_root"] = _b.get("move_root", "")
    CATEGORIES.append(_cat)

# 自动化测试沙盒（仅当设置了环境变量 CLEAR_C_SANDBOX 时注入一个额外分项）
if os.environ.get("CLEAR_C_SANDBOX"):
    CATEGORIES.insert(1, dict(id="sandbox", tool="sandbox", category="system", risk="safe",
                              name="测试沙盒", nameEn="Sandbox", desc="自动化测试目录", descEn="Test dir",
                              roots=[os.environ["CLEAR_C_SANDBOX"]]))

if os.environ.get("CLEAR_C_SANDBOX"):
    CATEGORIES = [c for c in CATEGORIES if c["id"] == "sandbox" or c.get("locked") or c.get("risk") == "migrate" or c.get("special_clean") == "recycle"]

CAT_BY_ID = {c["id"]: c for c in CATEGORIES}


def protected_roots():
    roots = [APP_DIR, _BASE, os.path.dirname(history_file())]
    for cat in CATEGORIES:
        if cat.get("locked") or cat.get("risk") == "migrate":
            roots.extend(E(p) for p in cat.get("roots", []) if not any(c in p for c in "*?["))
    return tuple(root for root in roots if root)


def protected_file(path, roots=None):
    """Protect persistent roots even if a different rule includes their parent."""
    return any(within(path, root) for root in (roots if roots is not None else protected_roots()))


DIRECT_MIN_AGE = 7 * 24 * 60 * 60
DIRECT_NOTES = {
    "temp-files": ("仅用户 Temp；旧文件不代表一定无用，请先结束安装和其他任务。", "User Temp only; age does not prove a file is unused. Finish installations and other tasks first."),
    "python-cache": ("仅 pip HTTP 下载缓存；下次安装可能重新下载，离线安装可能失败。", "pip HTTP cache only; future installs may download again and offline installs may fail."),
    "npm-store": ("仅 npm _cacache 下载缓存；下次安装可能重新下载，离线安装可能失败；不处理 pnpm store。", "npm _cacache only; future installs may download again and offline installs may fail. pnpm stores stay."),
    "nuget-cache": ("仅 NuGet HTTP 缓存；还原包时可能重新下载，离线还原可能失败；保留全局包目录。", "NuGet HTTP cache only; restores may download again or fail offline. Global packages stay."),
    "electron-cache": ("仅 Electron 下载缓存；下次安装需重新下载对应版本，耗费时间和流量；离线或源失效时可能无法恢复。", "Electron download cache only; reinstalling requires time and bandwidth. Recovery may fail offline or if the source disappears."),
}


def direct_roots(cid):
    """Narrow, code-reviewed allowlist; JSON risk=safe never grants permanent deletion."""
    if cid == "temp-files":
        return [os.path.join(LA(), "Temp")]
    if cid == "python-cache":
        return [os.path.join(LA(), "pip", "Cache", name) for name in ("http", "http-v2")]
    if cid == "npm-store":
        return [os.path.join(LA(), "npm-cache", "_cacache"), E(r"%USERPROFILE%\.npm\_cacache")]
    if cid == "nuget-cache":
        return [os.path.join(LA(), "NuGet", "v3-cache")]
    if cid == "electron-cache":
        return [os.path.join(LA(), "electron", "Cache")]
    return []


def plan_summary(cid, item):
    out = dict(size=item["size"], count=len(item["entries"]))
    if item.get("delete_mode") == "permanent":
        out.update(note=DIRECT_NOTES.get(cid, ("", ""))[0], note_en=DIRECT_NOTES.get(cid, ("", ""))[1],
                   roots=[root for root in direct_roots(cid) if any(within(e[0], root) for e in item["entries"])])
    return out


def direct_eligible(cat, path, expected):
    try:
        return (not is_admin() and not cat.get("locked") and cat.get("risk") not in ("migrate", "danger")
                and any(within(path, root) for root in direct_roots(cat["id"]))
                and plain_path(path) and not protected_file(path)
                and fingerprint(path) == expected
                and time.time() - os.lstat(path).st_mtime >= max(DIRECT_MIN_AGE, cat.get("min_age_min", 0) * 60))
    except (OSError, ValueError, TypeError):
        return False


def delete_direct_files(cat, paths, identities, stop):
    ok, failed = [], []
    for path in paths:
        if stop.is_set():
            failed.append(path)
        elif direct_eligible(cat, path, identities.get(path)) and delete_verified_file(path, identities.get(path)):
            ok.append(path)
        else:
            failed.append(path)
    return ok, failed


# ----------------------------------------------------------------------------
# 底层工具
# ----------------------------------------------------------------------------
def run_cmd(args, timeout=120):
    """执行外部命令，返回 (returncode, 合并输出)"""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
        return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return -2, "命令超时"
    except Exception as e:
        return -1, str(e)


def sysfile_size(name):
    try:
        return os.stat(DRIVE_ROOT + name).st_size
    except OSError:
        return -1  # 未知（普通权限通常无法读取该文件）


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_int64),
                ("i64NumItems", ctypes.c_int64)]


def recyclebin_size():
    """通过 Shell 接口查询当前用户在 C 盘回收站的大小"""
    if not IS_WIN:
        return -1, ""
    try:
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(DRIVE_ROOT, ctypes.byref(info))
        if hr == 0:
            return int(info.i64Size), ""
        return 0, ""  # 0x8000FFFF 等值表示回收站为空
    except Exception:
        return -1, ""


def empty_recycle_bin():
    if is_admin():
        return False, "管理员模式仅允许只读扫描，请以普通权限启动"
    if not IS_WIN:
        return False, "仅支持 Windows"
    try:
        hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, DRIVE_ROOT, 7)  # 7=不确认/无进度/无声音
        if hr == 0 or hr == -2147418113:  # S_OK 或 0x8000FFFF（已空）
            return True, ""
    except Exception:
        pass
    rc, out = run_cmd(["powershell", "-NoProfile", "-Command",
                       "Clear-RecycleBin -DriveLetter %s -Force -ErrorAction Stop" % DRIVE], 180)
    return rc == 0, out.strip()[:200]


def vss_storage_bytes():
    """查询系统还原点占用空间（需管理员）"""
    if not is_admin():
        return -1, "需要管理员权限才能查询还原点"
    rc, out = run_cmd(["vssadmin", "list", "shadowstorage"], 90)
    if rc != 0:
        return -1, "查询失败（可能没有创建过还原点）"
    used = 0
    for line in out.splitlines():
        low = line.lower()
        if ("used" in low) or ("已使用" in line):
            m = re.search(r"\(([\d,\s]+)\s*(?:bytes|字节)", line)
            if m:
                used = max(used, int(re.sub(r"[^\d]", "", m.group(1))))
    return (used, "" if used else "未发现可清理的还原点")


def walk_tree(root, stop, on_file, on_dir, budget):
    """迭代式目录遍历；budget=[剩余配额]，到 0 即停"""
    stack = [root]
    while stack and not stop.is_set():
        d = stack.pop()
        if not plain_path(d):
            continue
        on_dir(d)
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if stop.is_set() or budget[0] <= 0:
                    return
                try:
                    st = e.stat(follow_symlinks=False)
                    if e.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        budget[0] -= 1
                    else:
                        st = e.stat(follow_symlinks=False)
                        on_file(e.path, st.st_size, st.st_mtime)
                        budget[0] -= 1
                except OSError:
                    continue


# ----------------------------------------------------------------------------
# 扫描任务
# ----------------------------------------------------------------------------
class ScanJob:
    def __init__(self, cats):
        self.cats = [c for c in cats if c["id"] == "sandbox"] if os.environ.get("CLEAR_C_SANDBOX") else cats
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.stop = threading.Event()
        self.status = "idle"
        self.current = ""
        self.started = 0.0
        self.ended = 0.0
        self._last_touch = 0.0
        self.per = {c["id"]: dict(size=0, count=0, status="pending", note="") for c in self.cats}
        self.entries = {c["id"]: [] for c in self.cats}
        self.dirs = {c["id"]: [] for c in self.cats}
        self.roots_info = {c["id"]: [] for c in self.cats}
        self.last_used = {}  # tool_id -> 最新文件 mtime
        self.identities = {}
        self.scan_id = secrets.token_hex(16)

    def start(self, ids=None):
        with self.lock:
            if self.status in ("running", "stopping"):
                return False, "正在扫描中，请先停止"
            available = {c["id"] for c in self.cats}
            if ids is not None and (not ids or not set(ids).issubset(available)):
                return False, "扫描范围为空或包含未知分项"
            if not OPERATION_LOCK.acquire(blocking=False):
                return False, "已有扫描或清理任务进行中"
            self._reset()
            self.scope = set(ids) if ids is not None else available
            self.manifest_bytes = 0
            self.manifest_count = 0
            self.partial = False
            for cid in available - self.scope:
                self.per[cid].update(status="not_scanned", note="未扫描此分项")
            self.status = "running"
            self.started = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        return True, ""

    def stop_scan(self):
        with self.lock:
            if self.status == "running":
                self.stop.set()
                self.status = "stopping"

    def _touch(self, p):
        now = time.time()
        if now - self._last_touch > 0.08:
            self._last_touch = now
            self.current = p

    def _run(self):
        try:
            self._scan_run()
        except Exception as exc:
            with self.lock:
                self.status = "error"
                self.current = str(exc)
                self.ended = time.time()
        finally:
            OPERATION_LOCK.release()

    def _scan_run(self):
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(self._scan_cat, c) for c in self.cats if c["id"] in self.scope]
            for f in futs:
                f.result()
        stopped = self.stop.is_set()
        with self.lock:
            for info in self.per.values():
                if info["status"] in ("running", "pending"):
                    info["status"] = "done"
                    if stopped:
                        info["note"] = (info["note"] + " " if info["note"] else "") + "扫描被中断，结果不完整"
            self.status = "stopped" if stopped else "done"
            self.ended = time.time()
    def _reserve_entry(self, path):
        estimate = 640 + len(path) * 4
        with self.lock:
            if self.manifest_count >= MAX_SCAN_ENTRIES or self.manifest_bytes + estimate > MAX_MANIFEST_BYTES:
                self.partial = True
                return False
            self.manifest_count += 1
            self.manifest_bytes += estimate
            return True

    def _scan_cat(self, cat):
        cid = cat["id"]
        info = self.per[cid]
        info["status"] = "running"
        entries, dirs, root_stats = [], [], []
        total, count, note = 0, 0, ""
        retain = not cat.get("locked") and cat.get("risk") != "migrate"
        protected = protected_roots()
        latest = 0
        identities = {}
        budget = [MAX_FILES_PER_CAT]

        sc = cat.get("special_size")
        if sc == "sysfile":
            for name in cat["sysfiles"]:
                sz = sysfile_size(name)
                if sz >= 0:
                    total += sz
                    count += 1
                    entries.append((DRIVE_ROOT + name, sz, 0, DRIVE_ROOT + name))
                    root_stats.append(dict(root=DRIVE_ROOT + name, size=sz, count=1))
        elif sc == "vss":
            total, note2 = vss_storage_bytes()
            if total < 0:
                total, note = 0, note2
        elif sc == "recyclebin":
            total, _ = recyclebin_size()
            if total < 0:
                total = 0
        else:
            roots = expand_roots(cat.get("roots", []))
            if not roots:
                note = "本机未发现相关目录"
            for root in roots:
                if self.stop.is_set() or budget[0] <= 0:
                    break
                self._touch(root)
                if not plain_path(root):
                    continue
                if os.path.isfile(root):
                    try:
                        st = os.stat(root)
                        if retain:
                            if not protected_file(root, protected) and self._reserve_entry(root):
                                identities[root] = fingerprint(root)
                                entries.append((root, st.st_size, st.st_mtime, root))
                        latest = max(latest, st.st_mtime)
                        total += st.st_size
                        count += 1
                        root_stats.append(dict(root=root, size=st.st_size, count=1))
                    except OSError:
                        pass
                    continue

                r_size, r_count = 0, 0

                def on_file(p, s, m):
                    nonlocal total, count, r_size, r_count, latest
                    if retain and not protected_file(p, protected) and self._reserve_entry(p):
                        identities[p] = fingerprint(p)
                        entries.append((p, s, m, root))
                    latest = max(latest, m)
                    total += s
                    count += 1
                    r_size += s
                    r_count += 1

                def on_dir(p):
                    pass  # Empty directories are deliberately retained.

                walk_tree(root, self.stop, on_file, on_dir, budget)
                root_stats.append(dict(root=root, size=r_size, count=r_count))
                self._touch(root)

        if budget[0] <= 0:
            note = (note + " " if note else "") + "文件过多，仅统计前 %d 项" % MAX_FILES_PER_CAT
        if not is_admin() and cat.get("read_admin") and total == 0 and not note:
            note = "普通权限无法读取，需以管理员身份启动"
        with self.lock:
            info.update(size=total, count=count, note=note.strip(), status="done")
            tool = cat.get("tool", "")
            self.last_used[tool] = max(self.last_used.get(tool, 0), latest)
            self.identities.update(identities)
        self.entries[cid] = entries
        self.dirs[cid] = dirs
        self.roots_info[cid] = root_stats

    def snapshot(self):
        with self.lock:
            per = {cid: dict(v) for cid, v in self.per.items()}
            status, current = self.status, self.current
            started, ended = self.started, self.ended
        done = sum(1 for i in per.values() if i["status"] == "done")
        found = sum(i["size"] for i in per.values())
        total = sum(i["status"] != "not_scanned" for i in per.values())
        return dict(status=status, current=current, started=started, ended=ended,
                    scope=sorted(getattr(self, "scope", [])), partial=getattr(self, "partial", False),
                    manifest_bytes=getattr(self, "manifest_bytes", 0),
                    per=per, done=done, total=total, found=found,
                    percent=int(done * 100 / total) if total else 0)


# ----------------------------------------------------------------------------
# 删除语义：Windows 生产路径送回收站，失败跳过、绝不回退成永久删除
# ----------------------------------------------------------------------------
FO_DELETE = 3
FOF_ALLOWUNDO = 0x40
FOF_NOCONFIRMATION = 0x10
FOF_SILENT = 0x4
FOF_NOERRORUI = 0x400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]


def _build_pfrom(paths):
    """SHFileOperation 的 pFrom：多路径 \0 分隔、必须双 \0 结尾
    （create_unicode_buffer 会在内容后再补一个终止符，所以内容只需一个 \0）"""
    return ctypes.create_unicode_buffer("\0".join(paths) + "\0")


def _sh_recycle(op_ptr):
    """实际 Shell 调用入口（测试中替换此函数以 mock，不碰真实回收站）"""
    return ctypes.windll.shell32.SHFileOperationW(op_ptr)


def _permanent_delete_allowed():
    """Test adapter only; delete_files additionally enforces the sandbox path."""
    return bool(os.environ.get("CLEAR_C_SANDBOX"))



def delete_files(paths):
    """删除一组文件，返回 (成功路径, 失败路径)。

    Windows 生产路径用 SHFileOperationW 送回收站（FOF_ALLOWUNDO），
    单批 pFrom 控制在约 30KB 内，批失败降级为逐个调用，单个仍失败计入失败——
    任何情况下都不会回退成 os.remove。"""
    if is_admin():
        return [], list(paths)
    if _permanent_delete_allowed():
        ok, failed = [], []
        for p in paths:
            try:
                if not within(p, os.environ["CLEAR_C_SANDBOX"]) or not plain_path(p):
                    raise OSError("outside test sandbox")
                os.remove(p)
                ok.append(p)
            except OSError:
                failed.append(p)
        return ok, failed
    if not IS_WIN:
        return [], list(paths)
    ok, failed = [], []
    batch, batch_len = [], 0
    for p in paths:
        batch.append(p)
        batch_len += len(p) + 1
        if batch_len >= 15000:  # 约 30KB wchar 上限，超了分批
            good, bad = _recycle_batch(batch)
            ok += good
            failed += bad
            batch, batch_len = [], 0
    if batch:
        good, bad = _recycle_batch(batch)
        ok += good
        failed += bad
    return ok, failed


def _recycle_batch(batch):
    op = SHFILEOPSTRUCTW()
    buf = _build_pfrom(batch)
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    if _sh_recycle(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted:
        return list(batch), []
    if len(batch) == 1:
        log("无法送入回收站: " + batch[0])
        return [], list(batch)
    good, failed = [], []
    for p in batch:  # 整批失败降级为逐个，定位失败文件
        g, b = _recycle_batch([p])
        good += g
        failed += b
    return good, failed


# ----------------------------------------------------------------------------
# 清理任务
# ----------------------------------------------------------------------------
class CleanJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.per = {}
        self.current = ""
        self.dry = False
        self.via = "recycle"
        self.samples = {}
        self.stop = threading.Event()
        self.started = 0.0

    def start(self, plan, dry):
        if is_admin() and not dry:
            return False, "管理员模式仅允许只读扫描，请以普通权限启动"
        modes = {it.get("delete_mode", "recycle") for it in plan.values()}
        if len(modes) != 1 or not modes.issubset({"recycle", "permanent"}):
            return False, "无效的清理方式"
        mode = next(iter(modes))
        if mode == "permanent" and any(not direct_roots(cid) or CAT_BY_ID[cid].get("locked")
                                       or CAT_BY_ID[cid].get("risk") in ("danger", "migrate") for cid in plan):
            return False, "直接删除只允许已验证的临时文件和下载缓存目录"
        with self.lock:
            if self.status in ("running", "stopping"):
                return False, "已有清理任务正在进行"
            if not OPERATION_LOCK.acquire(blocking=False):
                return False, "已有扫描或清理任务进行中"
            if SCAN.status not in ("done", "stopped") or any(it.get("scan_id") != SCAN.scan_id or time.time() > it.get("expires", 0) for it in plan.values()):
                OPERATION_LOCK.release()
                return False, "计划已过期，请重新扫描并确认"
            self.status = "running"
            self.via = "permanent" if mode == "permanent" or _permanent_delete_allowed() else "recycle"
            self.samples = {}
            self.per = {cid: dict(status="pending", freed=0, skipped=0, recycled=0,
                                  note="", size=it["size"])
                        for cid, it in plan.items()}
            self.dry = bool(dry)
            self.plan_token = next(iter(plan.values())).get("plan_token", "")
            self.current = ""
            self.stop = threading.Event()
            self.started = time.time()
            if not dry:
                SCAN.status = "stale"
        threading.Thread(target=self._run, args=(plan,), daemon=True).start()
        return True, ""

    def stop_clean(self):
        with self.lock:
            if self.status == "running":
                self.stop.set()
                self.status = "stopping"

    def _run(self, plan):
        try:
            self._clean_run(plan)
        except Exception as exc:
            with self.lock:
                self.status = "error"
                self.current = str(exc)
        finally:
            if not self.dry:
                SCAN.status = "stale"
            OPERATION_LOCK.release()

    def _clean_run(self, plan):
        running_processes()  # 刷新进程缓存，供运行中警告与历史记录使用
        for cid, it in plan.items():
            if self.stop.is_set():
                break
            self._clean_one(cid, it)
        stopped = self.stop.is_set()
        with self.lock:
            for info in self.per.values():
                if info["status"] in ("running", "pending"):
                    info["status"] = "skipped" if stopped else "done"
            self.status = "stopped" if stopped else "done"
        if not self.dry:
            self._write_history(plan)

    def _clean_one(self, cid, it):
        info = self.per[cid]
        cat = CAT_BY_ID[cid]
        info["status"] = "running"
        note, freed, skipped = "", 0, 0

        sp = cat.get("special_clean")
        if sp == "recycle":
            if self.dry:
                freed, note = it["size"], "预览：将清空 %s 盘回收站（当前用户）" % DRIVE
            else:
                ok, msg = empty_recycle_bin()
                freed = it["size"] if ok else 0
                skipped = 0 if ok else 1
                note = ("已清空 %s 盘回收站（当前用户）" % DRIVE) if ok else ("清空回收站失败: " + msg)
        elif sp:
            skipped, note = 1, "系统变更已停用，请使用 Windows 系统工具"
        else:
            freed, skipped, note = self._clean_generic(cat, it)

        info["freed"] = freed
        info["skipped"] = skipped
        info["note"] = note
        if info["status"] == "running":
            info["status"] = "done"

    def _clean_generic(self, cat, it):
        entries, dirs = it["entries"], it["dirs"]
        min_age = cat.get("min_age_min", 0) * 60
        direct = it.get("delete_mode") == "permanent"
        if direct:
            min_age = max(min_age, DIRECT_MIN_AGE)
        now = time.time()
        freed = skipped = removed = 0
        protected = protected_roots()
        self.samples[cat["id"]] = []
        for offset in range(0, len(entries), 64):
            if self.stop.is_set():
                skipped += len(entries) - offset
                break
            keep = []
            for e in entries[offset:offset + 64]:
                p, size, mt, root = e
                self.current = p
                try:
                    if (not within(p, root) or not plain_path(p) or protected_file(p, protected)
                            or fingerprint(p) != it["identities"].get(p)
                            or (min_age and time.time() - os.lstat(p).st_mtime < min_age)):
                        skipped += 1
                        continue
                except (OSError, ValueError):
                    skipped += 1
                    continue
                keep.append((p, size))
            if self.dry:
                freed += sum(size for _, size in keep)
            else:
                if direct:
                    ok, failed = delete_direct_files(cat, [p for p, _ in keep], it["identities"], self.stop)
                else:
                    ok, failed = delete_files([p for p, _ in keep])
                sizes = dict(keep)
                freed += sum(sizes.get(p, 0) for p in ok)
                skipped += len(failed)
                removed += len(ok)
                self.samples[cat["id"]] = (self.samples[cat["id"]] + ok)[:5]
                if self.via == "recycle":
                    self.per[cat["id"]]["recycled"] = removed
            self.per[cat["id"]].update(freed=freed, skipped=skipped)
        if self.dry:
            head = "预览模式，未实际删除"
        elif self.via == "permanent":
            head = "已永久删除 %d 个文件" % removed
        else:
            head = "已送入回收站 %d 个文件" % removed
        tail = ("；跳过 %d 个（被占用/文件变化/保留期内/操作失败）" % skipped) if skipped else ""
        return freed, skipped, head + tail

    def _write_history(self, plan):
        try:
            with self.lock:
                per = {cid: dict(i) for cid, i in self.per.items()}
            running = []
            for tid in {CAT_BY_ID.get(cid, {}).get("tool") for cid in plan}:
                if tid and TOOLS.get(tid) and tool_running(tid):
                    running.append(TOOLS[tid].get("name", tid))
            record = dict(
                ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
                via=self.via,
                ids=sorted(plan.keys()),
                total_freed=sum(i["freed"] for i in per.values()),
                total_skipped=sum(i["skipped"] for i in per.values()),
                per={cid: dict(freed=i["freed"], skipped=i["skipped"],
                               recycled=i.get("recycled", 0), note=i["note"])
                     for cid, i in per.items()},
                samples={cid: self.samples.get(cid, []) for cid in plan},
                running_tools=sorted(set(running)),
            )
            append_history(record)
        except Exception as e:
            log("写入清理历史失败: " + str(e))

    def snapshot(self):
        with self.lock:
            per = {cid: dict(v) for cid, v in self.per.items()}
            status, current = self.status, self.current
        total_freed = sum(i["freed"] for i in per.values())
        total_skipped = sum(i["skipped"] for i in per.values())
        involved = {CAT_BY_ID.get(cid, {}).get("tool") for cid in per}
        untouched_locked = [c["id"] for c in CATEGORIES
                            if c.get("locked") and c.get("tool") in involved]
        untouched_migrate = [c["id"] for c in CATEGORIES
                             if c.get("risk") == "migrate" and c.get("tool") in involved
                             and SCAN.per.get(c["id"], {}).get("size", 0) > 0]
        return dict(status=status, current=current, per=per,
                    plan_token=getattr(self, "plan_token", ""),
                    total_freed=total_freed, total_skipped=total_skipped,
                    via=self.via,
                    report=dict(untouched_locked=untouched_locked,
                                untouched_migrate=untouched_migrate))


SCAN = ScanJob(CATEGORIES)
CLEAN = CleanJob()


# ----------------------------------------------------------------------------
# 迁移（Hugging Face / Ollama / LM Studio）：复制到目标盘 + 原路径建目录联接
# 从严原则：先整目录复制 -> 校验 -> 源改名备份 -> 建联接 -> 验证 -> 删备份；
# 任何一步失败都会回滚，源文件保持原样
# ----------------------------------------------------------------------------
def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def dir_size(path):
    total = 0
    for r, _, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(r, f)).st_size
            except OSError:
                pass
    return total


class MoveJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"   # idle/running/done/error
        self.note = ""
        self.error = ""
        self.dry = False
        self.info = {}

    def start(self, tool_id, target_drive, dry):
        if not dry:
            return False, "迁移目前仅支持预览；事务校验和恢复验收后再开放执行"
        with self.lock:
            if self.status == "running":
                return False, "已有迁移任务进行中"
        root_tpl = ""
        for c in CATEGORIES:
            if c.get("moveable") and c.get("tool") == tool_id:
                root_tpl = c.get("move_root", "")
                break
        if not root_tpl:
            return False, "该工具不支持迁移"
        src = E(root_tpl)
        if not plain_path(src) or not os.path.isdir(src):
            return False, "本机未找到目录: " + src
        if not re.fullmatch(r"[A-Za-z]:?", target_drive or ""):
            return False, "目标必须是盘符，例如 D"
        target_drive = target_drive[0].upper()
        if not target_drive or not os.path.exists(target_drive + ":\\"):
            return False, "目标盘不存在: " + target_drive
        dst = target_drive + ":\\DeepCleanMoved\\" + tool_id
        with self.lock:
            self.dry = bool(dry)
            self.status = "running"
            self.note = ""
            self.error = ""
            self.info = dict(src=src, dst=dst, tool=tool_id)
        threading.Thread(target=self._run, args=(src, dst, bool(dry)), daemon=True).start()
        return True, ""

    def _run(self, src, dst, dry):
        try:
            total = dir_size(src)
            free = shutil.disk_usage(dst[:2] + "\\").free
            with self.lock:
                self.info.update(size=total, free=free)
            if total == 0:
                raise RuntimeError("源目录为空，无需迁移")
            if total > free:
                raise RuntimeError("目标盘空间不足：需要 %s，剩余 %s" % (fmt_size(total), fmt_size(free)))
            if dry:
                with self.lock:
                    self.status = "done"
                    self.note = "预览：将迁移 %s 到 %s" % (fmt_size(total), dst)
                return
            raise RuntimeError("迁移执行已停用")
        except Exception as e:
            with self.lock:
                self.status = "error"
                self.error = str(e)

    def snapshot(self):
        with self.lock:
            return dict(status=self.status, note=self.note, error=self.error,
                        dry=self.dry, info=dict(self.info))


MOVE = MoveJob()


# ----------------------------------------------------------------------------
# 清理历史（仅本机：%LOCALAPPDATA%\DeepClean\history.jsonl，DEEPCLEAN_HISTORY 可覆盖）
# 记录每次清理的汇总与少量样例路径，不做按文件还原
# ----------------------------------------------------------------------------
def history_file():
    return os.environ.get("DEEPCLEAN_HISTORY") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")),
        "DeepClean", "history.jsonl")


def append_history(record):
    path = history_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:  # 超过 1MB 从头切掉最旧行，保留最近约 200 行
        if os.path.getsize(path) > 1024 * 1024:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-200:]) + "\n")
    except OSError:
        pass


def read_history(limit):
    try:
        with open(history_file(), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    items = []
    for line in reversed(lines):  # 最新在前
        try:
            items.append(json.loads(line))
        except Exception:
            continue
        if len(items) >= limit:
            break
    return items


def build_clean_plan(ids, excluded=None, confirm_danger=False, delete_mode="recycle"):
    if delete_mode not in ("recycle", "permanent"):
        return None, "无效的清理方式"
    if delete_mode == "permanent" and any(not direct_roots(cid) for cid in ids):
        return None, "直接删除只允许已验证的临时文件和下载缓存目录"
    if OPERATION_LOCK.locked():
        return None, "已有扫描或清理任务进行中，请等待完成"
    if SCAN.status in ("running", "stopping"):
        return None, "扫描正在进行中，请等待完成或先停止扫描"
    if SCAN.status not in ("done", "stopped"):
        return None, "请先完成一次扫描"
    if getattr(SCAN, "partial", False):
        return None, "扫描清单达到全局预算，请缩小范围后重新扫描；本次结果仅供统计"
    if "recycle-bin" in set(ids) and len(set(ids)) > 1:
        # 送回收站的文件可能马上被 SHEmptyRecycleBin 倒掉，禁止同一趟混合清理
        return None, "清空回收站请单独执行，不能与其它清理项一起勾选"
    excluded = set(excluded or [])
    plan = {}
    for cid in ids:
        cat = CAT_BY_ID.get(cid)
        if not cat:
            continue
        # 安全红线：锁定项（会话/历史）与迁移项永远不会进入清理计划；
        # 未锁定的危险分项必须显式确认（confirm_danger）才会进入
        if cat.get("locked") or cat.get("risk") == "migrate":
            continue
        if cat.get("risk") == "danger" and not confirm_danger:
            continue
        if SCAN.per.get(cid, {}).get("status") not in ("done",):
            continue
        entries = SCAN.entries.get(cid, [])
        if excluded:
            # 第 4 位是所属 root 目录，被排除的目录整个跳过
            entries = [e for e in entries if not any(within(e[0], root) for root in excluded)]
        if delete_mode == "permanent":
            entries = [e for e in entries if direct_eligible(cat, e[0], SCAN.identities.get(e[0]))]
            if not entries:
                continue
        if cat.get("special_size"):
            # 回收站/还原点/系统文件等特殊类别：大小来自系统接口，无法按目录拆分
            size = SCAN.per.get(cid, {}).get("size", 0)
        else:
            size = sum(e[1] for e in entries)
        plan[cid] = dict(size=size, entries=entries, dirs=[], delete_mode=delete_mode,
                         scan_id=SCAN.scan_id, expires=time.time() + 300,
                         identities={e[0]: SCAN.identities.get(e[0]) for e in entries})
    if not plan:
        return None, ("没有符合条件的旧文件：仅处理白名单内 7 天未修改的文件，管理员模式不可执行"
                      if delete_mode == "permanent" else "未选择任何清理项")
    return plan, ""


def relaunch_as_admin():
    if OPERATION_LOCK.locked():
        return False, "请先等待当前扫描或清理结束"
    if not IS_WIN:
        return False, "仅支持 Windows"
    if is_admin():
        return False, "当前已经是管理员模式"
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, '"%s"' % os.path.abspath(__file__), APP_DIR, 1)
        if rc <= 32:
            return False, "未能启动（可能被取消）"
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return True, "正在以管理员身份重新启动新窗口…"
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------
# 命令行模式（供 AI 助手 / 脚本调用；不带 cli 子命令时仍启动网页界面）
# ----------------------------------------------------------------------------
CATEGORY_NAMES = {"assistant": "编程助手", "model": "本地模型", "cn-app": "国内应用",
                  "dev": "开发缓存", "chat": "聊天应用", "system": "系统垃圾"}


def _cli_wait(job, label, total):
    """同步等待后台任务完成，进度写到 stderr（不污染 stdout 的 JSON）"""
    last = -1
    while job.status in ("running", "stopping"):
        done = sum(1 for i in job.per.values() if i.get("status") == "done")
        if done != last:
            last = done
            print("\r%s: %d/%d" % (label, done, total), end="", file=sys.stderr, flush=True)
        time.sleep(0.15)
    print("", file=sys.stderr)


def _cli_scan_if_needed(ids=None):
    """确保已有一份完成的扫描结果，没有就同步扫一次"""
    if SCAN.status in ("done", "stopped"):
        return True, ""
    if CLEAN.status == "running":
        return False, "已有清理任务进行中，请稍后再试"
    ok, err = SCAN.start(ids)
    if not ok:
        return False, err
    _cli_wait(SCAN, "扫描中", len(SCAN.cats))
    return SCAN.status == "done", "" if SCAN.status == "done" else "扫描未完成"


def _cli_state_payload():
    """扫描完成后的 tools+buckets+totals 汇总（scan 输出）"""
    du = shutil.disk_usage(DRIVE_ROOT)
    running_processes()
    tools = []
    for tid, t in TOOLS.items():
        size = sum(SCAN.per.get(c["id"], {}).get("size", 0)
                   for c in CATEGORIES if c.get("tool") == tid)
        tools.append(dict(id=tid, name=t.get("name", tid),
                          category=t.get("category", "system"),
                          running=tool_running(tid), last_used_days=last_used_days(tid),
                          size=size))
    buckets = []
    for c in CATEGORIES:
        i = SCAN.per.get(c["id"], {})
        buckets.append(dict(id=c["id"], tool=c.get("tool", ""), name=c.get("name", c["id"]),
                            risk=c.get("risk", "safe"), locked=bool(c.get("locked")),
                            moveable=bool(c.get("moveable")),
                            cleanable=not c.get("locked") and c.get("risk") != "migrate",
                            size=i.get("size", 0), count=i.get("count", 0),
                            note=i.get("note", ""),
                            roots=[dict(root=r["root"], size=r["size"], count=r["count"])
                                   for r in SCAN.roots_info.get(c["id"], []) if r["size"] > 0]))
    cleanable = [c for c in CATEGORIES if not c.get("locked") and c.get("risk") != "migrate"]
    totals = dict(
        safe=sum(SCAN.per.get(c["id"], {}).get("size", 0) for c in cleanable if c.get("risk") == "safe"),
        review=sum(SCAN.per.get(c["id"], {}).get("size", 0) for c in cleanable if c.get("risk") == "rebuildable"),
        migrate=sum(SCAN.per.get(c["id"], {}).get("size", 0) for c in CATEGORIES if c.get("risk") == "migrate"),
    )
    return dict(tools=tools, buckets=buckets, totals=totals,
                drive=dict(free=du.free, total=du.total))


def run_cli(args):
    import argparse
    p = argparse.ArgumentParser(
        prog="python app.py cli",
        description="深清 DeepClean 命令行模式（stdout 输出 JSON，进度走 stderr）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("categories", help="列出全部分项及安全级说明")
    sp = sub.add_parser("scan", help="扫描并输出各工具/分项大小与目录明细")
    sp.add_argument("--json", action="store_true", default=True, help="以 JSON 输出（默认行为）")
    sp.add_argument("--ids", help="只扫描指定分项，逗号分隔")
    cp = sub.add_parser("clean", help="清理指定分项（需要时自动先扫描；locked/migrate 自动跳过，危险分项需 --confirm-danger）")
    cp.add_argument("--ids", required=True, help="分项 id，逗号分隔，如 npm-store,kimi-cache")
    cp.add_argument("--dry", action="store_true", help="预览模式，不实际删除")
    cp.add_argument("--permanent", action="store_true", help="仅直接删除白名单内至少 7 天未修改的文件，不能从回收站恢复")
    cp.add_argument("--yes", action="store_true", help="确认执行（不带时仅输出清理计划并以退出码 2 结束）")
    cp.add_argument("--confirm-danger", action="store_true",
                    help="未锁定的危险分项（risk=danger）需额外确认才会执行")
    cp.add_argument("--exclude-root", action="append", default=[], metavar="DIR",
                    help="排除指定目录不清理（可重复传入多个）")
    cp.add_argument("--json", action="store_true", default=True, help="以 JSON 输出（默认行为）")
    mp = sub.add_parser("move", help="迁移本地模型到其他盘（HF/Ollama/LM Studio，建立目录联接）")
    mp.add_argument("--tool", required=True, help="工具 id，如 ollama / hf / lmstudio")
    mp.add_argument("--to", default="D", help="目标盘符（默认 D）")
    mp.add_argument("--dry", action="store_true", help="预览模式，不实际迁移")
    a = p.parse_args(args)

    if a.cmd == "categories":
        cats = [dict(id=c["id"], tool=c.get("tool", ""), name=c.get("name", c["id"]),
                     category=c.get("category", "system"), risk=c.get("risk", "safe"),
                     locked=bool(c.get("locked")), moveable=bool(c.get("moveable")),
                     need_admin=c.get("clean_admin", False),
                     min_age_min=c.get("min_age_min", 0), desc=c.get("desc", ""))
                for c in CATEGORIES]
        print(json.dumps(dict(ok=True, buckets=cats), ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "scan":
        ok, err = _cli_scan_if_needed(a.ids.split(",") if a.ids else None)
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        snap = SCAN.snapshot()
        out = dict(ok=True, status=snap["status"], found=snap["found"], scope=snap["scope"], partial=snap["partial"])
        out.update(_cli_state_payload())
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "clean":
        ids = [x.strip() for x in (a.ids or "").split(",") if x.strip()]
        unknown = [x for x in ids if x not in CAT_BY_ID]
        if unknown:
            print(json.dumps(dict(ok=False, error="未知分项: " + ", ".join(unknown),
                                  hint="先运行 cli categories 查看可用分项"), ensure_ascii=False))
            return 1
        skipped = [x for x in ids if CAT_BY_ID[x].get("locked") or CAT_BY_ID[x].get("risk") == "migrate"]
        rest = [x for x in ids if x not in skipped]
        unconfirmed = [x for x in rest if CAT_BY_ID[x].get("risk") == "danger" and not a.confirm_danger]
        ids = [x for x in rest if x not in unconfirmed]
        if not ids:
            if skipped:
                print(json.dumps(dict(ok=False, error="所选分项均为锁定/迁移项，不会删除任何文件",
                                      skipped=skipped,
                                      hint="migrate 分项请使用 cli move 迁移"), ensure_ascii=False))
            else:
                print(json.dumps(dict(ok=False, error="所选分项均为危险分项，未确认不会执行",
                                      skipped_danger=unconfirmed,
                                      hint="确认风险可控后加 --confirm-danger 重新执行"), ensure_ascii=False))
            return 1
        ok, err = _cli_scan_if_needed(ids)
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        plan, err = build_clean_plan(ids, list(a.exclude_root), a.confirm_danger, "permanent" if a.permanent else "recycle")
        if plan is None:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        for tool in sorted({CAT_BY_ID.get(cid, {}).get("tool") for cid in plan}):
            if tool and tool_running(tool):
                print("警告: %s 正在运行。清理其缓存可能导致卡顿或写入失败，建议先退出再清。"
                      % TOOLS.get(tool, {}).get("name", tool), file=sys.stderr)
        if not a.yes and not a.dry:
            out = dict(ok=False, need_confirmation=True,
                       delete_mode="permanent" if a.permanent else "recycle",
                       message=("永久删除，不进入回收站；下载缓存删除后可能需要联网重新下载，源失效时可能无法恢复。" if a.permanent else "")
                               + "这是清理计划。确认无误后加 --yes 执行；只想看将删除的文件明细可加 --dry",
                       plan=[dict(id=cid, name=CAT_BY_ID[cid]["name"], **plan_summary(cid, it))
                             for cid, it in plan.items()],
                       total_size=sum(it["size"] for it in plan.values()),
                       skipped_locked=skipped)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        ok, err = CLEAN.start(plan, a.dry)
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        _cli_wait(CLEAN, "清理中", len(plan))
        snap = CLEAN.snapshot()
        du = shutil.disk_usage(DRIVE_ROOT)
        print(json.dumps(dict(ok=snap["status"] == "done", status=snap["status"], dry=bool(a.dry),
                              via=snap.get("via", "recycle"),
                              freed=snap["total_freed"], skipped=snap["total_skipped"],
                              free_now=du.free, skipped_locked=skipped, skipped_danger=unconfirmed,
                              per={cid: dict(status=i["status"], freed=i["freed"],
                                             skipped=i["skipped"], recycled=i.get("recycled", 0),
                                             note=i["note"])
                                    for cid, i in snap["per"].items()}),
                         ensure_ascii=False, indent=2))
        return 0 if snap["status"] == "done" else 1

    if a.cmd == "move":
        ok, err = MOVE.start(a.tool, a.to, a.dry)
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        while MOVE.status == "running":
            time.sleep(0.2)
        snap = MOVE.snapshot()
        print(json.dumps(dict(ok=snap["status"] == "done", **snap), ensure_ascii=False, indent=2))
        return 0 if snap["status"] == "done" else 1
    return 0


_proc_cache = {"t": 0.0, "names": set()}


def running_processes():
    """当前全部进程名（小写），60 秒缓存"""
    now = time.time()
    if now - _proc_cache["t"] > 60:
        rc, out = run_cmd(["tasklist", "/fo", "csv", "/nh"], 30)
        names = set()
        if rc == 0:
            for line in out.splitlines():
                parts = line.split('","')
                if parts:
                    names.add(parts[0].strip('"').strip().lower())
        _proc_cache["t"] = now
        _proc_cache["names"] = names
    return _proc_cache["names"]


def tool_running(tool_id):
    for p in TOOLS.get(tool_id, {}).get("processes", []):
        if p.lower() in _proc_cache["names"]:
            return True
    return False


def last_used_days(tool_id):
    ts = getattr(SCAN, "last_used", {}).get(tool_id)
    if not ts:
        return None
    return max(0, int((time.time() - ts) / 86400))


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
SUPPORT_IMAGES = {"/support/donate-wechat.png", "/support/donate-alipay.png"}


def support_links():
    """Explicit HTTPS links and the two bundled, unmodified payment images."""
    try:
        with open(os.path.join(_BASE, "support.json"), encoding="utf-8") as f:
            items = json.load(f).get("links", [])
        result = []
        for x in items:
            if not isinstance(x, dict) or not isinstance(x.get("label"), str):
                continue
            label_en = x["labelEn"] if isinstance(x.get("labelEn"), str) and x.get("labelEn") else x["label"]
            if isinstance(x.get("image"), str) and x["image"] in SUPPORT_IMAGES:
                result.append(dict(label=x["label"], labelEn=label_en, image=x["image"]))
            elif isinstance(x.get("url"), str):
                url = urlsplit(x["url"])
                if url.scheme == "https" and url.hostname and not url.username:
                    result.append(dict(label=x["label"], labelEn=label_en, url=x["url"]))
        return result[:5]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def api_state():
    du = shutil.disk_usage(DRIVE_ROOT)
    running_processes()
    tools = []
    for tid, t in TOOLS.items():
        tools.append(dict(id=tid, name=t.get("name", tid),
                          nameEn=t.get("nameEn", t.get("name", tid)),
                          category=t.get("category", "system"),
                          running=tool_running(tid),
                          last_used_days=last_used_days(tid)))
    buckets = []
    for c in CATEGORIES:
        i = SCAN.per.get(c["id"], {})
        buckets.append(dict(
            id=c["id"], tool=c.get("tool", ""), category=c.get("category", "system"),
            risk=c.get("risk", "safe"), locked=bool(c.get("locked")), scanned=i.get("status") == "done",
            direct_delete=bool(direct_roots(c["id"])),
            moveable=bool(c.get("moveable")), move_root=c.get("move_root", ""),
            default_off=bool(c.get("default_off")),
            name=c.get("name", c["id"]), nameEn=c.get("nameEn", c["id"]),
            desc=c.get("desc", ""), descEn=c.get("descEn", ""),
            special=bool(c.get("special_size") or c.get("special_clean")),
            paths=c.get("roots", []),
            min_age_min=c.get("min_age_min", 0),
            view_admin=bool(c.get("read_admin") and not is_admin()),
            need_admin=bool(c.get("clean_admin") and not is_admin()),
            size=i.get("size", 0), count=i.get("count", 0),
            note=i.get("note", ""), status=i.get("status", "pending"),
        ))
    return dict(admin=is_admin(), win=IS_WIN, support_links=support_links(), drive=dict(total=du.total, used=du.used, free=du.free),
                drive_letter=DRIVE, tools=tools, buckets=buckets)


def api_roots():
    """各类别下实际参与清理的目录明细（含各自大小），供前端按目录勾选排除"""
    cats = {}
    for c in CATEGORIES:
        cid = c["id"]
        cats[cid] = dict(name=c["name"], group=c.get("group", "system"),
                         roots=list(SCAN.roots_info.get(cid, [])),
                         templates=c.get("roots", []),
                         note=SCAN.per.get(cid, {}).get("note", ""))
    return dict(ok=True, status=SCAN.status, categories=cats)


# 仅接受回环来源：Host/Origin 必须指向本机且端口与本服务一致（防 DNS rebinding
# 读取扫描结果），POST 拒绝跨站 Origin / Sec-Fetch-Site（防其它网页驱动本接口删除文件）
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY_BYTES = 1_000_000


def _host_port(value):
    """解析 Host/Origin 头，返回 (scheme, host, port)；无法解析时 port 为 None"""
    v = (value or "").strip().lower()
    scheme = ""
    if "://" in v:
        scheme, v = v.split("://", 1)
        v = v.split("/", 1)[0]
    if v.startswith("["):  # [::1]:8520
        host, _, rest = v[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else None
    elif v.count(":") == 1:
        host, port = v.rsplit(":", 1)
    else:
        host, port = v, None
    return scheme, host, (int(port) if port and port.isdigit() else None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClearC/1.0"

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, fmt, *args):
        pass

    def _guard(self, check_origin):
        """本机同源校验；不通过时直接应答 403 并返回 False"""
        port = self.server.server_address[1]
        _, host, hport = _host_port(self.headers.get("Host"))
        if host not in LOCAL_HOSTS or hport != port:
            self._json(dict(error="forbidden: untrusted host"), 403)
            return False
        if check_origin:
            if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
                self._json(dict(error="forbidden: cross-site request"), 403)
                return False
            # 任何 Origin 都必须精确匹配本机 http://<回环>:<端口>；
            # "null"（file:// 页面、沙箱 iframe）同样拒绝，只放行不带 Origin 的本地脚本/CLI
            origin = self.headers.get("Origin")
            if origin:
                scheme, ohost, oport = _host_port(origin)
                if scheme != "http" or ohost not in LOCAL_HOSTS or oport != port:
                    self._json(dict(error="forbidden: cross-origin request"), 403)
                    return False
        if self.path.split("?")[0].startswith("/api/"):
            token = self.headers.get("X-DeepClean-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), API_TOKEN.encode("ascii")):
                self._json(dict(error="Session expired. Please reopen the page from the launcher.", error_zh="会话失效，请通过启动程序重新打开页面", error_code="session_expired"), 401)
                return False
        return True

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._guard(False):
            return
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path in SUPPORT_IMAGES:
                with open(os.path.join(STATIC_DIR, "support", os.path.basename(path)), "rb") as f:
                    self._send(200, f.read(), "image/png")
            elif path == "/api/state":
                self._json(api_state())
            elif path == "/api/progress":
                self._json(SCAN.snapshot())
            elif path == "/api/clean/progress":
                self._json(CLEAN.snapshot())
            elif path == "/api/roots":
                self._json(api_roots())
            elif path == "/api/move/progress":
                self._json(MOVE.snapshot())
            elif path == "/api/history":
                limit = 20
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("limit="):
                            try:
                                limit = max(1, min(100, int(part[6:])))
                            except ValueError:
                                pass
                self._json(dict(ok=True, items=read_history(limit)))
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(dict(error="not found"), 404)
        except Exception as e:
            self._json(dict(error=str(e)), 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._guard(True):
            return
        try:
            length = self.headers.get("Content-Length") or "0"
            if not length.isascii() or not length.isdigit() or self.headers.get("Transfer-Encoding"):
                self._json(dict(error="invalid content length"), 400)
                return
            n = int(length)
            if n > MAX_BODY_BYTES:
                self._json(dict(error="payload too large"), 413)
                return
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeError):
                self._json(dict(error="invalid JSON"), 400)
                return
            if not isinstance(body, dict):
                self._json(dict(error="JSON object required"), 400)
                return
            for key in ("dry", "confirm_danger", "confirm_permanent"):
                if key in body and not isinstance(body[key], bool):
                    self._json(dict(error="boolean required: " + key), 400)
                    return
            for key, limit in (("ids", 64), ("excluded_roots", 512)):
                if key in body and (not isinstance(body[key], list) or len(body[key]) > limit or any(not isinstance(x, str) for x in body[key])):
                    self._json(dict(error="invalid list: " + key), 400)
                    return
            if path == "/api/scan/start":
                ok, err = SCAN.start(body.get("ids"))
                self._json(dict(ok=ok, error=err))
            elif path == "/api/shutdown":
                if MOVE.status == "running":
                    self._json(dict(ok=False, error="请等待迁移预览结束后再退出"))
                    return
                if not OPERATION_LOCK.acquire(blocking=False):
                    self._json(dict(ok=False, error="请先停止并等待当前任务结束，再退出"))
                    return
                self._json(dict(ok=True))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == "/api/scan/stop":
                SCAN.stop_scan()
                self._json(dict(ok=True))
            elif path in ("/api/clean", "/api/clean/preview"):
                if path == "/api/clean" and not body.get("dry"):
                    with PLAN_LOCK:
                        plan = HTTP_PLANS.pop(str(body.get("plan_token", "")), None)
                    if plan is None:
                        self._json(dict(ok=False, error="请先预览并确认清理计划"))
                        return
                    if any(it.get("delete_mode") == "permanent" for it in plan.values()) and body.get("confirm_permanent") is not True:
                        self._json(dict(ok=False, error="永久删除需要明确确认，请重新预览"))
                        return
                    ok, err = CLEAN.start(plan, False)
                    self._json(dict(ok=ok, error=err))
                    return
                ids = [str(x) for x in (body.get("ids") or [])][:64]
                excluded = [str(x) for x in (body.get("excluded_roots") or [])][:512]
                plan, err = build_clean_plan(ids, excluded, bool(body.get("confirm_danger")), body.get("delete_mode", "recycle"))
                if plan is None:
                    self._json(dict(ok=False, error=err))
                    return
                if path == "/api/clean/preview":
                    token = secrets.token_urlsafe(24)
                    for item in plan.values():
                        item["plan_token"] = token
                    with PLAN_LOCK:
                        HTTP_PLANS.clear()  # One outstanding approval; another preview supersedes it.
                        HTTP_PLANS[token] = plan
                    self._json(dict(ok=True, plan_token=token, delete_mode=body.get("delete_mode", "recycle"),
                                    per={cid: plan_summary(cid, it) for cid, it in plan.items()}))
                    return
                ok, err = CLEAN.start(plan, bool(body.get("dry")))
                self._json(dict(ok=ok, error=err))
            elif path == "/api/clean/stop":
                CLEAN.stop_clean()
                self._json(dict(ok=True))
            elif path == "/api/move":
                tool = str(body.get("tool") or "")
                to = str(body.get("to") or "D")
                dry = bool(body.get("dry"))
                ok, err = MOVE.start(tool, to, dry)
                self._json(dict(ok=ok, error=err))
            elif path == "/api/relaunch_admin":
                ok, msg = relaunch_as_admin()
                self._json(dict(ok=ok, msg=msg))
            elif path == "/api/open_recycle":
                if IS_WIN:
                    run_cmd(["explorer.exe", "shell:RecycleBinFolder"], 15)
                    self._json(dict(ok=True))
                else:
                    self._json(dict(ok=False, error="仅支持 Windows"))
            else:
                self._json(dict(error="not found"), 404)
        except Exception as e:
            self._json(dict(error=str(e)), 500)


def find_port():
    for p in range(8520, 8541):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)]


def _shell_open(url):
    """ShellExecute 打开 URL（带 SEE_MASK_FLAG_NO_UI）。

    os.startfile 不带 NO_UI：Windows 沙盒 / 精简系统没有注册 http 关联时，
    会先弹出系统「无法打开此 http 链接」错误框。这里带 NO_UI 标志，
    失败时静默返回 False，交由上层走浏览器路径兜底。
    """
    if not IS_WIN:
        return False
    try:
        sei = SHELLEXECUTEINFO()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = 0x00000400  # SEE_MASK_FLAG_NO_UI
        sei.nShow = 1           # SW_SHOWNORMAL
        sei.lpFile = url
        return bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)))
    except Exception:
        return False


def _has_http_association():
    """注册表里是否登记了 http 协议的打开方式（Windows 沙盒等精简系统没有）"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\http\UserChoice") as k:
            if winreg.QueryValueEx(k, "ProgId")[0]:
                return True
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"http\shell\open\command") as k:
            return bool(winreg.QueryValueEx(k, None)[0])
    except OSError:
        return False


def _open_browser(url):
    """打开结果页面。

    已知浏览器安装路径优先：Windows 沙盒等精简系统的 http 协议关联不可靠
    （注册表项可能存在但指向不可用的处理器，ShellExecute 还会弹系统错误框），
    所以只要在本机找到 Edge/Chrome/Firefox 就直接拉起，完全不碰协议关联；
    实在没有已知浏览器时才尝试 ShellExecute（NO_UI），最后弹窗给出地址。
    """
    if IS_WIN:
        for browser in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        ):
            if browser and os.path.exists(browser):
                log("打开结果页面: " + browser)
                try:
                    subprocess.Popen([browser, url])
                    return
                except Exception as e:
                    log("启动浏览器失败 %s: %s" % (browser, e))
        if _has_http_association() and _shell_open(url):
            log("已通过系统默认浏览器打开")
            return
        log("未找到已知浏览器且默认浏览器打开失败")
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "深清已在后台运行。\n\n请用浏览器打开：%s\n\n（关闭本提示不影响清理功能）" % url,
                "深清 DeepClean", 0x40)
        except Exception:
            pass
    else:
        webbrowser.open(url)


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can share an occupied TCP port with another process.
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if IS_WIN:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    root = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else APP_DIR
    lease = Instance(root, is_admin())
    owner = lease.acquire()
    stopping = "--stop" in sys.argv
    if not owner:
        for _ in range(30):
            try:
                session = lease.session()
                url = "http://127.0.0.1:%d" % session["port"]
                request = urllib.request.Request(url + ("/api/shutdown" if stopping else "/api/progress"),
                    data=b"{}" if stopping else None,
                    headers={"X-DeepClean-Token": session["token"], "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    result = json.load(response)
                if stopping and not result.get("ok"):
                    raise RuntimeError(result.get("error", "退出失败"))
                if not stopping and os.environ.get("CLEAR_C_NO_BROWSER") != "1":
                    _open_browser(url + "/#token=" + session["token"])
                return
            except (OSError, ValueError):
                time.sleep(.1)
        raise RuntimeError("已有实例但暂时无法连接，请稍后重试")
    try:
        if stopping:
            return
        httpd = None
        for port in range(8520, 8541):
            try:
                httpd = LocalHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        if httpd is None:
            raise RuntimeError("未找到可用端口")
        httpd.daemon_threads = True
        lease.publish(port, API_TOKEN)
        log("深清已启动: http://127.0.0.1:%d/" % port)
        if os.environ.get("CLEAR_C_NO_BROWSER") != "1":
            _open_browser("http://127.0.0.1:%d/#token=%s" % (port, API_TOKEN))
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
    finally:
        lease.close()


def _fatal(msg):
    log(msg)
    if IS_WIN:
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, "深度C盘清理 启动失败", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512 * 1024:
            os.remove(LOG_FILE)
        if len(sys.argv) > 1 and sys.argv[1] == "cli":
            root = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else APP_DIR
            lease = Instance(root, is_admin())
            if not lease.acquire():
                print(json.dumps(dict(ok=False, error="已有实例运行，请先退出网页实例再使用 CLI")))
                sys.exit(1)
            try:
                sys.exit(run_cli(sys.argv[2:]))
            finally:
                lease.close()
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        _fatal("深度C盘清理启动失败，详细信息已写入 clearc.log：\n\n" + traceback.format_exc())
