#!/usr/bin/env python3
"""
AniPrep 动漫媒体文件规范化重命名工具
====================================

将动漫视频文件及其外挂字幕重命名为 Emby/Jellyfin 友好格式：
    S{季号}E{集号}_{原文件名}.ext

依赖：Python 3.8+ 标准库（tkinter） + requests
"""

import os
import re
import sys
import ctypes
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import json
import requests

# ============================================================================
#  配置持久化
# ============================================================================

# 配置路径：EXE 和 .py 均指向可执行文件所在目录
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = _BASE_DIR / "aniprep_config.json"

class Config:
    """全局配置，读写 JSON 文件"""
    _defaults = {
        "tmdb_api_key": "",
        "tmdb_language": "zh-CN",
        "last_root_folder": "",
        "rename_folders": False,
        "window_geometry": "1160x720",
        "ui_scale": 1.0,          # 保存窗口尺寸时使用的界面缩放比
    }

    def __init__(self):
        self._data = dict(self._defaults)
        self._file_keys = set()
        self._load()

    def _load(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                for k in self._defaults:
                    if k in loaded:
                        self._data[k] = loaded[k]
                self._file_keys = {k for k in loaded if k in self._defaults}
        except Exception:
            pass

    def from_file(self, key: str) -> bool:
        """该键是否来自配置文件（用于区分「旧配置没有这个键」）。"""
        return key in self._file_keys

    def save(self):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name == '_data':
            super().__setattr__(name, value)
        elif name in self._data:
            self._data[name] = value
            self.save()
        else:
            super().__setattr__(name, value)

    def set(self, key, value):
        if key in self._data:
            self._data[key] = value
            self.save()


# ============================================================================
#  TMDB API 客户端
# ============================================================================

class TMDBClient:
    """TMDB API v3 封装"""
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, language: str = "zh-CN"):
        self.api_key = api_key
        self.language = language

    def _get(self, path: str, **params) -> Optional[dict]:
        params.setdefault("api_key", self.api_key)
        params.setdefault("language", self.language)
        try:
            r = requests.get(f"{self.BASE}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def search_tv(self, query: str) -> list[dict]:
        """搜索电视剧，返回结果列表 [{id, name, first_air_date, original_language, poster_path}, ...]"""
        data = self._get("/search/tv", query=query)
        return data.get("results", []) if data else []

    def get_seasons(self, tv_id: int) -> list[dict]:
        """获取所有季的信息 [{season_number, episode_count, name}, ...]"""
        data = self._get(f"/tv/{tv_id}")
        return data.get("seasons", []) if data else []

    def get_episodes(self, tv_id: int, season_number: int) -> list[dict]:
        """获取某季所有集 [{episode_number, name}, ...]"""
        data = self._get(f"/tv/{tv_id}/season/{season_number}")
        return data.get("episodes", []) if data else []

    def get_tv_name_year(self, tv_id: int) -> tuple:
        """返回 (剧名, 首播年份)"""
        data = self._get(f"/tv/{tv_id}")
        if not data:
            return ("", "")
        name = data.get("name", "")
        date = data.get("first_air_date", "")
        year = date[:4] if date else ""
        return (name, year)

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.m4v', '.ts', '.webm', '.rmvb'}
SUB_EXTS   = {'.ass', '.ssa', '.srt', '.sub', '.idx', '.sup', '.vtt'}

# Windows 文件名非法字符（含路径分隔符）
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*]')

# 分季子文件夹匹配模式
SEASON_PATTERNS = [
    re.compile(r'^[Ss](\d{1,2})$'),                  # S1, s01
    re.compile(r'^[Ss]eason\s*(\d{1,2})$', re.I),    # Season 1, season 01
    re.compile(r'^第\s*(\d{1,2})\s*季$'),             # 第1季, 第 2 季
]

# 集号匹配模式（按优先级）
EPISODE_PATTERNS = [
    re.compile(r'\[(\d{1,4})\]'),                    # [01] [001] [720]
    re.compile(r'第\s*(\d{1,4})\s*(?:话|集|話)'),     # 第01话 第720話
    re.compile(r'\b[Ee][Pp]\s*(\d{1,4})\b'),         # EP01 Ep001
    re.compile(r'(?:^|[\s._-])[Ss][Pp][._ ]?(\d{1,4})(?![a-zA-Z0-9])'),  # SP01 sp01 SP_01
    re.compile(r'-\s*(\d{1,4})\b'),                  # - 01
    re.compile(r'[._ ](\d{1,4})\.[^.]+$'),           # 文件名末尾 .001.mkv
    re.compile(r'[._ ](\d{1,4})$'),                  # 无扩展名时末尾 _001
]

# 已重命名文件的前缀检测（S01E01_xxx → 提取季/集/原名）
ALREADY_RENAMED_RE = re.compile(r'^S(\d{2})E(\d{2,3})_(.+)$', re.I)

# 可疑集号阈值
SUSPICIOUS_EPISODE = 500

# 路径长度警告阈值
PATH_LENGTH_WARN = 240


# ============================================================================
#  高 DPI 支持
# ============================================================================
#
#  Tk 默认不做 DPI 感知，Windows 会把整个窗口当位图拉伸（150% 下字发虚）。
#  这里让进程按物理像素渲染（文字变清晰），同时把所有「设计稿像素」用 px()
#  按缩放比换算，否则字变大了、间距没变，界面会挤成一团。
#  注意：点值字号由 Tk 按 DPI 自动放大，不需要额外处理。

DPI_SCALE = 1.0        # 界面缩放比，1.0 == 96dpi


def px(value: float) -> int:
    """设计稿像素 → 当前屏幕的实际像素。"""
    if DPI_SCALE == 1.0:
        return int(round(value))
    return int(round(value * DPI_SCALE))


def enable_dpi_awareness():
    """在创建 Tk 之前调用，让窗口按物理像素渲染（消除位图拉伸导致的模糊）。"""
    if os.name != 'nt':
        return
    try:
        # PROCESS_SYSTEM_DPI_AWARE：跟随系统缩放，窗口不会被拉伸
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def detect_ui_scale(root) -> float:
    """读取当前屏幕缩放比；可用 ANIPREP_UI_SCALE 环境变量强制指定。"""
    forced = os.environ.get('ANIPREP_UI_SCALE')
    if forced:
        try:
            return max(1.0, float(forced))
        except ValueError:
            pass
    try:
        return max(1.0, root.winfo_fpixels('1i') / 96.0)
    except Exception:
        return 1.0


def _scale_geometry(geometry: str, factor: float) -> str:
    """把 "1240x780+100+50" 按比例缩放。"""
    m = re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)', geometry.strip())
    if not m or factor == 1.0:
        return geometry
    w, h, x, y = m.groups()
    return (f"{int(int(w) * factor)}x{int(int(h) * factor)}"
            f"{int(int(x) * factor):+d}{int(int(y) * factor):+d}")


# ============================================================================
#  主题色彩
# ============================================================================

class Theme:
    """全局色彩定义 — 浅色简洁风（slate + indigo）"""
    # 背景层次
    bg              = "#F4F6F9"    # 页面底色
    surface         = "#FFFFFF"    # 卡片/面板
    surface_alt     = "#F8FAFC"    # 次级面板（表头、子行）
    surface_sunken  = "#F1F5F9"

    # 文字
    text            = "#0F172A"
    text_secondary  = "#64748B"
    text_muted      = "#94A3B8"

    # 主色
    accent          = "#4F6BED"
    accent_hover    = "#4058D4"
    accent_soft     = "#EEF1FE"
    accent_text     = "#FFFFFF"

    # 语义色
    success         = "#0E9F6E"
    success_soft    = "#E7F8F1"
    warning         = "#9A5B00"
    warning_soft    = "#FFF8E7"
    danger          = "#C62828"
    danger_soft     = "#FEF3F2"

    # 线条
    border          = "#E4E9F0"
    border_strong   = "#CBD5E1"
    grid            = "#EEF2F6"
    scrollbar       = "#CFD8E3"

    # 表格
    table_alt       = "#FAFBFD"
    table_hover     = "#F4F7FF"
    table_selected  = "#E9EEFF"

    # 输入控件
    input_bg        = "#FFFFFF"
    input_text      = "#0F172A"

    # 头部
    header_bg       = "#FFFFFF"
    header_text     = "#0F172A"


class Fonts:
    """运行时解析可用字体（中文优先），避免 Segoe UI 缺字回退导致的混排。"""

    family = "Segoe UI"
    mono   = "Consolas"
    body = ("Segoe UI", 10)
    small = ("Segoe UI", 9)
    bold = ("Segoe UI", 10, "bold")
    title = ("Segoe UI", 16, "bold")
    btn = ("Segoe UI", 10)
    btn_primary = ("Segoe UI", 11, "bold")
    chip = ("Segoe UI", 9)

    _UI_CANDIDATES = (
        "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
        "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", "Helvetica",
    )
    _MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Menlo", "Courier New")

    @classmethod
    def init(cls, root):
        try:
            available = set(tkfont.families(root))
        except tk.TclError:
            available = set()
        for name in cls._UI_CANDIDATES:
            if name in available:
                cls.family = name
                break
        for name in cls._MONO_CANDIDATES:
            if name in available:
                cls.mono = name
                break
        f = cls.family
        cls.body = (f, 10)
        cls.small = (f, 9)
        cls.bold = (f, 10, "bold")
        cls.title = (f, 16, "bold")
        cls.brand_sub = (f, 9)
        cls.btn = (f, 10)
        cls.btn_primary = (f, 11, "bold")
        cls.chip = (f, 9)
        cls.table = (f, 10)
        cls.table_head = (f, 9, "bold")


def draw_round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """在 Canvas 上画圆角矩形（平滑多边形近似）。"""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Chip(tk.Canvas):
    """圆角统计胶囊。"""

    TONES = {
        'neutral': ("#F1F5F9", "#64748B"),
        'accent':  ("#EEF1FE", "#4058D4"),
        'warn':    ("#FFF8E7", "#9A5B00"),
        'danger':  ("#FEF3F2", "#C62828"),
    }

    def __init__(self, parent, text: str = "", tone: str = 'neutral', height: int = 24):
        self._height = px(height)
        super().__init__(parent, height=self._height, highlightthickness=0, bd=0,
                         bg=parent.cget('bg'))
        self._text = text
        self._tone = tone
        self._font = tkfont.Font(family=Fonts.family, size=9)
        self._redraw()

    def set(self, text: str, tone: Optional[str] = None):
        if tone:
            self._tone = tone
        self._text = text
        self._redraw()

    def _redraw(self):
        self.delete('all')
        bg, fg = self.TONES.get(self._tone, self.TONES['neutral'])
        text_w = self._font.measure(self._text)
        w = text_w + px(22)
        h = self._height
        self.configure(width=w)
        draw_round_rect(self, 0, 0, w, h, h / 2, fill=bg, outline=bg)
        self.create_text(w / 2, h / 2 + 1, text=self._text,
                         fill=fg, font=self._font)


