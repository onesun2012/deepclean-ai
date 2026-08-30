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
import socket
import subprocess
import sys
import threading
import time
import webbrowser
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
MAX_FILES_PER_CAT = 400000  # 单类别统计上限，防止极端目录拖垮内存


LOG_FILE = os.path.join(APP_DIR, "clearc.log")
# PyInstaller 单文件模式下 APP_DIR 在临时解包目录里，日志放到 exe 旁边才找得到
if getattr(sys, "frozen", False):
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "clearc.log")


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    try:
        print(line, flush=True)
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
                continue
            tools += data.get("tools", [])
            buckets += data.get("buckets", [])
    else:
        log("未找到规则目录: " + RULES_DIR)
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

CAT_BY_ID = {c["id"]: c for c in CATEGORIES}


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
                ("iNumItems", ctypes.c_int),
                ("i64Size", ctypes.c_int64),
                ("i64CurrentSize", ctypes.c_int64)]


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
        self.cats = cats
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

    def start(self):
        with self.lock:
            if self.status in ("running", "stopping"):
                return False, "正在扫描中，请先停止"
            self._reset()
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
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(self._scan_cat, c) for c in self.cats]
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
        # 聚合每个工具的最后使用时间（全部分项文件的最大 mtime）
        last = {}
        for cid, ents in self.entries.items():
            tool = CAT_BY_ID.get(cid, {}).get("tool", "")
            if not tool:
                continue
            for e in ents:
                m = e[2]
                if m and m > last.get(tool, 0):
                    last[tool] = m
        self.last_used = last

    def _scan_cat(self, cat):
        cid = cat["id"]
        info = self.per[cid]
        info["status"] = "running"
        entries, dirs, root_stats = [], [], []
        total, count, note = 0, 0, ""
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
                if os.path.isfile(root):
                    try:
                        st = os.stat(root)
                        entries.append((root, st.st_size, st.st_mtime, root))
                        total += st.st_size
                        count += 1
                        root_stats.append(dict(root=root, size=st.st_size, count=1))
                    except OSError:
                        pass
                    continue

                r_size, r_count = 0, 0

                def on_file(p, s, m):
                    nonlocal total, count, r_size, r_count
                    entries.append((p, s, m, root))
                    total += s
                    count += 1
                    r_size += s
                    r_count += 1

                def on_dir(p):
                    dirs.append(p)

                walk_tree(root, self.stop, on_file, on_dir, budget)
                root_stats.append(dict(root=root, size=r_size, count=r_count))
                self._touch(root)

        if budget[0] <= 0:
            note = (note + " " if note else "") + "文件过多，仅统计前 %d 项" % MAX_FILES_PER_CAT
        if not is_admin() and cat.get("read_admin") and total == 0 and not note:
            note = "普通权限无法读取，需以管理员身份启动"
        with self.lock:
            info.update(size=total, count=count, note=note.strip(), status="done")
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
        return dict(status=status, current=current, started=started, ended=ended,
                    per=per, done=done, total=len(per), found=found,
                    percent=int(done * 100 / len(per)) if per else 0)


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
        self.stop = threading.Event()
        self.started = 0.0

    def start(self, plan, dry):
        with self.lock:
            if self.status == "running":
                return False, "已有清理任务正在进行"
            self.status = "running"
            self.per = {cid: dict(status="pending", freed=0, skipped=0, note="", size=it["size"])
                        for cid, it in plan.items()}
            self.dry = bool(dry)
            self.current = ""
            self.stop = threading.Event()
            self.started = time.time()
        threading.Thread(target=self._run, args=(plan,), daemon=True).start()
        return True, ""

    def stop_clean(self):
        with self.lock:
            if self.status == "running":
                self.stop.set()
                self.status = "stopping"

    def _run(self, plan):
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
        elif sp in ("dism", "vss", "hiber", "pagefile"):
            if not is_admin():
                info["status"] = "need_admin"
                info["note"] = "需要以管理员身份启动才能执行"
                return
            if sp == "dism":
                info["note"] = "正在执行 DISM 组件清理，可能需要 10~30 分钟，请耐心等待…"
                self.current = "DISM /StartComponentCleanup"
                rc, out = run_cmd([os.path.join(WINDIR, "System32", "Dism.exe"),
                                   "/Online", "/Cleanup-Image", "/StartComponentCleanup"], timeout=None)
                note = "DISM 组件清理完成" if rc == 0 else "DISM 退出码 %s（系统正在使用时可能失败，稍后再试）" % rc
                self.current = ""
            elif sp == "vss":
                self.current = "vssadmin 删除还原点"
                rc, out = run_cmd(["vssadmin", "delete", "shadows", "/for=%s:" % DRIVE, "/all", "/quiet"], 600)
                freed = it["size"] if rc == 0 else 0
                skipped = 0 if rc == 0 else 1
                note = "已删除全部系统还原点" if rc == 0 else "删除还原点失败: " + out.strip()[:120]
                self.current = ""
            elif sp == "hiber":
                self.current = "powercfg /h off"
                rc, out = run_cmd(["powercfg", "/h", "off"], 60)
                freed = it["size"] if rc == 0 else 0
                skipped = 0 if rc == 0 else 1
                note = ("已关闭休眠功能并删除 hiberfil.sys；如需恢复请以管理员执行 powercfg /h on"
                        if rc == 0 else "执行失败: " + out.strip()[:120])
                self.current = ""
            elif sp == "pagefile":
                self.current = "注册表 ClearPageFileAtShutdown"
                rc, out = run_cmd(["reg", "add",
                                   r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
                                   "/v", "ClearPageFileAtShutdown", "/t", "REG_DWORD", "/d", "1", "/f"], 60)
                skipped = 0 if rc == 0 else 1
                note = ("已设置关机时自动清空页面文件，重启电脑后生效"
                        if rc == 0 else "设置失败: " + out.strip()[:120])
                self.current = ""
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
        now = time.time()
        freed = skipped = removed = 0
        n = len(entries)
        for i, e in enumerate(entries):
            p, s, mt = e[0], e[1], e[2]
            if self.stop.is_set():
                break
            if i % 40 == 0:
                self.current = p
            if min_age and mt and (now - mt) < min_age:
                skipped += 1
                continue
            if self.dry:
                freed += s
                continue
            try:
                os.remove(p)
                freed += s
                removed += 1
            except OSError:
                skipped += 1
        if not self.dry and not self.stop.is_set():
            for d in reversed(dirs):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
        head = "预览模式，未实际删除" if self.dry else "已删除 %d 个文件" % removed
        tail = ("；跳过 %d 个（被占用/无权限/保留期内）" % skipped) if skipped else ""
        return freed, skipped, head + tail

    def snapshot(self):
        with self.lock:
            per = {cid: dict(v) for cid, v in self.per.items()}
            status, current = self.status, self.current
        total_freed = sum(i["freed"] for i in per.values())
        total_skipped = sum(i["skipped"] for i in per.values())
        return dict(status=status, current=current, per=per,
                    total_freed=total_freed, total_skipped=total_skipped)


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
        if not os.path.isdir(src):
            return False, "本机未找到目录: " + src
        target_drive = (target_drive or "D").strip(":\\/")[:1].upper()
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
            if os.path.exists(dst):
                raise RuntimeError("目标目录已存在: " + dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copytree(src, dst, symlinks=True)  # 跨盘剪切 = 复制 + 校验 + 删源；symlinks 保留 HF 等缓存内的链接结构
            except Exception:
                shutil.rmtree(dst, ignore_errors=True)
                raise
            copied = dir_size(dst)
            if abs(copied - total) > max(1024 * 1024, total // 1000):
                shutil.rmtree(dst, ignore_errors=True)
                raise RuntimeError("复制校验不一致，已删除副本，原目录未动")
            backup = src + ".deepclean-bak"
            if os.path.exists(backup):
                shutil.rmtree(dst, ignore_errors=True)
                raise RuntimeError("存在旧备份目录 " + backup + "，请先手动处理")
            os.rename(src, backup)
            rc, out = run_cmd(["cmd", "/c", "mklink", "/J", src, dst], 30)
            if rc != 0 or not os.path.isdir(src):
                os.rename(backup, src)  # 回滚
                raise RuntimeError("创建目录联接失败: " + out.strip()[:160])
            shutil.rmtree(backup, ignore_errors=True)
            with self.lock:
                self.status = "done"
                self.note = "已迁移 %s 到 %s，原位置已建立目录联接，应用无需任何配置" % (fmt_size(total), dst)
        except Exception as e:
            with self.lock:
                self.status = "error"
                self.error = str(e)

    def snapshot(self):
        with self.lock:
            return dict(status=self.status, note=self.note, error=self.error,
                        dry=self.dry, info=dict(self.info))


MOVE = MoveJob()


def build_clean_plan(ids, excluded=None, confirm_danger=False):
    if SCAN.status in ("running", "stopping"):
        return None, "扫描正在进行中，请等待完成或先停止扫描"
    if SCAN.status not in ("done", "stopped"):
        return None, "请先完成一次扫描"
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
        entries = SCAN.entries.get(cid, [])
        if excluded:
            # 第 4 位是所属 root 目录，被排除的目录整个跳过
            entries = [e for e in entries if len(e) < 4 or e[3] not in excluded]
        if cat.get("special_size"):
            # 回收站/还原点/系统文件等特殊类别：大小来自系统接口，无法按目录拆分
            size = SCAN.per[cid]["size"]
        else:
            size = sum(e[1] for e in entries)
        plan[cid] = dict(size=size, entries=entries,
                         dirs=SCAN.dirs.get(cid, []))
    if not plan:
        return None, "未选择任何清理项"
    return plan, ""


def relaunch_as_admin():
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


def _cli_scan_if_needed():
    """确保已有一份完成的扫描结果，没有就同步扫一次"""
    if SCAN.status in ("done", "stopped"):
        return True, ""
    if CLEAN.status == "running":
        return False, "已有清理任务进行中，请稍后再试"
    ok, err = SCAN.start()
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
    cp = sub.add_parser("clean", help="清理指定分项（需要时自动先扫描；locked/migrate 自动跳过，危险分项需 --confirm-danger）")
    cp.add_argument("--ids", required=True, help="分项 id，逗号分隔，如 npm-store,kimi-cache")
    cp.add_argument("--dry", action="store_true", help="预览模式，不实际删除")
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
        ok, err = _cli_scan_if_needed()
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        snap = SCAN.snapshot()
        out = dict(ok=True, status=snap["status"], found=snap["found"])
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
        ok, err = _cli_scan_if_needed()
        if not ok:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        plan, err = build_clean_plan(ids, list(a.exclude_root), a.confirm_danger)
        if plan is None:
            print(json.dumps(dict(ok=False, error=err), ensure_ascii=False))
            return 1
        if not a.yes and not a.dry:
            out = dict(ok=False, need_confirmation=True,
                       message="这是清理计划。确认无误后加 --yes 执行；只想看将删除的文件明细可加 --dry",
                       plan=[dict(id=cid, name=CAT_BY_ID[cid]["name"], size=it["size"])
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
                              freed=snap["total_freed"], skipped=snap["total_skipped"],
                              free_now=du.free, skipped_locked=skipped, skipped_danger=unconfirmed,
                              per={cid: dict(status=i["status"], freed=i["freed"],
                                             skipped=i["skipped"], note=i["note"])
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
def api_state():
    du = shutil.disk_usage(DRIVE_ROOT)
    running_processes()
    tools = []
    for tid, t in TOOLS.items():
        tools.append(dict(id=tid, name=t.get("name", tid),
                          category=t.get("category", "system"),
                          running=tool_running(tid),
                          last_used_days=last_used_days(tid)))
    buckets = []
    for c in CATEGORIES:
        i = SCAN.per.get(c["id"], {})
        buckets.append(dict(
            id=c["id"], tool=c.get("tool", ""), category=c.get("category", "system"),
            risk=c.get("risk", "safe"), locked=bool(c.get("locked")),
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
    return dict(admin=is_admin(), drive=dict(total=du.total, used=du.used, free=du.free),
                drive_letter=DRIVE, tools=tools, buckets=buckets)


def api_roots():
    """各类别下实际参与清理的目录明细（含各自大小），供前端按目录勾选排除"""
    cats = {}
    for c in CATEGORIES:
        cid = c["id"]
        cats[cid] = dict(name=c["name"], group=c.get("group", "system"),
                         roots=list(SCAN.roots_info.get(cid, [])),
                         templates=c.get("roots", []),
                         note=SCAN.per[cid].get("note", ""))
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
        return True

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY_BYTES:
                self._json(dict(error="payload too large"), 413)
                return
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            if path == "/api/scan/start":
                ok, err = SCAN.start()
                self._json(dict(ok=ok, error=err))
            elif path == "/api/scan/stop":
                SCAN.stop_scan()
                self._json(dict(ok=True))
            elif path == "/api/clean":
                ids = [str(x) for x in (body.get("ids") or [])][:64]
                excluded = [str(x) for x in (body.get("excluded_roots") or [])][:512]
                plan, err = build_clean_plan(ids, excluded, bool(body.get("confirm_danger")))
                if plan is None:
                    self._json(dict(ok=False, error=err))
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
        log("未找到已知浏览器且默认浏览器打开失败: " + url)
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "深清已在后台运行。\n\n请用浏览器打开：%s\n\n（关闭本提示不影响清理功能）" % url,
                "深清 DeepClean", 0x40)
        except Exception:
            pass
    else:
        webbrowser.open(url)


def main():
    port = find_port()
    if not port:
        log("未找到可用端口")
        sys.exit(1)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    log("深度C盘清理已启动: %s   （管理员模式: %s）" % (url, "是" if is_admin() else "否"))
    log("使用完毕后直接关闭本窗口/进程即可。")
    if os.environ.get("CLEAR_C_NO_BROWSER") != "1":
        _open_browser(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("已退出")


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
            sys.exit(run_cli(sys.argv[2:]))
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        _fatal("深度C盘清理启动失败，详细信息已写入 clearc.log：\n\n" + traceback.format_exc())