class FolderDropHook:
    """让 Tk 窗口接受「从资源管理器拖入文件夹」。

    Tk 本身没有拖放支持，这里用纯 ctypes 实现：给窗口及其所有子控件挂上
    WS_EX_ACCEPTFILES，并子类化它们的窗口过程来接收 WM_DROPFILES。
    不依赖第三方库，打包时无需额外数据文件；非 Windows 或失败时静默降级。

    注意：窗口过程由 Tcl 的消息循环调用，里面**不能调任何 tkinter API**
    （Tcl 会在 C 层释放/恢复线程状态，与 ctypes 回调的 GIL 状态冲突，
    实测会直接 fatal error）。因此回调只把路径放进队列，由 Tk 定时器取走。
    """

    WM_DROPFILES = 0x0233
    GWL_WNDPROC = -4

    def __init__(self, root_widget):
        self._pending = []
        self._originals = {}
        self._proc = None
        self._ok = False
        if os.name != 'nt':
            return
        try:
            self._install(root_widget)
            self._ok = True
        except Exception:
            self._ok = False

    @property
    def enabled(self) -> bool:
        return self._ok

    def take_pending(self) -> list:
        """取走已入队的拖入路径（只能从 Tk 线程调用）。"""
        if not self._pending:
            return []
        items, self._pending = self._pending, []
        return items

    # ------------------------------------------------------------------
    def _install(self, widget):
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32

        wndproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_size_t,
                                       ctypes.c_ssize_t)
        self._proc = wndproc_t(self._wndproc)   # 必须持有引用，否则会被 GC

        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_size_t,
                                           ctypes.c_ssize_t]

        top = int(widget.winfo_id())
        for hwnd in [top] + self._descendants(top):
            shell32.DragAcceptFiles(ctypes.c_void_p(hwnd), True)
            old = user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), self.GWL_WNDPROC,
                                           ctypes.cast(self._proc, ctypes.c_void_p))
            if old:
                self._originals[hwnd] = old

        if not self._originals:
            raise OSError('cannot subclass window procedure')

    @staticmethod
    def _descendants(hwnd):
        """枚举所有子窗口（Tk 的每个控件都是一个子窗口）。"""
        user32 = ctypes.windll.user32
        found = []
        cb_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(h, _param):
            found.append(int(h))
            return 1

        user32.EnumChildWindows(ctypes.c_void_p(hwnd), cb_t(_cb), None)
        return found

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_DROPFILES:
            try:
                paths = self._read_drop(wparam)
                if paths:
                    self._pending.append(paths)   # 只碰 Python 对象
            except Exception:
                pass
            finally:
                try:
                    ctypes.windll.shell32.DragFinish(ctypes.c_void_p(wparam))
                except Exception:
                    pass
            return 0
        old = self._originals.get(int(hwnd) if hwnd else 0)
        if not old:
            return 0
        return ctypes.windll.user32.CallWindowProcW(
            ctypes.c_void_p(old), ctypes.c_void_p(hwnd), msg, wparam, lparam)

    @staticmethod
    def _read_drop(hdrop):
        """把 HDROP 句柄解析成路径列表。"""
        shell32 = ctypes.windll.shell32
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_wchar_p, ctypes.c_uint]
        shell32.DragQueryFileW.restype = ctypes.c_uint
        count = shell32.DragQueryFileW(ctypes.c_void_p(hdrop), 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            size = shell32.DragQueryFileW(ctypes.c_void_p(hdrop), i, None, 0)
            if not size:
                continue
            buf = ctypes.create_unicode_buffer(size + 1)
            shell32.DragQueryFileW(ctypes.c_void_p(hdrop), i, buf, size + 1)
            if buf.value:
                paths.append(buf.value)
        return paths



# ============================================================================
#  数据模型
# ============================================================================

@dataclass
class FileEntry:
    """代表一个待重命名的视频及其关联字幕"""
    original_path: str          # 原始完整路径
    original_name: str          # 原始文件名（含扩展名）
    original_stem: str          # 原始主文件名（无扩展名）
    ext: str                    # 扩展名（小写）
    season: str = "01"          # 季号（2位字符串）
    episode: str = ""           # 集号（2-3位字符串）
    parent_dir: str = ""        # 父目录
    checked: bool = True        # 是否参与重命名
    warning: str = ""           # 警告信息（"" 表示无警告）
    status: str = ""            # 执行结果状态（"" / "✓" / "✗"）
    already_renamed: bool = False  # 文件名已有 SxxExx_ 前缀
    subtitles: list = field(default_factory=list)  # 关联的 SubEntry 列表
    _new_name: str = ""         # 缓存新文件名


@dataclass
class SubEntry:
    """代表一个字幕文件"""
    original_path: str
    original_name: str
    ext: str
    new_name: str = ""
    sync_enabled: bool = True   # 默认勾选同步字幕


@dataclass
class FolderEntry:
    """代表一个待重命名的文件夹（根目录或分季子文件夹）"""
    original_path: str          # 原始完整路径
    original_name: str          # 原始文件夹名
    new_name: str = ""          # 新文件夹名
    parent_dir: str = ""        # 父目录
    kind: str = "root"          # 'root' | 'season'
    season: str = ""            # 季号（仅 kind='season'）
    checked: bool = False       # 默认不勾选
    status: str = ""            # 执行结果


@dataclass
class TmdbEpisodeInfo:
    """TMDB 单集信息"""
    episode_number: int
    name: str                   # 官方集名
    validated: bool = False     # 是否校验通过


# ============================================================================
#  核心逻辑 — 季号提取
# ============================================================================

def extract_season_from_folder(folder_name: str) -> Optional[int]:
    """从子文件夹名中提取季号，失败返回 None。"""
    for pattern in SEASON_PATTERNS:
        m = pattern.fullmatch(folder_name.strip())
        if m:
            num = int(m.group(1))
            if 0 <= num <= 99:
                return num
    return None


# ============================================================================
#  核心逻辑 — 集号提取
# ============================================================================

def extract_episode_number(filename_stem: str) -> Optional[int]:
    """从文件名中提取集号。

    规则：
    1. 优先在文件名后半段（后 50%）搜索
    2. 取匹配到的最后一个符合模式的数字
    3. > 500 视为可疑但依然提取（由 GUI 标记⚠️）
    """
    candidates = []
    half = len(filename_stem) // 2
    # 在后半段和全局各搜索一次，后半段结果优先
    search_ranges = [(half, len(filename_stem)), (0, len(filename_stem))]

    for start, end in search_ranges:
        segment = filename_stem[start:end]
        for pattern in EPISODE_PATTERNS:
            for m in pattern.finditer(segment):
                num = int(m.group(1))
                # 限制在合理范围：1 ~ 1999
                if 1 <= num <= 1999:
                    # 记录在整个文件名中的位置
                    abs_pos = start + m.start()
                    candidates.append((abs_pos, num))

    if not candidates:
        return None

    # 按位置排序，取最后一个出现的作为集号
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


# ============================================================================
#  核心逻辑 — 字幕匹配
# ============================================================================

def _ascii_word(token: str) -> str:
    """把短标记包装成「两侧不是英文字母」的断言。

    Python 的 \\b 把汉字也算作单词字符，所以 "[TV全集]" 里 \\bTV\\b 匹配不到；
    用 ASCII 边界代替，同时避免把 "[SPY×FAMILY]" 里的 SP 当成特典标记。
    """
    return rf'(?<![A-Za-z]){token}(?![A-Za-z])'


# 出现在括号里即视为技术信息的关键词（发布组、编码、分辨率、字幕/语言标记…）
_TECH_PATTERNS = [
    r'\d{2,4}[pPkK]',                       # 1080p 720P
    r'\d{3,4}\s*[xX×]\s*\d{3,4}',           # 1920x1080
    r'\d{0,2}bit',                          # 10bit / 8bit
    _ascii_word(r'BD(?:Rip|MV)?'), r'Blu-?ray', r'DVD(?:Rip)?',
    _ascii_word(r'WEB(?:-?DL|-?Rip)?'), _ascii_word(r'HDTV'),
    r'[xXhH]\.?26[45]', r'HEVC', r'AVC', r'Hi10P', r'Ma10p',
    r'FLAC', r'EAC3', r'TrueHD', r'AAC', r'AC3', r'DTS', r'MP3',
    r'MKV', r'MP4', r'AVI',
    r'简繁', r'简体', r'繁体', r'简日', r'繁日', r'中日', r'双语',
    r'外挂', r'内嵌', r'字幕', r'全集', r'特典', r'映像',
    _ascii_word(r'CHS'), _ascii_word(r'CHT'), _ascii_word(r'GB'),
    _ascii_word(r'BIG5'), _ascii_word(r'JPN'), _ascii_word(r'JPSC'),
    _ascii_word(r'JPTC'), _ascii_word(r'JP'), _ascii_word(r'EN'),
    _ascii_word(r'TV'), _ascii_word(r'OVA'), _ascii_word(r'SP'),
    r'NCOP', r'NCED', r'Ver\.?\s*\d',
    r'Baha', r'B-?Global', r'Bilibili', r'ABEMA', r'Crunchyroll', r'Netflix',
    r'\d{1,4}\s*[-~～]\s*\d{1,4}',          # 集数范围 01-12
]
_TECH_RE = re.compile('|'.join(_TECH_PATTERNS), re.I)

# 方括号组：半角 []、全角 【】 与 ［］
_GROUP_RE = re.compile(r'\[[^\[\]]*\]|【[^【】]*】|［[^［］]*］')
_LEADING_GROUPS_RE = re.compile(r'^(?:(?:\[[^\[\]]*\]|【[^【】]*】|［[^［］]*］)\s*)+')
# 纯数字 / 版本号 / 特典号 的括号组
_BARE_NOISE_RE = re.compile(r'\d{1,4}|[Vv]\d{1,2}|[Ss][Pp]\d*')


def _is_noise_group(content: str) -> bool:
    """判断一个括号里的内容是不是技术信息（该整组丢弃）。"""
    text = content.strip()
    if not text:
        return True
    if _BARE_NOISE_RE.fullmatch(text):
        return True
    return bool(_TECH_RE.search(text))


def extract_clean_title(folder_name: str) -> str:
    """从文件夹名中提取干净的剧名，供 TMDB 搜索使用。

    处理顺序：先剔技术括号组 → 再判开头发布组 → 去技术圆括号 → 去壳 → 收尾。
    永不返回空串（兜底返回原始文件夹名），避免调用方拿到空查询词。

    例：
      [SweetSub&VCB-Studio] DARLING in the FRANXX [Ma10p_1080p]
      → DARLING in the FRANXX
      [Nekomoe kissaten][Bocchi the Rock!][01-12][1080p][CHS_JPN]
      → Bocchi the Rock!
      [DBD-Raws][邪神与厨二病少女][01-12TV全集][1080P][BDRip][FLAC]
      → 邪神与厨二病少女
    """
    raw = folder_name.strip()
    name = raw

    # 1. 丢弃技术性的括号组（分辨率/编码/字幕/集数范围…）
    name = _GROUP_RE.sub(
        lambda m: '' if _is_noise_group(m.group(0)[1:-1]) else m.group(0), name
    ).strip()

    # 2. 处理开头的发布组括号
    m = _LEADING_GROUPS_RE.match(name)
    if m:
        groups = _GROUP_RE.findall(m.group(0))
        tail = name[m.end():].strip()
        if tail:
            # 括号外还有内容 → 开头的括号都是组名，整段丢掉
            name = tail
        elif groups:
            # 整串都在括号里 → 最后一组才是标题
            name = groups[-1][1:-1].strip()

    # 3. 丢弃技术性的圆括号
    name = re.sub(
        r'[（(][^）)]*[）)]',
        lambda mm: '' if _TECH_RE.search(mm.group(0)) else mm.group(0),
        name,
    ).strip()

    # 4. 剩下的方括号去掉壳（此时里面就是标题/副标题）
    name = _GROUP_RE.sub(lambda mm: mm.group(0)[1:-1].strip(), name)

    # 5. 装饰性符号：★…★ / ※ / 各类圆点
    name = re.sub(r'[★☆].*?[★☆]', ' ', name)
    name = re.sub(r'[※◆■●▲◇]', ' ', name)

    # 6. 结尾的集号标记：「 - 01」；「 - S2」这类季标记要保留
    name = re.sub(r'\s*[-–—_]\s*\d{1,4}\s*$', '', name)

    # 7. 中日双语并列的标题取前半（Fate / stay night 这类纯英文名不受影响）
    if re.search(r'\s*[/／]\s*', name) and re.search(r'[\u4e00-\u9fff]', name):
        name = re.split(r'\s*[/／]\s*', name, maxsplit=1)[0]

    name = re.sub(r'\s+', ' ', name).strip(' -_·.')

    # 8. 兜底：宁可让 TMDB 搜原始名，也不要给个空查询词
    if not name:
        return re.sub(r'\s+', ' ', raw)
    return name


def find_subtitle_files(video_path: str, video_stem: str) -> list[SubEntry]:
    """在视频同目录下查找主文件名相同的字幕文件。

    支持双扩展名格式：video.sc.ass / video.tc.ass / video.zh-Hans.srt 等。
    """
    video_dir = os.path.dirname(video_path)
    subs = []
    video_lower = video_stem.lower()
    try:
        for fname in os.listdir(video_dir):
            stem, ext = os.path.splitext(fname)
            ext_lower = ext.lower()
            if ext_lower not in SUB_EXTS:
                continue
            stem_lower = stem.lower()
            # 精确匹配
            if stem_lower == video_lower:
                subs.append(SubEntry(
                    original_path=os.path.join(video_dir, fname),
                    original_name=fname,
                    ext=ext_lower,
                ))
                continue
            # 双扩展名：video.sc.ass → splitext 两次 → video + .sc
            stem2, _ = os.path.splitext(stem)
            if stem2 and stem2.lower() == video_lower:
                subs.append(SubEntry(
                    original_path=os.path.join(video_dir, fname),
                    original_name=fname,
                    ext=ext_lower,
                ))
    except OSError:
        pass
    return subs


# ============================================================================
#  核心逻辑 — 新文件名生成
# ============================================================================

def generate_new_name(original_stem: str, ext: str, season: str, episode: str) -> str:
    """生成新文件名：S{季号}E{集号}_{原文件名}.ext"""
    ep_str = str(episode).zfill(2) if len(str(episode)) <= 2 else str(episode).zfill(3)
    season_str = str(season).zfill(2)
    return f"S{season_str}E{ep_str}_{original_stem}{ext}"


def compute_new_names(entry: FileEntry):
    """计算 FileEntry 的新文件名及其字幕的新文件名。"""
    if entry.already_renamed:
        # 已改名文件：新名 = 原名（无需再次重命名）
        entry._new_name = entry.original_name
        for sub in entry.subtitles:
            sub.new_name = sub.original_name
    else:
        entry._new_name = generate_new_name(
            entry.original_stem, entry.ext, entry.season, entry.episode
        )
        for sub in entry.subtitles:
            sub_stem = os.path.splitext(sub.original_name)[0]
            sub.new_name = generate_new_name(
                sub_stem, sub.ext, entry.season, entry.episode
            )


# ============================================================================
#  核心逻辑 — 安全检查
# ============================================================================

def sanitize_filename(name: str) -> str:
    """剔除 Windows 文件名中的非法字符。"""
    return ILLEGAL_CHARS.sub('', name)


def check_path_length(path: str) -> bool:
    """检查完整路径是否过长（>240 字符）。"""
    return len(path) > PATH_LENGTH_WARN


# ============================================================================
#  核心逻辑 — 冲突检测
# ============================================================================

def detect_conflicts(entries: list[FileEntry]) -> list[tuple[int, int, str]]:
    """检测重命名后是否存在路径冲突。

    返回 [(idx_a, idx_b, conflicting_path), ...]
    """
    new_to_indices: dict[str, list[int]] = {}
    for i, entry in enumerate(entries):
        if not entry.checked:
            continue
        new_dir = entry.parent_dir
        new_full = os.path.join(new_dir, entry._new_name)
        new_full = sanitize_filename(new_full)
        new_full_norm = os.path.normcase(new_full)
        new_to_indices.setdefault(new_full_norm, []).append(i)

        for sub in entry.subtitles:
            if not sub.sync_enabled:
                continue
            sub_full = os.path.join(new_dir, sub.new_name)
            sub_full = sanitize_filename(sub_full)
            sub_full_norm = os.path.normcase(sub_full)
            new_to_indices.setdefault(sub_full_norm, []).append(i)

    conflicts = []
    for path, indices in new_to_indices.items():
        if len(indices) > 1:
            conflicts.append((indices[0], indices[1], path))
    return conflicts


# ============================================================================
#  扫描线程
# ============================================================================

def scan_folder(root_path: str, default_season: str, result_queue: queue.Queue):
    """在后台线程中扫描文件夹，将结果逐批放入队列。"""
    entries = []
    folder_entries = []
    root_name = os.path.basename(os.path.normpath(root_path))

    try:
        subdirs = sorted([
            d for d in os.listdir(root_path)
            if os.path.isdir(os.path.join(root_path, d))
        ])
    except OSError:
        result_queue.put(('error', f'无法访问文件夹: {root_path}'))
        result_queue.put(('done', [], []))
        return

    # 判断顶层是否有分季子文件夹
    season_dirs = {}
    has_season_subdirs = False
    for d in subdirs:
        season_num = extract_season_from_folder(d)
        if season_num is not None:
            has_season_subdirs = True
            season_dirs[d] = f"{season_num:02d}"

    # 收集根文件夹
    folder_entries.append(FolderEntry(
        original_path=root_path,
        original_name=root_name,
        parent_dir=str(Path(root_path).parent),
        kind='root',
    ))

    # 如果没有分季子文件夹，扫描根目录本身
    if not has_season_subdirs:
        _scan_single_dir(root_path, default_season, entries, root_path)
    else:
        # 有分季子文件夹：逐个子文件夹扫描
        for subdir, season_str in season_dirs.items():
            sub_path = os.path.join(root_path, subdir)
            _scan_single_dir(sub_path, season_str, entries, root_path)
            # 收集分季文件夹
            folder_entries.append(FolderEntry(
                original_path=sub_path,
                original_name=subdir,
                parent_dir=root_path,
                kind='season',
                season=season_str,
            ))
        # 也扫描根目录下直接存在的视频（归类为默认季号）
        _scan_single_dir(root_path, default_season, entries, root_path)

    # 逐批发送到 GUI
    # 先发送文件夹
    if folder_entries:
        result_queue.put(('folders', folder_entries))
    batch = []
    for i, entry in enumerate(entries):
        batch.append(entry)
        if len(batch) >= 20 or i == len(entries) - 1:
            result_queue.put(('batch', batch))
            batch = []

    result_queue.put(('done', folder_entries, entries))


def _scan_single_dir(dir_path: str, season: str, entries: list, root_path: str):
    """扫描单个目录中的视频文件。"""
    try:
        files = sorted(os.listdir(dir_path))
    except OSError:
        return

    for fname in files:
        full_path = os.path.join(dir_path, fname)
        if os.path.isdir(full_path):
            continue
        stem, ext = os.path.splitext(fname)
        ext_lower = ext.lower()
        if ext_lower not in VIDEO_EXTS:
            continue

        # 检测是否已重命名过（文件名以 SxxExx_ 开头）
        renamed_match = ALREADY_RENAMED_RE.match(stem)
        if renamed_match:
            # 已经是目标格式：提取季号、集号、原始名称
            already_season = renamed_match.group(1)
            already_episode = renamed_match.group(2)
            original_stem_part = renamed_match.group(3)
            episode_str = already_episode
            warning = ""
            ep_num = int(already_episode)
            if ep_num > SUSPICIOUS_EPISODE:
                warning = f"集号 {ep_num} > 500，可能为年份或其他数字"
            entry = FileEntry(
                original_path=full_path,
                original_name=fname,
                original_stem=stem,
                ext=ext_lower,
                season=already_season,
                episode=episode_str,
                parent_dir=dir_path,
                warning=warning,
                already_renamed=True,
                checked=False,
            )
        else:
            # 正常提取集号
            ep_num = extract_episode_number(stem)
            episode_str = str(ep_num) if ep_num is not None else ""
            warning = ""
            if ep_num is not None and ep_num > SUSPICIOUS_EPISODE:
                warning = f"集号 {ep_num} > 500，可能为年份或其他数字"
            entry = FileEntry(
                original_path=full_path,
                original_name=fname,
                original_stem=stem,
                ext=ext_lower,
                season=season,
                episode=episode_str,
                parent_dir=dir_path,
                warning=warning,
            )
        # 查找同名字幕
        entry.subtitles = find_subtitle_files(full_path, stem)
        compute_new_names(entry)
        entries.append(entry)


# ============================================================================
#  重命名执行线程
# ============================================================================

def execute_rename(tasks: list, folders: list, result_queue: queue.Queue):
    """在后台线程中批量重命名。

    tasks: [{'orig_idx': int, 'old_path': str, 'new_path': str, 'old_name': str}, ...]
    使用 deep copy 数据，完全不依赖原始 entry 对象。
    """
    success = 0
    fail = 0
    total = len(tasks)

    for i, task in enumerate(tasks):
        # 重命名字幕
        for sub_old, sub_new in task.get('subs', []):
            try:
                if sub_old != sub_new and os.path.exists(sub_old):
                    os.rename(sub_old, sub_new)
            except OSError:
                pass

        # 重命名视频
        try:
            if task['old_path'] != task['new_path'] and os.path.exists(task['old_path']):
                os.rename(task['old_path'], task['new_path'])
                success += 1
            else:
                success += 1
        except OSError as e:
            fail += 1

        result_queue.put(('progress', f'[{i+1}/{total}] done {task["old_name"]}'))

    # 文件夹重命名
    if folders:
        for f in sorted(folders, key=lambda x: x.original_path.count(os.sep), reverse=True):
            try:
                new_path = os.path.join(f.parent_dir, sanitize_filename(f.new_name))
                if new_path != f.original_path and os.path.exists(f.original_path):
                    os.rename(f.original_path, new_path)
            except OSError:
                pass

    result_queue.put(('result', success, fail))


# ============================================================================
#  GUI 应用
# ============================================================================

class AniPrepApp:
    """Emby 重命名工具 GUI 主窗口"""

    def __init__(self, root: tk.Tk):
        self.root = root

        # 界面缩放比必须先算出来，后面所有 px() 都依赖它
        global DPI_SCALE
        DPI_SCALE = detect_ui_scale(root)

        self.root.title("AniPrep — 动漫媒体库规范化重命名")
        self.root.geometry(f"{px(1240)}x{px(780)}")
        self.root.minsize(px(960), px(600))

        # 配置
        self.cfg = Config()
        self._restore_geometry()

        # 字体（依赖 root，必须先解析）
        Fonts.init(self.root)

        # TMDB 客户端（懒初始化）
        self.tmdb: Optional[TMDBClient] = None
        self.tmdb_tv_id: Optional[int] = None
        self.tmdb_tv_name: str = ""
        self.tmdb_tv_year: str = ""
        self.tmdb_episodes: dict = {}  # season_str -> {ep_num: TmdbEpisodeInfo}

        # 数据
        self.entries: list[FileEntry] = []
        self.folders: list[FolderEntry] = []
        self.folder_path = tk.StringVar(value=self.cfg.last_root_folder)
        self.default_season = tk.StringVar(value="01")
        self.status_text = tk.StringVar(value="就绪")
        self.stats_text = tk.StringVar(value="")
        self.result_queue = queue.Queue()
        self._scan_overlay = None
        self._renaming = False
        self._show_tmdb_column = tk.BooleanVar(value=False)

        # 配置根窗口背景
        self.root.configure(bg=Theme.bg)

        # 构建界面
        self._build_menu()
        self._build_header()
        self._build_control_bar()
        self._build_table()
        self._build_footer()
        self._apply_theme()
        self._setup_tags()
        self._bind_hover()

        # 定时轮询队列
        self._poll_queue()

        # 支持把文件夹直接拖进窗口（Windows，纯 ctypes 实现）
        self._drop_hook = FolderDropHook(self.root)

        # 窗口关闭时保存配置
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _handle_drop(self, paths):
        """处理拖入的路径：文件夹直接用；拖的是文件则取其所在目录。

        若文件所在目录本身就是分季目录（S1 / Season 01 / 第1季），
        则再上一层到剧集根目录，否则季号会丢。
        """
        if self._renaming:
            return
        if str(self.scan_btn.cget('state')) == 'disabled':
            return                     # 正在扫描，忽略

        target = None
        for p in paths:
            if os.path.isdir(p):
                target = p
                break
            if os.path.isfile(p):
                parent = os.path.dirname(p)
                grand = os.path.dirname(parent)
                if (grand and os.path.isdir(grand)
                        and extract_season_from_folder(os.path.basename(parent))
                        is not None):
                    parent = grand
                target = parent
                break
        if not target or not os.path.isdir(target):
            return

        self.folder_path.set(target)
        self._start_scan()

    def _restore_geometry(self):
        """恢复上次的窗口尺寸。

        旧版本是 DPI 不感知的，配置里存的是「被系统拉伸过的逻辑像素」，
        需要换算成物理像素，才能保持同样大的窗口。
        注意：ui_scale 只在保存尺寸时一起写入，这里不能回写，
        否则「尺寸还是旧值、缩放比已更新」的错配会把窗口夹到最小尺寸。
        """
        geom = (self.cfg.window_geometry or '').strip()
        if not geom:
            return
        if not self.cfg.from_file('ui_scale'):
            factor = DPI_SCALE                     # 旧配置：逻辑像素 → 物理像素
        else:
            try:
                saved = float(self.cfg.ui_scale or 1.0)
            except (TypeError, ValueError):
                saved = 1.0
            factor = DPI_SCALE / saved if saved else 1.0
        try:
            self.root.geometry(_scale_geometry(geom, factor))
        except tk.TclError:
            pass

    def _on_close(self):
        """窗口关闭时保存状态"""
        try:
            self.cfg.set('window_geometry', self.root.geometry())
            self.cfg.set('ui_scale', DPI_SCALE)
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------
    #  菜单栏
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=Theme.surface,
                          fg=Theme.text,
                          activebackground=Theme.accent,
                          activeforeground=Theme.accent_text,
                          bd=0, relief='flat')
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0,
                            bg=Theme.surface,
                            fg=Theme.text,
                            activebackground=Theme.accent,
                            activeforeground=Theme.accent_text)
        file_menu.add_command(label="选择文件夹…", command=self._browse_folder)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="文件", menu=file_menu)

        # 设置菜单
        settings_menu = tk.Menu(menubar, tearoff=0,
                                bg=Theme.surface,
                                fg=Theme.text,
                                activebackground=Theme.accent,
                                activeforeground=Theme.accent_text)
        settings_menu.add_command(label="TMDB API Key 设置…", command=self._set_api_key)
        menubar.add_cascade(label="设置", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=0,
                            bg=Theme.surface,
                            fg=Theme.text,
                            activebackground=Theme.accent,
                            activeforeground=Theme.accent_text)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

    # ------------------------------------------------------------------
    #  标题横幅
    # ------------------------------------------------------------------

    def _build_header(self):
        """顶部品牌栏：图标 + 名称 + 副标题，右侧显示 TMDB 与版本"""
        self.header_frame = tk.Frame(self.root, bg=Theme.header_bg)
        self.header_frame.pack(fill=tk.X)

        inner = tk.Frame(self.header_frame, bg=Theme.header_bg,
                         padx=px(18), pady=px(11))
        inner.pack(fill=tk.X)

        # 品牌图标（圆角方块 + 字母）
        side = px(34)
        mark = tk.Canvas(inner, width=side, height=side, bg=Theme.header_bg,
                         highlightthickness=0, bd=0)
        mark.pack(side=tk.LEFT)
        draw_round_rect(mark, 1, 1, side - 1, side - 1, px(10),
                        fill=Theme.accent, outline='')
        mark.create_text(side / 2, side / 2, text='A', fill=Theme.accent_text,
                         font=(Fonts.family, 15, 'bold'))

        titles = tk.Frame(inner, bg=Theme.header_bg)
        titles.pack(side=tk.LEFT, padx=(px(11), 0))
        tk.Label(titles, text="AniPrep", font=(Fonts.family, 15, 'bold'),
                 fg=Theme.header_text, bg=Theme.header_bg).pack(anchor='w')
        tk.Label(titles, text="动漫媒体库 · 规范化重命名 · Emby / Jellyfin",
                 font=Fonts.brand_sub, fg=Theme.text_muted,
                 bg=Theme.header_bg).pack(anchor='w')

        # 右侧：TMDB 状态
        right = tk.Frame(inner, bg=Theme.header_bg)
        right.pack(side=tk.RIGHT)
        self.tmdb_status = tk.Label(right, text='', font=Fonts.small,
                                    fg=Theme.success, bg=Theme.header_bg)
        self.tmdb_status.pack(side=tk.RIGHT, padx=(px(8), 0))
        tk.Label(right, text='v1.6', font=Fonts.small, fg=Theme.text_muted,
                 bg=Theme.header_bg).pack(side=tk.RIGHT)

        # 细分隔线
        self.header_sep = tk.Frame(self.root, height=1, bg=Theme.border)
        self.header_sep.pack(fill=tk.X)

    # ------------------------------------------------------------------
    #  控制栏
    # ------------------------------------------------------------------

    def _build_control_bar(self):
        """工具卡片：路径行 + 选项行"""
        self.control_frame = tk.Frame(self.root, bg=Theme.bg, padx=px(16), pady=px(12))
        self.control_frame.pack(fill=tk.X)

        card = tk.Frame(self.control_frame, bg=Theme.surface,
                        highlightbackground=Theme.border, highlightthickness=1)
        card.pack(fill=tk.X)
        pad = tk.Frame(card, bg=Theme.surface, padx=px(14), pady=px(12))
        pad.pack(fill=tk.X)

        # ---- 第一行：路径 + 浏览 + 扫描 ----
        row1 = tk.Frame(pad, bg=Theme.surface)
        row1.pack(fill=tk.X)

        tk.Label(row1, text='媒体库文件夹', font=Fonts.small,
                 fg=Theme.text_secondary, bg=Theme.surface).pack(side=tk.LEFT, padx=(0, px(10)))

        self.folder_entry = ttk.Entry(row1, textvariable=self.folder_path,
                                      font=Fonts.body)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(row1, text='浏览', command=self._browse_folder,
                   style='Ghost.TButton').pack(side=tk.LEFT, padx=(px(8), 0))
        self.scan_btn = ttk.Button(row1, text='扫描', command=self._start_scan,
                                   style='Primary.TButton')
        self.scan_btn.pack(side=tk.LEFT, padx=(px(6), 0))

        # ---- 第二行：季号 + TMDB | 文件夹开关 + 批量选择 ----
        row2 = tk.Frame(pad, bg=Theme.surface)
        row2.pack(fill=tk.X, pady=(px(10), 0))

        left2 = tk.Frame(row2, bg=Theme.surface)
        left2.pack(side=tk.LEFT)

        tk.Label(left2, text='默认季号', font=Fonts.small,
                 fg=Theme.text_secondary, bg=Theme.surface).pack(side=tk.LEFT, padx=(0, px(6)))
        self.season_combo = ttk.Combobox(
            left2, textvariable=self.default_season, width=4,
            values=[f"{i:02d}" for i in range(0, 100)],
            state='readonly', font=Fonts.body,
        )
        self.season_combo.pack(side=tk.LEFT)

        tk.Frame(left2, width=1, bg=Theme.border).pack(side=tk.LEFT, fill=tk.Y,
                                                       padx=(px(18), px(16)), pady=px(2))

        self.tmdb_btn_search = ttk.Button(left2, text='TMDB 匹配',
                                          command=self._search_tmdb,
                                          style='Ghost.TButton')
        self.tmdb_btn_search.pack(side=tk.LEFT)

        right2 = tk.Frame(row2, bg=Theme.surface)
        right2.pack(side=tk.RIGHT)

        self._rename_folders_var = tk.BooleanVar(value=self.cfg.rename_folders)
        self.chk_rename_folders = tk.Checkbutton(
            right2, text='同步重命名文件夹',
            variable=self._rename_folders_var,
            command=self._on_toggle_rename_folders,
            bg=Theme.surface, fg=Theme.text_secondary,
            activebackground=Theme.surface, activeforeground=Theme.text,
            selectcolor=Theme.surface, font=Fonts.body,
            bd=0, highlightthickness=0, cursor='hand2',
        )
        self.chk_rename_folders.pack(side=tk.LEFT, padx=(0, px(12)))

        tk.Frame(right2, width=1, bg=Theme.border).pack(side=tk.LEFT, fill=tk.Y,
                                                        padx=(0, px(12)), pady=px(1))

        ttk.Button(right2, text='全选', command=self._select_all,
                   style='Ghost.TButton').pack(side=tk.LEFT, padx=(0, px(4)))
        ttk.Button(right2, text='清空选择', command=self._deselect_all,
                   style='Ghost.TButton').pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(right2, text='仅警告项', command=self._select_warnings,
                   style='Ghost.TButton').pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    #  预览表格（增强版）
    # ------------------------------------------------------------------

    def _build_table(self):
        self.table_frame = tk.Frame(self.root, bg=Theme.bg,
                                    padx=px(16), pady=0)
        self.table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, px(12)))

        # 表格容器（带边框效果）
        self.table_inner = tk.Frame(self.table_frame, bg=Theme.surface,
                                    highlightbackground=Theme.border,
                                    highlightthickness=1)
        self.table_inner.pack(fill=tk.BOTH, expand=True)

        columns = ('checked', 'season', 'episode', 'original', 'new_name', 'tmdb_name')
        self.tree = ttk.Treeview(
            self.table_inner,
            columns=columns,
            show='headings',
            selectmode='extended',
            style='Card.Treeview',
            height=6,
        )

        self.tree.heading('checked', text='选', anchor='center')
        self.tree.heading('season', text='季', anchor='center')
        self.tree.heading('episode', text='集', anchor='center')
        self.tree.heading('original', text='原文件名', anchor='w')
        self.tree.heading('new_name', text='新文件名', anchor='w')
        self.tree.heading('tmdb_name', text='TMDB 集名', anchor='w')

        self.tree.column('checked', width=px(42), minwidth=px(42), anchor='center', stretch=False)
        self.tree.column('season', width=px(52), minwidth=px(52), anchor='center', stretch=False)
        self.tree.column('episode', width=px(52), minwidth=px(52), anchor='center', stretch=False)
        self.tree.column('original', width=px(262), minwidth=px(140), stretch=True)
        self.tree.column('new_name', width=px(300), minwidth=px(140), stretch=True)
        self.tree.column('tmdb_name', width=px(180), minwidth=px(80), stretch=True)

        # 滚动条
        vsb = ttk.Scrollbar(self.table_inner, orient=tk.VERTICAL,
                            command=self.tree.yview, style='Card.Vertical.TScrollbar')
        hsb = ttk.Scrollbar(self.table_inner, orient=tk.HORIZONTAL,
                            command=self.tree.xview, style='Card.Horizontal.TScrollbar')
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, px(5)), pady=px(6))
        hsb.pack(side=tk.BOTTOM, fill=tk.X, padx=px(6), pady=(px(2), px(5)))
        self.tree.pack(fill=tk.BOTH, expand=True, padx=(0, 0), pady=0)
        self._vsb, self._hsb = vsb, hsb
        self._sync_busy = False
        self.tree.bind('<Configure>',
                       lambda e: self.root.after_idle(self._sync_scrollbars))

        # 空状态提示（无数据时覆盖在表格中央）
        self._empty_state = tk.Frame(self.table_inner, bg=Theme.surface)
        icon = px(56)
        emark = tk.Canvas(self._empty_state, width=icon, height=icon,
                          bg=Theme.surface, highlightthickness=0, bd=0)
        emark.pack()
        # 图标：圆角底 + 三条「列表」横条（不依赖字体符号）
        draw_round_rect(emark, px(2), px(2), icon - px(2), icon - px(2), px(17),
                        fill=Theme.accent_soft, outline='')
        for i, bar_w in enumerate((22, 16, 22)):
            y = px(22) + i * px(7)
            draw_round_rect(emark, px(17), y, px(17) + px(bar_w), y + px(3), px(1.5),
                            fill=Theme.accent, outline='')
        tk.Label(self._empty_state, text='还没有扫描任何文件',
                 font=(Fonts.family, 11, 'bold'), fg=Theme.text_secondary,
                 bg=Theme.surface).pack(pady=(px(12), px(3)))
        tk.Label(self._empty_state,
                 text='点击「选择文件夹」，或直接把文件夹拖到这里',
                 font=Fonts.small, fg=Theme.text_muted,
                 bg=Theme.surface).pack()
        ttk.Button(self._empty_state, text='选择文件夹',
                   command=self._browse_folder,
                   style='Primary.TButton').pack(pady=(px(14), 0))

        # 双击编辑季号/集号
        self.tree.bind('<Double-1>', self._on_double_click)
        # 勾选框切换
        self.tree.bind('<Button-1>', self._on_click)
        # 滚动时清理 hover
        self.tree.bind('<MouseWheel>', lambda e: self._clear_hover())

        # 编辑控件（复用）
        self.edit_widget = None
        self.edit_item = None
        self.edit_column = None

        # Hover 状态
        self._hovered_iid = None
        self._hover_saved = None

        self._update_empty_state()

    def _update_empty_state(self):
        """表格为空时显示空状态提示。"""
        if not hasattr(self, '_empty_state'):
            return
        if self.entries:
            self._empty_state.place_forget()
        elif not self._scan_overlay:
            self._empty_state.place(relx=0.5, rely=0.45, anchor='center')

    def _sync_scrollbars(self):
        """内容不足一屏时自动隐藏滚动条，避免多余线条。"""
        if not hasattr(self, '_vsb') or self._sync_busy:
            return
        self._sync_busy = True
        try:
            for _ in range(2):
                y0, y1 = self.tree.yview()
                need_v = (y0, y1) != (0.0, 1.0)
                if need_v and not self._vsb.winfo_ismapped():
                    self._vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 5),
                                   pady=6, before=self.tree)
                elif not need_v and self._vsb.winfo_ismapped():
                    self._vsb.pack_forget()

                x0, x1 = self.tree.xview()
                need_h = (x0, x1) != (0.0, 1.0)
                if need_h and not self._hsb.winfo_ismapped():
                    self._hsb.pack(side=tk.BOTTOM, fill=tk.X, padx=6,
                                   pady=(2, 5), before=self.tree)
                elif not need_h and self._hsb.winfo_ismapped():
                    self._hsb.pack_forget()
        finally:
            self._sync_busy = False

    def _bind_hover(self):
        """绑定行悬停高亮"""
        self.tree.bind('<Motion>', self._on_motion)

    def _on_motion(self, event):
        """鼠标移动时高亮当前行"""
        iid = self.tree.identify_row(event.y)
        if iid == self._hovered_iid:
            return
        # 清除旧 hover
        if self._hovered_iid and self.tree.exists(self._hovered_iid):
            self._restore_row_tag(self._hovered_iid)
        self._hovered_iid = iid
        if iid and iid.startswith('v'):
            tags = tuple(self.tree.item(iid, 'tags'))
            # 语义行（警告/冲突/子行/文件夹）不参与 hover，避免底色叠加歧义
            if any(t in ('conflict', 'warn', 'sub', 'sub_alt', 'sub_warn', 'folder')
                   for t in tags):
                return
            self._hover_saved = tags
            self.tree.item(iid, tags=('hover',))

    def _restore_row_tag(self, iid):
        """恢复一行的原始标签（移除 hover）"""
        if not self.tree.exists(iid):
            return
        self.tree.item(iid, tags=self._hover_saved or ())
        self._hover_saved = None

    def _clear_hover(self):
        """清除 hover 状态"""
        if self._hovered_iid and self.tree.exists(self._hovered_iid):
            self._restore_row_tag(self._hovered_iid)
        self._hovered_iid = None

    def _setup_tags(self):
        """配置表格行样式标签（每行只挂一个底色标签，避免优先级歧义）。"""
        c = Theme
        self.tree.tag_configure('row_normal', background=c.surface, foreground=c.text)
        self.tree.tag_configure('row_alt', background=c.table_alt, foreground=c.text)
        self.tree.tag_configure('warn', background=c.warning_soft, foreground=c.warning)
        self.tree.tag_configure('conflict', background=c.danger_soft, foreground=c.danger)
        self.tree.tag_configure('sub', background=c.surface_alt, foreground=c.text_muted)
        self.tree.tag_configure('sub_alt', background=c.surface_sunken, foreground=c.text_muted)
        self.tree.tag_configure('sub_warn', background='#FDF6E8', foreground='#A97A2E')
        self.tree.tag_configure('renamed', background=c.surface, foreground=c.text_muted)
        self.tree.tag_configure('hover', background=c.table_hover, foreground=c.text)
        self.tree.tag_configure('folder', background=c.surface_sunken,
                                foreground=c.text_secondary,
                                font=(Fonts.family, 10, 'bold'))

    # ------------------------------------------------------------------
    #  底部操作栏 & 状态栏
    # ------------------------------------------------------------------

    def _build_footer(self):
        """底部固定条：统计胶囊（左） + 状态文字与主操作（右）"""
        sep = tk.Frame(self.root, height=1, bg=Theme.border)
        sep.pack(fill=tk.X)

        self.footer_frame = tk.Frame(self.root, bg=Theme.surface)
        self.footer_frame.pack(fill=tk.X)

        inner = tk.Frame(self.footer_frame, bg=Theme.surface, padx=px(16), pady=px(10))
        inner.pack(fill=tk.X)

        # 左侧：统计胶囊
        left = tk.Frame(inner, bg=Theme.surface)
        left.pack(side=tk.LEFT)

        self.chip_total = self._make_chip(left, '视频 0')
        self.chip_total.pack(side=tk.LEFT, padx=(0, px(6)))
        self.chip_subs = self._make_chip(left, '字幕 0')
        self.chip_subs.pack(side=tk.LEFT, padx=(0, px(6)))
        self.chip_warn = self._make_chip(left, '警告 0')
        self.chip_warn.pack(side=tk.LEFT, padx=(0, px(6)))
        self.chip_conflict = self._make_chip(left, '冲突 0')
        self.chip_conflict.pack(side=tk.LEFT)

        # 右侧：主按钮 + 状态文字
        right = tk.Frame(inner, bg=Theme.surface)
        right.pack(side=tk.RIGHT)

        self.rename_btn = ttk.Button(
            right, text='执行重命名', command=self._execute_rename,
            style='Accent.TButton', state=tk.DISABLED,
        )
        self.rename_btn.pack(side=tk.RIGHT)

        self.status_label = tk.Label(
            right, textvariable=self.status_text,
            font=Fonts.small,
            fg=Theme.text_secondary,
            bg=Theme.surface,
        )
        self.status_label.pack(side=tk.RIGHT, padx=(0, px(14)))

    def _make_chip(self, parent, text):
        """创建一个圆角统计胶囊"""
        return Chip(parent, text)

    # ------------------------------------------------------------------
    #  事件处理 — 勾选切换
    # ------------------------------------------------------------------

    def _on_click(self, event):
        """处理单击：切换勾选状态。"""
        region = self.tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        col = self.tree.identify_column(event.x)
        # column #1 = checked
        if col != '#1':
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        # 文件夹行不可点击切换
        if item.startswith('fd'):
            return
        # 获取与该行关联的 entry
        idx = self._item_to_index(item)
        if idx is None:
            return
        entry = self.entries[idx]
        entry.checked = not entry.checked
        # 同步字幕勾选
        for sub in entry.subtitles:
            sub.sync_enabled = entry.checked
        self._refresh_table()

    def _on_double_click(self, event):
        """处理双击：编辑季号或集号。"""
        region = self.tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        col = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item:
            return
        idx = self._item_to_index(item)
        if idx is None:
            return
        entry = self.entries[idx]

        col_num = int(col[1:]) - 1
        col_name = ('checked', 'season', 'episode', 'original', 'new_name', 'tmdb_name')[col_num]
        if col_name not in ('season', 'episode'):
            return

        # 获取单元格坐标
        bbox = self.tree.bbox(item, col)
        if not bbox:
            return

        # 创建编辑控件
        self._finish_edit()
        self.edit_item = item
        self.edit_column = col_name
        self.edit_widget = ttk.Entry(self.tree)
        current_val = entry.season if col_name == 'season' else entry.episode
        self.edit_widget.insert(0, current_val)
        self.edit_widget.select_range(0, tk.END)
        self.edit_widget.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        self.edit_widget.focus_set()
        self.edit_widget.bind('<Return>', lambda e: self._finish_edit())
        self.edit_widget.bind('<FocusOut>', lambda e: self._finish_edit())
        self.edit_widget.bind('<Escape>', lambda e: self._cancel_edit())

    def _finish_edit(self):
        """完成编辑，保存值并刷新。"""
        if not self.edit_widget or not self.edit_item:
            return
        new_val = self.edit_widget.get().strip()
        col = self.edit_column
        idx = self._item_to_index(self.edit_item)
        if idx is not None:
            entry = self.entries[idx]
            if col == 'season':
                # 验证：1-99
                try:
                    num = int(new_val)
                    if 0 <= num <= 99:
                        entry.season = f"{num:02d}"
                except ValueError:
                    pass
            elif col == 'episode':
                # 验证：1-999
                try:
                    num = int(new_val)
                    if 1 <= num <= 999:
                        entry.episode = str(num)
                        if num > SUSPICIOUS_EPISODE:
                            entry.warning = f"集号 {num} > 500，可能为年份或其他数字"
                        else:
                            entry.warning = ""
                except ValueError:
                    if new_val == "":
                        entry.episode = ""
                        entry.warning = ""
            compute_new_names(entry)
        self._cancel_edit()
        self._refresh_table()

    def _cancel_edit(self):
        """取消编辑。"""
        if self.edit_widget:
            self.edit_widget.destroy()
            self.edit_widget = None
            self.edit_item = None
            self.edit_column = None

    # ------------------------------------------------------------------
    #  索引映射
    # ------------------------------------------------------------------

    def _item_to_index(self, item: str) -> Optional[int]:
        """从 Treeview item iid 反查 entries 索引。"""
        if item.startswith('v'):
            try:
                return int(item[1:])
            except ValueError:
                return None
        if item.startswith('fd'):
            # 文件夹行，返回 None（不在 entries 中）
            return None
        return None

    # ------------------------------------------------------------------
    #  按钮操作
    # ------------------------------------------------------------------

    def _browse_folder(self):
        path = filedialog.askdirectory(title="选择动漫根文件夹")
        if path:
            self.folder_path.set(path)
            self.cfg.set('last_root_folder', path)
            self._start_scan()

    def _start_scan(self):
        path = self.folder_path.get().strip()
        if not path:
            messagebox.showwarning("提示", "请先选择一个文件夹")
            return
        if not os.path.isdir(path):
            messagebox.showerror("错误", f"文件夹不存在: {path}")
            return

        # 保存路径
        self.cfg.set('last_root_folder', path)

        # 清空现有数据
        self.entries.clear()
        self.folders.clear()
        self.tree.delete(*self.tree.get_children())
        self.status_text.set("正在扫描，请稍候...")
        self.scan_btn.config(state=tk.DISABLED, text='扫描中...')

        # 显示扫描中的覆盖提示
        self._show_scan_overlay("正在扫描文件夹…")
        self._update_empty_state()

        threading.Thread(
            target=scan_folder,
            args=(path, self.default_season.get(), self.result_queue),
            daemon=True,
        ).start()

    def _show_scan_overlay(self, msg: str):
        """在表格中央显示带进度动画的提示卡片"""
        if not hasattr(self, 'table_frame'):
            return
        self._hide_scan_overlay()
        self._empty_state.place_forget()

        card = tk.Frame(self.table_frame, bg=Theme.surface,
                        highlightbackground=Theme.border, highlightthickness=1)
        tk.Label(card, text=msg, font=(Fonts.family, 11),
                 fg=Theme.text, bg=Theme.surface).pack(padx=px(36), pady=(px(22), 0))
        tk.Label(card, text='解析文件名中的季集号与字幕，请稍候',
                 font=Fonts.small, fg=Theme.text_muted,
                 bg=Theme.surface).pack(padx=px(36), pady=(px(4), 0))
        pb = ttk.Progressbar(card, mode='indeterminate', length=px(236),
                             style='Line.Horizontal.TProgressbar')
        pb.pack(padx=px(36), pady=(px(14), px(22)))
        card.place(relx=0.5, rely=0.45, anchor='center')
        try:
            pb.start(14)
        except tk.TclError:
            pass
        self._scan_overlay = card
        self._scan_pb = pb

    def _hide_scan_overlay(self):
        if getattr(self, '_scan_pb', None) is not None:
            try:
                self._scan_pb.stop()
            except tk.TclError:
                pass
            self._scan_pb = None
        if hasattr(self, '_scan_overlay') and self._scan_overlay:
            self._scan_overlay.destroy()
            self._scan_overlay = None
        self._update_empty_state()

    def _select_all(self):
        for entry in self.entries:
            entry.checked = True
            for sub in entry.subtitles:
                sub.sync_enabled = True
        self._refresh_table()

    def _deselect_all(self):
        for entry in self.entries:
            entry.checked = False
            for sub in entry.subtitles:
                sub.sync_enabled = False
        self._refresh_table()

    def _select_warnings(self):
        for entry in self.entries:
            entry.checked = (entry.warning != "")
            for sub in entry.subtitles:
                sub.sync_enabled = entry.checked
        self._refresh_table()

    def _execute_rename(self):
        """执行重命名前检查，通过后在后台线程中执行。"""
        # 立即锁定 TMDB 回调
        self._renaming = True

        if not self.entries:
            self._renaming = False
            messagebox.showwarning("提示", "没有可重命名的文件，请先扫描")
            return

        # 筛选勾选且集号非空的条目
        checked = [e for e in self.entries if e.checked and e.episode]
        if not checked:
            self._renaming = False
            messagebox.showwarning("提示", "没有选中任何有效文件（需要集号不为空）")
            return

        # 检查未填集号
        empty_eps = [e for e in self.entries if e.checked and not e.episode]
        if empty_eps:
            names = "\n".join(f"  • {e.original_name}" for e in empty_eps[:10])
            more = f"\n  ... 等共 {len(empty_eps)} 个" if len(empty_eps) > 10 else ""
            if not messagebox.askyesno(
                "确认",
                f"以下 {len(empty_eps)} 个文件的集号为空，将跳过它们：\n{names}{more}\n\n是否继续重命名其他文件？"
            ):
                self._renaming = False
                return

        # 检查 ⚠️ 警告行
        warnings = [e for e in checked if e.warning]
        if warnings:
            names = "\n".join(
                f"  · {e.original_name} — {e.warning}" for e in warnings[:10]
            )
            more = f"\n  ... 等共 {len(warnings)} 个" if len(warnings) > 10 else ""
            if not messagebox.askyesno(
                "警告确认",
                f"以下 {len(warnings)} 个文件存在警告：\n{names}{more}\n\n是否仍要继续？"
            ):
                self._renaming = False
                return

        # 冲突检测
        conflicts = detect_conflicts(checked)
        if conflicts:
            conflict_msgs = []
            for a, b, path in conflicts:
                conflict_msgs.append(f"  • {checked[a]._new_name}\n    ↕ {checked[b]._new_name}")
            names = "\n".join(conflict_msgs[:10])
            messagebox.showerror("命名冲突", f"检测到 {len(conflicts)} 个命名冲突，请修正后重试：\n{names}")
            self._renaming = False
            return

        # 路径长度检查
        long_paths = []
        for e in checked:
            new_path = os.path.join(e.parent_dir, e._new_name)
            if check_path_length(new_path):
                long_paths.append(f"  • {e.original_name} → {len(new_path)} 字符")
        if long_paths:
            names = "\n".join(long_paths[:10])
            more = f"\n  ... 等共 {len(long_paths)} 个" if len(long_paths) > 10 else ""
            if not messagebox.askyesno(
                "路径过长警告",
                f"以下 {len(long_paths)} 个文件的目标路径超过 {PATH_LENGTH_WARN} 字符：\n{names}{more}\n\n是否继续？"
            ):
                self._renaming = False
                return

        # 最终确认
        sel_count = len([e for e in checked if e.episode])
        folder_count = len([f for f in self.folders if f.checked and f.new_name and f.new_name != f.original_name])
        confirm_msg = f"即将重命名 {sel_count} 个视频文件及其关联字幕"
        if folder_count:
            confirm_msg += f"\n以及 {folder_count} 个文件夹"
        confirm_msg += "\n\n此操作不可撤销，是否继续？"
        if not messagebox.askyesno("确认重命名", confirm_msg):
            self._renaming = False
            return

        # 在后台线程中执行
        self.rename_btn.config(state=tk.DISABLED)
        self.status_text.set("正在重命名...")
        folders_to_rename = [f for f in self.folders if f.checked and f.new_name and f.new_name != f.original_name]

        # deep copy：完全脱离 self.entries，后续任何修改都不影响本次重命名
        import copy
        checked_copy = copy.deepcopy(checked)

        tasks = []
        for i, e in enumerate(checked_copy):
            tasks.append({
                'orig_idx': self.entries.index(checked[i]),
                'old_path': e.original_path,
                'new_path': os.path.join(e.parent_dir, sanitize_filename(e._new_name)),
                'old_name': e.original_name,
                'subs': [(s.original_path,
                          os.path.join(e.parent_dir, sanitize_filename(s.new_name)))
                         for s in e.subtitles if s.sync_enabled],
            })

        threading.Thread(
            target=execute_rename,
            args=(tasks, folders_to_rename, self.result_queue),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    #  队列轮询
    # ------------------------------------------------------------------

    def _poll_queue(self):
        """定时轮询后台线程的结果队列。"""
        try:
            while True:
                msg = self.result_queue.get_nowait()
                kind = msg[0]

                if kind == 'folders':
                    # 收到文件夹列表
                    self.folders.clear()
                    for f in msg[1]:
                        self.folders.append(f)
                        f.checked = self._rename_folders_var.get()
                    # 如果之前有 TMDB 信息，自动设置文件夹名
                    if self.tmdb_tv_name:
                        self._on_tmdb_loaded()
                    self._update_folder_rows()

                elif kind == 'batch':
                    # 收到一批扫描结果
                    for entry in msg[1]:
                        self.entries.append(entry)
                        self._insert_entry_row(entry, idx=len(self.entries) - 1)

                elif kind == 'error':
                    messagebox.showerror("扫描错误", msg[1])

                elif kind == 'done':
                    self._hide_scan_overlay()
                    self.scan_btn.config(state=tk.NORMAL, text='扫描')
                    # 自动偏移跨季集号
                    self._apply_season_offset()
                    # 偏移后重建表格，确保界面与数据一致
                    self._refresh_table()
                    # 自动 TMDB 搜索
                    self.root.after(100, self._auto_tmdb_search)
                    # msg = ('done', folder_entries, file_entries)
                    if len(msg) >= 3:
                        # 文件夹已在 'folders' 消息中处理
                        pass
                    self._refresh_status()
                    self._refresh_conflicts()

                elif kind == 'progress':
                    self.status_text.set(msg[1])

                elif kind == 'result':
                    self.rename_btn.config(state=tk.NORMAL)
                    success, fail = msg[1], msg[2]
                    self._refresh_table()
                    self.status_text.set(f"重命名完成：{success} 成功，{fail} 失败")
                    messagebox.showinfo("完成", f"重命名完成！\n成功: {success}\n失败: {fail}")
                    self._renaming = False

        except queue.Empty:
            pass
        finally:
            # 取走拖入的路径（窗口过程里不能调 tkinter，只能在这里处理）
            try:
                for paths in self._drop_hook.take_pending():
                    self._handle_drop(paths)
            except Exception:
                pass
            self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    #  表格刷新
    # ------------------------------------------------------------------

    def _update_folder_rows(self):
        """更新/插入文件夹行"""
        # 移除旧文件夹行
        for item in self.tree.get_children():
            if item.startswith('fd'):
                self.tree.delete(item)

        if not self.folders:
            return

        for i, f in enumerate(self.folders):
            kind_label = '根目录' if f.kind == 'root' else '季目录'
            original_display = f'{kind_label} · {f.original_name}'
            new_display = f.new_name or f.original_name
            checked = '●' if f.checked else '○'
            tags = ['folder']
            iid = f"fd{i}"
            self.tree.insert(
                '', i,
                iid=iid,
                values=(checked, f.season or '', '', original_display,
                        new_display, ''),
                tags=tuple(tags),
            )

    def _insert_entry_row(self, entry: FileEntry, idx: int):
        """插入一条视频行及其字幕子行（每行只挂一个底色标签）"""
        if entry.warning:
            row_tag = 'warn'
        elif entry.already_renamed:
            row_tag = 'renamed'
        else:
            row_tag = 'row_alt' if idx % 2 == 1 else 'row_normal'

        if entry.already_renamed:
            checked_mark = '◇'   # 已改名不可操作
        else:
            checked_mark = '●' if entry.checked else '○'
        ep_display = entry.episode if entry.episode else '—'

        # TMDB 集名
        tmdb_name = ''
        if self._show_tmdb_column.get() and entry.episode:
            try:
                ep_num = int(entry.episode)
                eps = self.tmdb_episodes.get(entry.season, {})
                info = eps.get(ep_num)
                if info:
                    tmdb_name = info.name
            except ValueError:
                pass

        video_iid = f"v{idx}"
        self.tree.insert(
            '', tk.END, iid=video_iid,
            values=(checked_mark, entry.season, ep_display,
                    entry.original_name, entry._new_name, tmdb_name),
            tags=(row_tag,),
        )

        # 插入字幕子行
        if entry.warning:
            sub_tag = 'sub_warn'
        else:
            sub_tag = 'sub_alt' if idx % 2 == 1 else 'sub'
        for sub in entry.subtitles:
            self.tree.insert(
                '', tk.END,
                values=('▸', '', '', f'  {sub.original_name}', sub.new_name, ''),
                tags=(sub_tag,),
            )

        self._update_empty_state()

    def _refresh_table(self):
        """完全重建表格。"""
        self._hovered_iid = None
        self._hover_saved = None
        self.tree.delete(*self.tree.get_children())
        self._update_folder_rows()
        for i, entry in enumerate(self.entries):
            self._insert_entry_row(entry, i)
        self._refresh_conflicts()
        self._refresh_status()
        self._update_empty_state()
        self._sync_scrollbars()

    def _refresh_conflicts(self):
        """检查并高亮冲突行。"""
        conflicts = detect_conflicts(self.entries)
        for a, b, path in conflicts:
            for iid in (f"v{a}", f"v{b}"):
                if self.tree.exists(iid):
                    self.tree.item(iid, tags=('conflict',))

    def _refresh_status(self):
        """更新底部统计胶囊与状态文字"""
        video_count = len(self.entries)
        sub_count = sum(len(e.subtitles) for e in self.entries)
        warning_count = sum(1 for e in self.entries if e.warning)
        conflicts = detect_conflicts(self.entries)
        conflict_count = len(conflicts)
        empty_ep_count = sum(1 for e in self.entries if not e.episode)
        folder_count = len(self.folders)

        self.chip_total.set(f'视频 {video_count}')
        self.chip_subs.set(f'字幕 {sub_count}')
        self.chip_warn.set(f'警告 {warning_count}',
                           'warn' if warning_count else 'neutral')
        self.chip_conflict.set(f'冲突 {conflict_count}',
                               'danger' if conflict_count else 'neutral')

        status_parts = [f'{video_count} 视频', f'{sub_count} 字幕']
        if folder_count:
            status_parts.append(f'{folder_count} 文件夹')
        if empty_ep_count:
            status_parts.append(f'{empty_ep_count} 未提取集号')
        if conflict_count:
            status_parts.append(f'{conflict_count} 冲突')
        if warning_count:
            status_parts.append(f'{warning_count} 需确认')
        self.status_text.set('  ·  '.join(status_parts))

        # 没有可执行项时禁用主按钮
        has_work = any(e.checked and e.episode for e in self.entries)
        try:
            self.rename_btn.config(
                state=tk.NORMAL if has_work and not self._renaming else tk.DISABLED)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    #  主题管理
    # ------------------------------------------------------------------

    def _apply_theme(self):
        """将当前主题应用到所有组件"""
        c = Theme

        # 根窗口
        self.root.configure(bg=c.bg)

        style = ttk.Style()
        try:
            style.theme_use('clam')   # clam 可定制性最佳
        except tk.TclError:
            pass

        def cfg(name, **opts):
            """样式配置容错包装：某些选项在特定 ttk 主题下不存在。"""
            try:
                style.configure(name, **opts)
            except tk.TclError:
                pass

        def mp(name, **opts):
            try:
                style.map(name, **opts)
            except tk.TclError:
                pass

        # 全局默认
        cfg('.', background=c.bg, foreground=c.text, font=Fonts.body)
        cfg('TFrame', background=c.bg)
        cfg('Card.TFrame', background=c.surface)
        cfg('TLabel', background=c.bg, foreground=c.text, font=Fonts.body)
        cfg('Muted.TLabel', background=c.surface, foreground=c.text_secondary,
            font=Fonts.small)

        # 输入框
        cfg('TEntry',
            fieldbackground=c.input_bg, foreground=c.input_text,
            insertcolor=c.text, bordercolor=c.border_strong,
            lightcolor=c.border_strong, darkcolor=c.border_strong,
            padding=(px(8), px(6)), relief='flat')
        mp('TEntry',
           bordercolor=[('focus', c.accent)],
           lightcolor=[('focus', c.accent)],
           darkcolor=[('focus', c.accent)],
           fieldbackground=[('disabled', c.surface_sunken)],
           foreground=[('disabled', c.text_muted)])

        # Combobox
        cfg('TCombobox',
            fieldbackground=c.input_bg, foreground=c.input_text,
            background=c.surface, arrowcolor=c.text_secondary, arrowsize=13,
            bordercolor=c.border_strong, lightcolor=c.border_strong,
            darkcolor=c.border_strong, padding=(px(8), px(4)))
        mp('TCombobox',
           fieldbackground=[('readonly', c.input_bg)],
           foreground=[('readonly', c.input_text)],
           bordercolor=[('focus', c.accent), ('hover', c.border_strong)],
           arrowcolor=[('active', c.accent)])

        # 下拉列表（Windows 上只能靠 option 数据库）
        self.root.option_add('*TCombobox*Listbox.background', c.surface)
        self.root.option_add('*TCombobox*Listbox.foreground', c.text)
        self.root.option_add('*TCombobox*Listbox.selectBackground', c.accent)
        self.root.option_add('*TCombobox*Listbox.selectForeground', c.accent_text)
        self.root.option_add('*TCombobox*Listbox.borderWidth', 0)

        # --- 按钮 ---
        # 主按钮（实心）
        cfg('Primary.TButton',
            background=c.accent, foreground=c.accent_text,
            borderwidth=0, focusthickness=0, focuscolor=c.accent,
            padding=(px(18), px(7)), relief='flat', font=Fonts.bold,
            bordercolor=c.accent, lightcolor=c.accent, darkcolor=c.accent)
        mp('Primary.TButton',
           background=[('disabled', '#C7D2E8'), ('pressed', c.accent_hover),
                       ('active', c.accent_hover)],
           foreground=[('disabled', '#F5F7FB')])

        # 主操作按钮（更大）
        cfg('Accent.TButton',
            background=c.accent, foreground=c.accent_text,
            borderwidth=0, focusthickness=0, focuscolor=c.accent,
            padding=(px(26), px(9)), relief='flat', font=Fonts.btn_primary,
            bordercolor=c.accent, lightcolor=c.accent, darkcolor=c.accent)
        mp('Accent.TButton',
           background=[('disabled', '#C7D2E8'), ('pressed', c.accent_hover),
                       ('active', c.accent_hover)],
           foreground=[('disabled', '#F5F7FB')])

        # 次级按钮（白底细描边）
        cfg('Ghost.TButton',
            background=c.surface, foreground='#334155',
            borderwidth=1, relief='solid', focusthickness=0, focuscolor=c.surface,
            padding=(px(14), px(6)), font=Fonts.btn,
            bordercolor='#D3DCE7', lightcolor='#D3DCE7', darkcolor='#D3DCE7')
        mp('Ghost.TButton',
           background=[('pressed', c.accent_soft), ('active', '#F4F7FF'),
                       ('disabled', c.surface)],
           foreground=[('active', c.accent_hover), ('pressed', c.accent_hover),
                       ('disabled', c.text_muted)],
           bordercolor=[('active', '#B9C6D6')],
           lightcolor=[('active', '#B9C6D6')],
           darkcolor=[('active', '#B9C6D6')])

        # 兼容旧样式名
        cfg('Secondary.TButton',
            background=c.surface, foreground='#334155',
            borderwidth=1, relief='solid', focusthickness=0, padding=(px(14), px(6)),
            font=Fonts.btn, bordercolor='#D3DCE7',
            lightcolor='#D3DCE7', darkcolor='#D3DCE7')
        mp('Secondary.TButton',
           background=[('active', '#F4F7FF')],
           foreground=[('active', c.accent_hover)])

        # 勾选框（放在白色卡片上）
        cfg('Card.TCheckbutton',
            background=c.surface, foreground=c.text_secondary,
            focuscolor=c.surface, font=Fonts.body)

        # 进度条
        cfg('TProgressbar',
            background=c.accent, troughcolor=c.surface_sunken,
            bordercolor=c.surface_sunken, lightcolor=c.accent,
            darkcolor=c.accent, thickness=px(5))
        cfg('Line.Horizontal.TProgressbar',
            background=c.accent, troughcolor=c.surface_sunken,
            bordercolor=c.surface_sunken, lightcolor=c.accent,
            darkcolor=c.accent, thickness=px(5))

        # --- 表格 ---
        cfg('Card.Treeview',
            background=c.surface, fieldbackground=c.surface,
            foreground=c.text, bordercolor=c.border,
            borderwidth=1, relief='flat', rowheight=px(30),
            font=Fonts.table)
        mp('Card.Treeview',
           background=[('selected', c.table_selected)],
           foreground=[('selected', c.text)])

        cfg('Card.Treeview.Heading',
            background=c.surface_alt, foreground=c.text_secondary,
            font=Fonts.table_head, borderwidth=0, relief='flat',
            padding=(px(10), px(9)), bordercolor=c.border)
        mp('Card.Treeview.Heading',
           background=[('active', c.table_hover)],
           foreground=[('active', c.text)])

        # --- 滚动条（无箭头，细长条）---
        for orient in ('Vertical', 'Horizontal'):
            cfg(f'Card.{orient}.TScrollbar',
                background=c.scrollbar, troughcolor=c.surface,
                bordercolor=c.surface, lightcolor=c.scrollbar,
                darkcolor=c.scrollbar, arrowcolor=c.surface,
                gripcount=0, arrowsize=0, relief='flat')
            mp(f'Card.{orient}.TScrollbar',
               background=[('active', c.text_muted)])

        cfg('TSeparator', background=c.border)

        # 更新自定义标签
        self._setup_tags()

        # 刷新表格（重建以应用新颜色）
        if hasattr(self, 'entries') and self.entries:
            self._refresh_table()

    # ------------------------------------------------------------------
    #  TMDB 集成
    # ------------------------------------------------------------------

    def _get_tmdb(self) -> Optional[TMDBClient]:
        """获取或创建 TMDB 客户端"""
        api_key = self.cfg.tmdb_api_key.strip()
        if not api_key:
            return None
        if self.tmdb is None:
            self.tmdb = TMDBClient(api_key, self.cfg.tmdb_language)
        return self.tmdb

    def _set_api_key(self):
        """设置 TMDB API Key"""
        current = self.cfg.tmdb_api_key
        key = simpledialog.askstring(
            "TMDB API Key",
            "请输入 TMDB API v3 Key（在 https://www.themoviedb.org/settings/api 申请）：\n\n留空则禁用 TMDB 功能",
            initialvalue=current,
        )
        if key is not None:
            self.cfg.set('tmdb_api_key', key.strip())
            self.tmdb = None  # 重置客户端
            if key.strip():
                self.tmdb_status.config(text='已设置 API Key', fg=Theme.success)
            else:
                self.tmdb_status.config(text='')

    def _auto_tmdb_search(self):
        """后台静默 TMDB 搜索（线程执行，不卡 UI）。"""
        tmdb = self._get_tmdb()
        if not tmdb:
            return

        folder_name = os.path.basename(os.path.normpath(self.folder_path.get())) or ''
        query = extract_clean_title(folder_name)
        if not query:
            return

        # 显示状态提示
        self._show_scan_overlay("🔍 正在自动匹配 TMDB 数据...")

        def _run():
            results = tmdb.search_tv(query)
            if not results:
                self.root.after(0, self._hide_scan_overlay)
                return

            r = results[0]
            tv_id = r['id']
            name = r.get('name', '?')
            date = r.get('first_air_date', '')[:4]

            name_en, year_en = tmdb.get_tv_name_year(tv_id)
            tv_name = name_en or name
            tv_year = str(year_en or date)

            # 加载分季信息
            episodes = {}
            seasons = tmdb.get_seasons(tv_id)
            for s in seasons:
                sn = s.get('season_number', 0)
                if sn <= 0:
                    continue
                ep_list = tmdb.get_episodes(tv_id, sn)
                season_str = f"{sn:02d}"
                eps = {}
                for ep in ep_list:
                    ep_num = ep.get('episode_number', 0)
                    eps[ep_num] = TmdbEpisodeInfo(
                        episode_number=ep_num,
                        name=ep.get('name', f'第{ep_num}集'),
                    )
                episodes[season_str] = eps

            # 回到主线程更新 UI（重命名进行中时跳过）
            def _update_ui():
                if getattr(self, '_renaming', False):
                    self._hide_scan_overlay()
                    return
                self.tmdb_tv_name = tv_name
                self.tmdb_tv_year = tv_year
                self.tmdb_tv_id = tv_id
                self.tmdb_episodes = episodes
                self._show_tmdb_column.set(True)
                self._on_tmdb_loaded()
                self.tmdb_status.config(
                    text=f'{self.tmdb_tv_name} ({self.tmdb_tv_year})',
                    fg=Theme.success,
                )
                self._refresh_table()
                self._hide_scan_overlay()

            self.root.after(0, _update_ui)

        threading.Thread(target=_run, daemon=True).start()

    def _search_tmdb(self):
        """打开 TMDB 搜索弹窗"""
        tmdb = self._get_tmdb()
        if not tmdb:
            if not messagebox.askyesno("未配置 API Key",
                                        "需要 TMDB API Key 才能使用此功能。\n\n是否现在设置？"):
                return
            self._set_api_key()
            tmdb = self._get_tmdb()
            if not tmdb:
                return

        # 搜索弹窗
        dlg = tk.Toplevel(self.root)
        dlg.title("TMDB 匹配剧集")
        dlg.geometry(f"{px(620)}x{px(560)}")
        dlg.configure(bg=Theme.bg)
        dlg.transient(self.root)
        dlg.grab_set()

        # 搜索卡片
        top = tk.Frame(dlg, bg=Theme.bg, padx=px(16), pady=px(14))
        top.pack(fill=tk.X)

        card = tk.Frame(top, bg=Theme.surface,
                        highlightbackground=Theme.border, highlightthickness=1)
        card.pack(fill=tk.X)
        cpad = tk.Frame(card, bg=Theme.surface, padx=px(14), pady=px(12))
        cpad.pack(fill=tk.X)

        tk.Label(cpad, text='搜索剧名', font=Fonts.small,
                 bg=Theme.surface, fg=Theme.text_secondary).pack(anchor='w')

        search_row = tk.Frame(cpad, bg=Theme.surface)
        search_row.pack(fill=tk.X, pady=(px(6), 0))

        # 智能提取标题
        folder_name = os.path.basename(os.path.normpath(self.folder_path.get())) or ''
        clean_title = extract_clean_title(folder_name)
        query_var = tk.StringVar(value=clean_title)
        query_entry = ttk.Entry(search_row, textvariable=query_var,
                                font=Fonts.body)
        query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        query_entry.select_range(0, tk.END)
        query_entry.focus_set()

        # 显示原始文件夹名供参考
        if clean_title != folder_name:
            tk.Label(cpad, text=f'来自文件夹：{folder_name}',
                     font=Fonts.small, fg=Theme.text_muted, bg=Theme.surface,
                     wraplength=px(540), justify='left').pack(anchor='w', pady=(px(8), 0))

        # 结果列表
        list_frame = tk.Frame(dlg, bg=Theme.surface,
                              highlightbackground=Theme.border, highlightthickness=1)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=px(16))

        columns = ('name', 'year', 'lang')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings',
                            selectmode='browse', height=8, style='Card.Treeview')
        tree.heading('name', text='剧名', anchor='w')
        tree.heading('year', text='首播年份', anchor='center')
        tree.heading('lang', text='语言', anchor='center')
        tree.column('name', width=px(380), minwidth=px(180), stretch=True)
        tree.column('year', width=px(90), anchor='center', stretch=False)
        tree.column('lang', width=px(70), anchor='center', stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview,
                            style='Card.Vertical.TScrollbar')
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, px(5)), pady=px(6))
        tree.pack(fill=tk.BOTH, expand=True)

        # 底部操作区
        btn_frame = tk.Frame(dlg, bg=Theme.bg, padx=px(16), pady=px(12))
        btn_frame.pack(fill=tk.X)

        result_label = tk.Label(btn_frame, text='', bg=Theme.bg, fg=Theme.text_secondary,
                                font=Fonts.small)
        result_label.pack(side=tk.LEFT)

        def do_search():
            query = query_var.get().strip()
            if not query:
                return
            result_label.config(text='搜索中...')
            dlg.update()
            results = tmdb.search_tv(query)
            tree.delete(*tree.get_children())
            if not results:
                result_label.config(text='无结果')
                return
            result_label.config(text=f'找到 {len(results)} 个结果')
            for r in results:
                name = r.get('name', '?')
                date = r.get('first_air_date', '')[:4]
                lang = r.get('original_language', '?').upper()
                tree.insert('', tk.END, iid=str(r['id']),
                            values=(name, date, lang))

        def on_select():
            sel = tree.selection()
            if not sel:
                return
            tv_id = int(sel[0])
            item = tree.item(sel[0])
            name = item['values'][0]
            year = item['values'][1]

            # 获取详细信息
            result_label.config(text=f'获取 "{name}" 分季信息...')
            dlg.update()
            name_en, year_en = tmdb.get_tv_name_year(tv_id)
            if name_en:
                self.tmdb_tv_name = name_en
            else:
                self.tmdb_tv_name = name
            self.tmdb_tv_year = str(year_en or year)
            self.tmdb_tv_id = tv_id

            # 加载所有季的集信息
            self.tmdb_episodes.clear()
            seasons = tmdb.get_seasons(tv_id)
            for s in seasons:
                sn = s.get('season_number', 0)
                if sn <= 0:
                    continue
                ep_list = tmdb.get_episodes(tv_id, sn)
                season_str = f"{sn:02d}"
                eps = {}
                for ep in ep_list:
                    ep_num = ep.get('episode_number', 0)
                    eps[ep_num] = TmdbEpisodeInfo(
                        episode_number=ep_num,
                        name=ep.get('name', f'第{ep_num}集'),
                    )
                self.tmdb_episodes[season_str] = eps

            self._show_tmdb_column.set(True)
            self._on_tmdb_loaded()
            self.tmdb_status.config(
                text=f'{self.tmdb_tv_name} ({self.tmdb_tv_year})',
                fg=Theme.success,
            )
            dlg.destroy()

        query_entry.bind('<Return>', lambda e: do_search())
        ttk.Button(search_row, text='搜索', command=do_search,
                   style='Primary.TButton').pack(side=tk.LEFT, padx=(px(8), 0))

        ttk.Button(btn_frame, text='确认选择', command=on_select,
                   style='Accent.TButton').pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text='取消', command=dlg.destroy,
                   style='Ghost.TButton').pack(side=tk.RIGHT, padx=(0, px(8)))

        # 自动搜索
        if folder_name:
            dlg.after(200, do_search)

    def _apply_season_offset(self):
        """把跨季的全局集号（如 S2 从 13 开始）换算成季内编号。

        规则：
        1. 特典季（S00）不参与累计，也不做偏移；
        2. 每季的「全局起始集号」= 前面所有季的集数 + 1，前面的季优先用
           TMDB 集数，取 max(TMDB, 本季文件数)——用户文件比 TMDB 多时，
           说明他手上的编号是跟着文件走的；
        3. 只有当最小集号恰好落在 [起始集号, 起始集号+本季集数) 区间时，
           才认定为全局编号并偏移；否则原样保留（宁可不动，也不猜）；
        4. 偏移后超出本季集数的行打警告，让用户确认。
        """
        if getattr(self, '_renaming', False):
            return

        # 特典季不参与
        seasons = sorted({e.season for e in self.entries
                          if e.season and e.season != "00"})
        if len(seasons) <= 1:
            return

        def file_count(s):
            return sum(1 for e in self.entries
                       if e.season == s and e.episode and not e.already_renamed)

        def season_count(s):
            tmdb_n = len(self.tmdb_episodes.get(s) or {})
            return max(tmdb_n, file_count(s))

        def prev_total(s):
            """该季之前（不含特典）累计的集数。"""
            keys = set(self.tmdb_episodes) | set(seasons)
            return sum(season_count(t) for t in keys
                       if t != "00" and t < s)

        changed = False
        for s in seasons:
            files = [e for e in self.entries
                     if e.season == s and e.episode and not e.already_renamed]
            if not files:
                continue
            nums = [int(e.episode) for e in files if e.episode.isdigit()]
            if not nums:
                continue

            offset = prev_total(s)          # 前几季的总集数 == 需要减掉的量
            if offset <= 0:
                continue                    # 第一季无需偏移
            count = season_count(s) or len(files)
            min_ep = min(nums)

            # 判据：最小集号必须落在本季的全局区间内，否则视为已按季编号
            if not (offset + 1 <= min_ep < offset + 1 + count):
                continue

            max_after = 0
            for e in files:
                try:
                    num = int(e.episode)
                except ValueError:
                    continue
                if num <= offset:
                    continue
                e.episode = str(num - offset)
                changed = True
                max_after = max(max_after, num - offset)

            if max_after > count:
                warn = f"跨季偏移后集号 {max_after} 超出本季集数 ({count})"
                for e in files:
                    if e.warning:
                        if warn not in e.warning:
                            e.warning += "；" + warn
                    else:
                        e.warning = warn

        if changed:
            for e in self.entries:
                compute_new_names(e)

    def _on_tmdb_loaded(self):
        """TMDB 数据加载后：自动偏移跨季集号 + 刷新 TMDB 列"""
        self._apply_season_offset()

        # 自动设置文件夹新名
        if self.tmdb_tv_name and self.tmdb_tv_year:
            for f in self.folders:
                if f.kind == 'root':
                    f.new_name = f"{self.tmdb_tv_name} ({self.tmdb_tv_year})"
        for f in self.folders:
            if f.kind == 'season' and f.season:
                f.new_name = f"Season {f.season}"

        # ---- 步骤 4：校验集号范围并标记警告 ----
        for e in self.entries:
            eps = self.tmdb_episodes.get(e.season, {})
            if eps:
                try:
                    ep_num = int(e.episode)
                    max_ep = max(eps.keys()) if eps else 0
                    if ep_num in eps:
                        eps[ep_num].validated = True
                    if ep_num > max_ep:
                        warn = f"集号 {ep_num} 超出 TMDB 记录 ({max_ep} 集)"
                        e.warning = warn if not e.warning else e.warning + "；" + warn
                except ValueError:
                    pass

        self._refresh_table()

    def _on_toggle_rename_folders(self):
        """切换文件夹重命名开关"""
        self.cfg.set('rename_folders', self._rename_folders_var.get())
        for f in self.folders:
            f.checked = self._rename_folders_var.get()
        self._refresh_table()

    # ------------------------------------------------------------------
    #  帮助 / 关于
    # ------------------------------------------------------------------

    def _show_help(self):
        text = (
            "使用说明\n"
            "════════\n\n"
            "1. 选择动漫根文件夹（如 海贼王/）：\n"
            "   - 点击「浏览」，或\n"
            "   - 直接把文件夹从资源管理器拖进窗口\n"
            "2. 点击「扫描」自动识别分季子文件夹和集号\n"
            "3. 在表格中审核结果：\n"
            "   - 单击「选」列勾选/取消文件\n"
            "   - 双击「季」或「集」列编辑序号\n"
            "   - 琥珀色行表示集号可疑，需要确认\n"
            "4. 点击「执行重命名」批量重命名\n\n"
            "命名格式：S{季号}E{集号}_{原文件名}.ext\n"
            "字幕文件会自动同步重命名。"
        )
        messagebox.showinfo("使用说明", text)

    def _show_about(self):
        text = (
            "AniPrep 动漫媒体库规范化重命名工具\n"
            "版本 1.6\n\n"
            "将动漫视频及外挂字幕重命名为\n"
            "Emby/Jellyfin 友好格式\n\n"
            "• 支持 TMDB 自动匹配季集信息\n"
            "• 支持 S0 (特典/Specials) 季号\n"
            "• 跨季集号自动偏移\n"
            "• 文件夹同步重命名\n"
            "• 支持把文件夹直接拖进窗口\n"
            "• 高 DPI 屏幕清晰渲染"
        )
        messagebox.showinfo("关于", text)


# ============================================================================
#  入口
# ============================================================================

def main():
    # 必须在创建 Tk 之前开启 DPI 感知，否则窗口会被系统放大拉伸（文字发虚）
    enable_dpi_awareness()
    root = tk.Tk()
    app = AniPrepApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
