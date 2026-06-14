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
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import json
import requests

# ============================================================================
#  配置持久化
# ============================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "aniprep_config.json"

class Config:
    """全局配置，读写 JSON 文件"""
    _defaults = {
        "tmdb_api_key": "",
        "tmdb_language": "zh-CN",
        "last_root_folder": "",
        "rename_folders": False,
        "window_geometry": "1160x720",
    }

    def __init__(self):
        self._data = dict(self._defaults)
        self._load()

    def _load(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                for k in self._defaults:
                    if k in loaded:
                        self._data[k] = loaded[k]
        except Exception:
            pass

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
#  主题色彩
# ============================================================================

class Theme:
    """全局色彩定义"""
    bg              = "#F0F2F5"
    surface         = "#FFFFFF"
    text            = "#1A1A2E"
    text_secondary  = "#6B7280"
    accent          = "#6490F5"    # 更浅的蓝
    accent_hover    = "#4F7DE8"
    accent_text     = "#FFFFFF"
    success         = "#10B981"
    warning         = "#D97706"
    danger          = "#EF4444"
    border          = "#D1D5DB"
    grid            = "#AEB3BA"     # 表格列分隔线
    scrollbar       = "#CBD5E1"
    header_bg       = "#FFFFFF"
    header_text     = "#1A1A2E"
    table_alt       = "#F0F2F5"
    table_hover     = "#EEF2FF"
    table_selected  = "#E0E5FF"
    input_bg        = "#FFFFFF"
    input_text      = "#1A1A2E"


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

def extract_clean_title(folder_name: str) -> str:
    """从文件夹名中提取干净的剧名。

    剥离常见标记：开头的 [压制组]、编码参数 (...) 等。
    例：
      [ANK-Raws] Omamori Himari (BDrip 1920x1080 x264 FLAC)
      → Omamori Himari
      [DBD-Raws][邪神与厨二病少女][01-12TV全集][1080P][BDRip][FLAC]
      → 邪神与厨二病少女
    """
    import re
    name = folder_name.strip()

    # 1. 剥离所有开头的 ASCII 方括号块（压制组/来源），不含 CJK 的视为标题保留
    name = re.sub(r'^(?:\[[ -~]+\]\s*)+', '', name).strip()

    # 2. 去除杂项方括号（集数范围、分辨率、编码、字幕标记等技术信息）
    tech_sq = r'\[[^\]]*(?:\d{2,4}[PpKk]|BDRip|BD|Blu-ray|DVD|WEB|HDTV|' \
              r'x264|x265|H264|H265|HEVC|AVC|Hi10P|10bit|8bit|' \
              r'FLAC|AAC|MP3|AC3|DTS|MKV|MP4|' \
              r'简繁|外挂|CHS|CHT|GB|BIG5|' \
              r'TV|全集|特典|映像|OVA|SP)[^\]]*\]'
    name = re.sub(tech_sq, '', name, flags=re.I).strip()

    # 3. 去除技术参数圆括号
    tech_rn = r'\([^)]*\b(?:BD|BDRip|Blu-ray|DVD|WEB|TV|HDTV|' \
              r'1080|720|480|2160|4K|8K|' \
              r'x264|x265|H264|H265|HEVC|AVC|FLAC|AAC|MP3|AC3|DTS|' \
              r'Hi10P|10bit|8bit|MKV|MP4|' \
              r'简繁|外挂|GB|BIG5|CHS|CHT|JP|EN|Sub|ASS|SRT)[^)]*\)'
    name = re.sub(tech_rn, '', name, flags=re.I).strip()

    # 4. 剩余方括号全部剥除（tech 已去，剩下的就是标题本身）
    name = re.sub(r'\[([^\]]+)\]', r'\1', name).strip()
    # 5. 清理多余空格
    name = re.sub(r'\s+', ' ', name).strip()
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

    tasks: [{'ref': FileEntry, 'old_path': str, 'new_path': str,
             'old_name': str, 'episode': str, 'new_name': str,
             'subs': [(old, new), ...]}, ...]
    所有路径数据已冻结，不依赖原始 entry 对象。
    """
    success = 0
    fail = 0
    total = len(tasks)

    for i, task in enumerate(tasks):
        # 重命名字幕
        for sub_old, sub_new in task['subs']:
            try:
                if sub_old != sub_new and os.path.exists(sub_old):
                    os.rename(sub_old, sub_new)
            except OSError:
                pass

        # 重命名视频
        try:
            if task['old_path'] != task['new_path'] and os.path.exists(task['old_path']):
                os.rename(task['old_path'], task['new_path'])
                task['ref'].original_path = task['new_path']
                # 用冻结值覆盖可能被篡改的字段
                task['ref'].episode = task['episode']
                task['ref']._new_name = task['new_name']
                task['ref'].status = '✓'
                success += 1
            else:
                task['ref'].status = '✓'
                success += 1
        except OSError as e:
            task['ref'].status = f'✗ {e}'
            fail += 1

        result_queue.put(('progress', f'[{i+1}/{total}] {task["ref"].status} {task["old_name"]}'))

    # 文件夹重命名
    if folders:
        for f in sorted(folders, key=lambda x: x.original_path.count(os.sep), reverse=True):
            try:
                new_path = os.path.join(f.parent_dir, sanitize_filename(f.new_name))
                if new_path != f.original_path and os.path.exists(f.original_path):
                    os.rename(f.original_path, new_path)
                    f.status = '✓'
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
        self.root.title("AniPrep — 动漫媒体文件规范化重命名工具")
        self.root.geometry("1200x760")
        self.root.minsize(900, 500)

        # 配置
        self.cfg = Config()
        self.root.geometry(self.cfg.window_geometry)

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

        # 窗口关闭时保存配置
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """窗口关闭时保存状态"""
        try:
            self.cfg.set('window_geometry', self.root.geometry())
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
        file_menu.add_command(label="📁 选择文件夹", command=self._browse_folder)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="文件", menu=file_menu)

        # 设置菜单
        settings_menu = tk.Menu(menubar, tearoff=0,
                                bg=Theme.surface,
                                fg=Theme.text,
                                activebackground=Theme.accent,
                                activeforeground=Theme.accent_text)
        settings_menu.add_command(label="🔑 TMDB API Key 设置...", command=self._set_api_key)
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
        """构建顶部标题横幅"""
        self.header_frame = tk.Frame(self.root, bg=Theme.header_bg,
                                     padx=24, pady=14)
        self.header_frame.pack(fill=tk.X)

        title_container = tk.Frame(self.header_frame, bg=Theme.header_bg)
        title_container.pack(side=tk.LEFT)

        tk.Label(title_container, text="AniPrep",
                 font=('Segoe UI', 18, 'bold'),
                 fg=Theme.accent,
                 bg=Theme.header_bg).pack(side=tk.LEFT)
        tk.Label(title_container, text=" 动漫媒体规范化重命名",
                 font=('Segoe UI', 12),
                 fg=Theme.text_secondary,
                 bg=Theme.header_bg).pack(side=tk.LEFT, padx=(6, 0))

        # 分隔线
        self.header_sep = tk.Frame(self.root, height=1, bg=Theme.accent)
        self.header_sep.pack(fill=tk.X)

    # ------------------------------------------------------------------
    #  控制栏
    # ------------------------------------------------------------------

    def _build_control_bar(self):
        """构建文件夹选择 & 操作按钮栏（两行布局）"""
        self.control_frame = tk.Frame(self.root, bg=Theme.bg, padx=20, pady=12)
        self.control_frame.pack(fill=tk.X)

        # ---- 第一行：路径选择 ----
        row1 = tk.Frame(self.control_frame, bg=Theme.bg)
        row1.pack(fill=tk.X)

        tk.Label(row1, text='文件夹', font=('Segoe UI', 10, 'bold'),
                 fg=Theme.text_secondary, bg=Theme.bg).pack(side=tk.LEFT)

        self.folder_entry = ttk.Entry(row1, textvariable=self.folder_path, width=44,
                                      font=('Segoe UI', 12))
        self.folder_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(row1, text='浏览', command=self._browse_folder,
                   style='Secondary.TButton').pack(side=tk.LEFT, padx=2)
        self.scan_btn = ttk.Button(row1, text='扫描', command=self._start_scan,
                                   style='Primary.TButton')
        self.scan_btn.pack(side=tk.LEFT, padx=(2, 16))

        tk.Label(row1, text='默认季号', font=('Segoe UI', 10, 'bold'),
                 fg=Theme.text_secondary, bg=Theme.bg).pack(side=tk.LEFT, padx=(0, 4))
        self.season_combo = ttk.Combobox(
            row1, textvariable=self.default_season, width=3,
            values=[f"{i:02d}" for i in range(0, 100)],
            state='readonly', font=('Segoe UI', 12),
        )
        self.season_combo.pack(side=tk.LEFT)

        # ---- 第二行：TMDB 按钮（左） + 快捷操作（右） ----
        row2 = tk.Frame(self.control_frame, bg=Theme.bg)
        row2.pack(fill=tk.X, pady=(6, 0))

        left2 = tk.Frame(row2, bg=Theme.bg)
        left2.pack(side=tk.LEFT)

        self.tmdb_btn_search = ttk.Button(left2, text='🔍 TMDB 搜索',
                                           command=self._search_tmdb,
                                           style='Secondary.TButton')
        self.tmdb_btn_search.pack(side=tk.LEFT, padx=2)
        self.tmdb_status = tk.Label(left2, text='', font=('Segoe UI', 10),
                                    fg=Theme.success, bg=Theme.bg)
        self.tmdb_status.pack(side=tk.LEFT, padx=(6, 0))

        right2 = tk.Frame(row2, bg=Theme.bg)
        right2.pack(side=tk.RIGHT)

        self._rename_folders_var = tk.BooleanVar(value=self.cfg.rename_folders)
        self.chk_rename_folders = tk.Checkbutton(
            right2, text='同步重命名文件夹',
            variable=self._rename_folders_var,
            command=self._on_toggle_rename_folders,
            bg=Theme.bg, fg=Theme.text,
            selectcolor=Theme.bg, activebackground=Theme.bg,
            activeforeground=Theme.text,
            font=('Segoe UI', 10),
        )
        self.chk_rename_folders.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(right2, text='全选', command=self._select_all,
                   style='Secondary.TButton').pack(side=tk.LEFT, padx=2)
        ttk.Button(right2, text='取消全选', command=self._deselect_all,
                   style='Secondary.TButton').pack(side=tk.LEFT, padx=2)
        ttk.Button(right2, text='仅选中 ⚠️', command=self._select_warnings,
                   style='Secondary.TButton').pack(side=tk.LEFT, padx=2)

    # ------------------------------------------------------------------
    #  预览表格（增强版）
    # ------------------------------------------------------------------

    def _build_table(self):
        self.table_frame = tk.Frame(self.root, bg=Theme.bg,
                                    padx=20, pady=2)
        self.table_frame.pack(fill=tk.BOTH, expand=True)

        # 表格容器（带边框效果）
        self.table_inner = tk.Frame(self.table_frame, bg=Theme.border)
        self.table_inner.pack(fill=tk.BOTH, expand=True)

        columns = ('checked', 'season', 'episode', 'original', 'new_name', 'tmdb_name')
        self.tree = ttk.Treeview(
            self.table_inner,
            columns=columns,
            show='headings',
            selectmode='extended',
        )

        self.tree.heading('checked', text='')
        self.tree.heading('season', text='季')
        self.tree.heading('episode', text='集')
        self.tree.heading('original', text='原文件名')
        self.tree.heading('new_name', text='新文件名')
        self.tree.heading('tmdb_name', text='TMDB 集名')

        self.tree.column('checked', width=32, anchor='center', stretch=False)
        self.tree.column('season', width=40, anchor='center', stretch=False)
        self.tree.column('episode', width=50, anchor='center', stretch=False)
        self.tree.column('original', width=300, minwidth=120)
        self.tree.column('new_name', width=340, minwidth=140)
        self.tree.column('tmdb_name', width=200, minwidth=60, stretch=True)

        # 滚动条
        vsb = ttk.Scrollbar(self.table_inner, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(self.table_inner, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)

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
            tags = list(self.tree.item(iid, 'tags'))
            if 'conflict' in tags:
                return
            tags.append('hover')
            self.tree.item(iid, tags=tuple(tags))

    def _restore_row_tag(self, iid):
        """恢复一行的原始标签（移除 hover）"""
        if not self.tree.exists(iid):
            return
        tags = list(self.tree.item(iid, 'tags'))
        if 'hover' in tags:
            tags.remove('hover')
        self.tree.item(iid, tags=tuple(tags))

    def _clear_hover(self):
        """清除 hover 状态"""
        if self._hovered_iid and self.tree.exists(self._hovered_iid):
            self._restore_row_tag(self._hovered_iid)
        self._hovered_iid = None

    def _setup_tags(self):
        """配置表格行样式标签（颜色来自当前主题）。"""
        c = Theme
        self.tree.tag_configure('warning', background=c.warning, foreground='#1A1A2E')
        self.tree.tag_configure('subtitle', background=c.table_alt, foreground=c.text_secondary)
        self.tree.tag_configure('conflict', background=c.danger, foreground='#FFFFFF')
        self.tree.tag_configure('success', foreground=c.success)
        self.tree.tag_configure('failed', foreground=c.danger)
        self.tree.tag_configure('renamed', foreground=c.accent)
        self.tree.tag_configure('hover', background=c.table_hover)
        self.tree.tag_configure('even', background=c.table_alt)
        # placeholder: 集号为空的行用斜体灰字提示
        self.tree.tag_configure('empty_ep', foreground=c.text_secondary)
        self.tree.tag_configure('folder', background=c.table_alt, font=('Segoe UI', 10, 'bold'))

    # ------------------------------------------------------------------
    #  底部操作栏 & 状态栏
    # ------------------------------------------------------------------

    def _build_footer(self):
        """构建底部区域：操作按钮 + 进度 + 统计芯片 + 状态"""
        self.footer_frame = tk.Frame(self.root, bg=Theme.surface,
                                     padx=20, pady=10)
        self.footer_frame.pack(fill=tk.X)

        # 左侧：统计芯片
        left = tk.Frame(self.footer_frame, bg=Theme.surface)
        left.pack(side=tk.LEFT)

        self.chip_total = self._make_chip(left, '📄 0')
        self.chip_total.pack(side=tk.LEFT, padx=3)
        self.chip_subs = self._make_chip(left, '📝 0')
        self.chip_subs.pack(side=tk.LEFT, padx=3)
        self.chip_warn = self._make_chip(left, '⚠️ 0')
        self.chip_warn.pack(side=tk.LEFT, padx=3)
        self.chip_conflict = self._make_chip(left, '❌ 0')
        self.chip_conflict.pack(side=tk.LEFT, padx=3)

        # 右侧：状态文字 + 执行按钮
        right = tk.Frame(self.footer_frame, bg=Theme.surface)
        right.pack(side=tk.RIGHT)

        self.status_label = tk.Label(
            right, textvariable=self.status_text,
            font=('Segoe UI', 10),
            fg=Theme.text_secondary,
            bg=Theme.surface,
        )
        self.status_label.pack(side=tk.RIGHT, padx=(0, 12))

        self.rename_btn = ttk.Button(
            right, text='执行重命名', command=self._execute_rename,
            style='Accent.TButton',
        )
        self.rename_btn.pack(side=tk.RIGHT)

    def _make_chip(self, parent, text):
        """创建一个统计芯片标签"""
        chip = tk.Label(
            parent, text=text,
            font=('Segoe UI', 10),
            fg=Theme.text_secondary,
            bg=Theme.table_alt,
            padx=8, pady=2,
        )
        return chip

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
        self._show_scan_overlay("🔍 正在扫描，请稍候...")

        threading.Thread(
            target=scan_folder,
            args=(path, self.default_season.get(), self.result_queue),
            daemon=True,
        ).start()

    def _show_scan_overlay(self, msg: str):
        """在表格区域显示醒目的状态提示"""
        if not hasattr(self, 'table_frame'):
            return
        self._hide_scan_overlay()
        self._scan_overlay = tk.Label(
            self.table_frame,
            text=msg,
            font=('Segoe UI', 18, 'bold'),
            fg=Theme.accent,
            bg=Theme.bg,
        )
        self._scan_overlay.place(relx=0.5, rely=0.5, anchor='center')

    def _hide_scan_overlay(self):
        if hasattr(self, '_scan_overlay') and self._scan_overlay:
            self._scan_overlay.destroy()
            self._scan_overlay = None

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
                f"  ⚠️ {e.original_name} — {e.warning}" for e in warnings[:10]
            )
            more = f"\n  ... 等共 {len(warnings)} 个" if len(warnings) > 10 else ""
            if not messagebox.askyesno(
                "⚠️ 警告确认",
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

        # 冻结任务数据：脱离 entry 对象引用，纯字符串副本
        tasks = []
        for e in checked:
            tasks.append({
                'ref': e,
                'old_path': e.original_path,
                'new_path': os.path.join(e.parent_dir, sanitize_filename(e._new_name)),
                'old_name': e.original_name,
                'episode': e.episode,       # 冻结当前值
                'new_name': e._new_name,    # 冻结当前值
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
            icon = '🗂️ 根目录' if f.kind == 'root' else '  🗂️ 季目录'
            original_display = f.original_name
            new_display = f.new_name or f.original_name
            checked = '●' if f.checked else '○'
            tags = ['folder']
            iid = f"fd{i}"
            self.tree.insert(
                '', i,
                iid=iid,
                values=(checked, '', '', f'{icon}  {original_display}', new_display, ''),
                tags=tuple(tags),
            )

    def _insert_entry_row(self, entry: FileEntry, idx: int):
        """插入一条视频行及其字幕子行（支持交替行色）"""
        tags = []
        if entry.already_renamed:
            tags.append('renamed')
        if entry.warning:
            tags.append('warning')
        if not entry.episode:
            tags.append('empty_ep')
        if idx % 2 == 1:
            tags.append('even')

        if entry.already_renamed:
            checked_mark = '◇'   # 已改名不可操作
        else:
            checked_mark = '●' if entry.checked else '○'
        ep_display = entry.episode if entry.episode else '—'
        if entry.episode and entry.warning:
            ep_display = f"⚡{entry.episode}"

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
            tags=tuple(tags),
        )

        # 插入字幕子行
        for sub in entry.subtitles:
            sub_tags = ['subtitle']
            if entry.warning:
                sub_tags.append('warning')
            if idx % 2 == 1:
                sub_tags.append('even')
            self.tree.insert(
                '', tk.END,
                values=('└', '', '', f'  {sub.original_name}', sub.new_name, ''),
                tags=tuple(sub_tags),
            )

    def _refresh_table(self):
        """完全重建表格。"""
        self.tree.delete(*self.tree.get_children())
        self._update_folder_rows()
        for i, entry in enumerate(self.entries):
            self._insert_entry_row(entry, i)
        self._refresh_conflicts()
        self._refresh_status()

    def _refresh_conflicts(self):
        """检查并高亮冲突行。"""
        conflicts = detect_conflicts(self.entries)
        for a, b, path in conflicts:
            video_iid_a = f"v{a}"
            video_iid_b = f"v{b}"
            try:
                current_tags_a = list(self.tree.item(video_iid_a, 'tags'))
                if 'conflict' not in current_tags_a:
                    current_tags_a.append('conflict')
                self.tree.item(video_iid_a, tags=tuple(current_tags_a))
            except tk.TclError:
                pass
            try:
                current_tags_b = list(self.tree.item(video_iid_b, 'tags'))
                if 'conflict' not in current_tags_b:
                    current_tags_b.append('conflict')
                self.tree.item(video_iid_b, tags=tuple(current_tags_b))
            except tk.TclError:
                pass

    def _refresh_status(self):
        """更新底部状态栏统计芯片"""
        video_count = len(self.entries)
        sub_count = sum(len(e.subtitles) for e in self.entries)
        warning_count = sum(1 for e in self.entries if e.warning)
        conflicts = detect_conflicts(self.entries)
        conflict_count = len(conflicts)
        empty_ep_count = sum(1 for e in self.entries if not e.episode)
        folder_count = len(self.folders)

        c = Theme
        self.chip_total.config(text=f'📄 {video_count}')
        self.chip_subs.config(text=f'📝 {sub_count}')
        if warning_count:
            self.chip_warn.config(text=f'⚠️ {warning_count}', fg='#1A1A2E', bg=c.warning)
        else:
            self.chip_warn.config(text=f'⚠️ 0', fg=c.text_secondary, bg=c.table_alt)
        if conflict_count:
            self.chip_conflict.config(text=f'❌ {conflict_count}', fg='#FFFFFF', bg=c.danger)
        else:
            self.chip_conflict.config(text=f'❌ 0', fg=c.text_secondary, bg=c.table_alt)

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

    # ------------------------------------------------------------------
    #  主题管理
    # ------------------------------------------------------------------

    def _apply_theme(self):
        """将当前主题应用到所有组件"""
        c = Theme

        # 根窗口
        self.root.configure(bg=c.bg)

        # ttk 样式
        style = ttk.Style()
        # 使用 clam 作为基底（可定制性最佳）
        style.theme_use('clam')

        # 全局默认
        style.configure('.',
                        background=c.bg,
                        foreground=c.text,
                        font=('Segoe UI', 12))

        # 框架
        style.configure('TFrame', background=c.bg)

        # 标签
        style.configure('TLabel',
                        background=c.bg,
                        foreground=c.text,
                        font=('Segoe UI', 12))

        # 输入框
        style.configure('TEntry',
                        fieldbackground=c.input_bg,
                        foreground=c.input_text,
                        bordercolor=c.border,
                        lightcolor=c.border,
                        darkcolor=c.border)
        style.map('TEntry',
                  fieldbackground=[('disabled', c.bg)],
                  bordercolor=[('focus', c.accent)])

        # Combobox
        style.configure('TCombobox',
                        fieldbackground=c.input_bg,
                        foreground=c.input_text,
                        background=c.input_bg,
                        arrowcolor=c.text_secondary,
                        bordercolor=c.border)
        style.map('TCombobox',
                  fieldbackground=[('readonly', c.input_bg)],
                  foreground=[('readonly', c.input_text)])

        # 下拉列表
        self.root.option_add('*TCombobox*Listbox.background', c.surface)
        self.root.option_add('*TCombobox*Listbox.foreground', c.text)
        self.root.option_add('*TCombobox*Listbox.selectBackground', c.accent)
        self.root.option_add('*TCombobox*Listbox.selectForeground', c.accent_text)
        # 也通过 style 设置
        style.configure('TCombobox.Listbox',
                        background=c.surface,
                        foreground=c.text,
                        selectbackground=c.accent,
                        selectforeground=c.accent_text)

        # 主按钮
        style.configure('Primary.TButton',
                        background=c.accent,
                        foreground=c.accent_text,
                        borderwidth=0,
                        focusthickness=0,
                        padding=(18, 6),
                        font=('Segoe UI', 10, 'bold'))
        style.map('Primary.TButton',
                  background=[('active', c.accent_hover), ('disabled', c.border)],
                  foreground=[('disabled', c.text_secondary)])

        # 强调按钮
        style.configure('Accent.TButton',
                        background=c.accent,
                        foreground=c.accent_text,
                        borderwidth=0,
                        focusthickness=0,
                        padding=(22, 8),
                        font=('Segoe UI', 11, 'bold'))
        style.map('Accent.TButton',
                  background=[('active', c.accent_hover), ('disabled', c.border)])

        # 次要按钮
        style.configure('Secondary.TButton',
                        background=c.surface,
                        foreground=c.text,
                        borderwidth=1,
                        bordercolor=c.border,
                        focusthickness=0,
                        padding=(14, 6),
                        font=('Segoe UI', 10))
        style.map('Secondary.TButton',
                  background=[('active', c.table_hover)])

        # 工具按钮
        style.configure('Tool.TButton',
                        background=c.bg,
                        foreground=c.text_secondary,
                        borderwidth=1,
                        bordercolor=c.border,
                        focusthickness=0,
                        padding=(10, 4),
                        font=('Segoe UI', 10))
        style.map('Tool.TButton',
                  background=[('active', c.table_hover)],
                  foreground=[('active', c.text)])

        # 进度条
        style.configure('TProgressbar',
                        background=c.accent,
                        troughcolor=c.table_alt,
                        bordercolor=c.border,
                        lightcolor=c.accent,
                        darkcolor=c.accent,
                        thickness=6)

        # Treeview 整体
        style.configure('Treeview',
                        background=c.surface,
                        foreground=c.text,
                        fieldbackground=c.grid,
                        bordercolor=c.border,
                        rowheight=32,
                        padding=1,
                        font=('Segoe UI', 12))
        style.map('Treeview',
                  background=[('selected', c.table_selected)],
                  foreground=[('selected', c.text)])

        # Treeview 表头
        style.configure('Treeview.Heading',
                        background=c.header_bg,
                        foreground=c.text_secondary,
                        font=('Segoe UI', 10, 'bold'),
                        borderwidth=1,
                        relief='solid')
        style.map('Treeview.Heading',
                  background=[('active', c.table_hover)],
                  foreground=[('active', c.text)])

        # Treeitem 单元格：注入 1px 右边框实线
        try:
            style.layout('Treeview.Item', [
                ('Treeitem.padding', {'children': [
                    ('Treeitem.border', {'children': [
                        ('Treeitem.focus', {'children': [
                            ('Treeitem.text', {'sticky': 'nswe'})
                        ], 'sticky': 'nswe'})
                    ], 'sticky': 'nswe', 'border': '1', 'relief': 'solid'})
                ], 'sticky': 'nswe'})
            ])
        except tk.TclError:
            pass

        # 滚动条
        style.configure('TScrollbar',
                        background=c.scrollbar,
                        troughcolor=c.bg,
                        bordercolor=c.bg,
                        arrowcolor=c.text_secondary,
                        gripcount=0,
                        arrowsize=14)
        style.map('TScrollbar',
                  background=[('active', c.text_secondary)])

        # 分隔线
        style.configure('TSeparator', background=c.border)

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
                self.tmdb_status.config(text='🔑 已设置', fg=Theme.success)
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
        dlg.title("TMDB 搜索剧集")
        dlg.geometry("580x520")
        dlg.configure(bg=Theme.bg)
        dlg.transient(self.root)
        dlg.grab_set()

        # 搜索栏
        search_frame = tk.Frame(dlg, bg=Theme.bg, padx=12, pady=12)
        search_frame.pack(fill=tk.X)

        tk.Label(search_frame, text='搜索剧名：', font=('Segoe UI', 12),
                 bg=Theme.bg, fg=Theme.text).pack(side=tk.LEFT)

        # 智能提取标题
        folder_name = os.path.basename(os.path.normpath(self.folder_path.get())) or ''
        clean_title = extract_clean_title(folder_name)
        query_var = tk.StringVar(value=clean_title)
        query_entry = ttk.Entry(search_frame, textvariable=query_var, width=30,
                                font=('Segoe UI', 12))
        query_entry.pack(side=tk.LEFT, padx=6)
        query_entry.select_range(0, tk.END)
        query_entry.focus_set()

        # 显示原始文件夹名供参考
        if clean_title != folder_name:
            hint_frame = tk.Frame(dlg, bg=Theme.bg)
            hint_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
            tk.Label(hint_frame, text=f'原始：{folder_name}',
                     font=('Segoe UI', 9), fg=Theme.text_secondary, bg=Theme.bg,
                     wraplength=540).pack(anchor='w')

        # 结果列表
        list_frame = tk.Frame(dlg, bg=Theme.bg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12)

        columns = ('name', 'year', 'lang')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings',
                            selectmode='browse', height=8)
        tree.heading('name', text='剧名')
        tree.heading('year', text='首播年份')
        tree.heading('lang', text='语言')
        tree.column('name', width=340)
        tree.column('year', width=80, anchor='center')
        tree.column('lang', width=60, anchor='center')
        tree.pack(fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮
        btn_frame = tk.Frame(dlg, bg=Theme.bg, padx=12, pady=12)
        btn_frame.pack(fill=tk.X)

        result_label = tk.Label(btn_frame, text='', bg=Theme.bg, fg=Theme.text_secondary,
                                font=('Segoe UI', 10))
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
        ttk.Button(search_frame, text='搜索', command=do_search,
                   style='Primary.TButton').pack(side=tk.LEFT, padx=4)

        ttk.Button(btn_frame, text='确认选择', command=on_select,
                   style='Accent.TButton').pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text='取消', command=dlg.destroy,
                   style='Secondary.TButton').pack(side=tk.RIGHT, padx=4)

        # 自动搜索
        if folder_name:
            dlg.after(200, do_search)

    def _apply_season_offset(self):
        """检测跨季全局序号并自动偏移为季内从1开始。"""
        if getattr(self, '_renaming', False):
            return  # 重命名中不动数据

        season_set = set(e.season for e in self.entries)
        if len(season_set) <= 1:
            return

        sorted_seasons = sorted(season_set)

        # 计算每季累积偏移：优先用 TMDB，否则用本季文件数
        offsets = {}
        cumulative = 0
        for s in sorted_seasons:
            offsets[s] = cumulative
            if self.tmdb_episodes and s in self.tmdb_episodes:
                cumulative += len(self.tmdb_episodes[s])
            else:
                # 无 TMDB：用本季文件数作为"该季应有集数"的估算
                count_in_season = sum(1 for e in self.entries if e.season == s)
                cumulative += count_in_season

        changed = False
        for e in self.entries:
            if e.season in offsets and e.episode:
                try:
                    ep_num = int(e.episode)
                    offset = offsets[e.season]
                    if offset > 0 and ep_num > offset:
                        e.episode = str(ep_num - offset)
                        changed = True
                except ValueError:
                    pass

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
            "1. 点击「浏览」选择动漫根文件夹（如 海贼王/）\n"
            "2. 点击「扫描」自动识别分季子文件夹和集号\n"
            "3. 在表格中审核结果：\n"
            "   - 单击 ☑ 列勾选/取消文件\n"
            "   - 双击「季」或「集」列编辑序号\n"
            "   - ⚠️ 黄底行表示集号 > 500 需要确认\n"
            "4. 点击「执行重命名」批量重命名\n\n"
            "命名格式：S{季号}E{集号}_{原文件名}.ext\n"
            "字幕文件会自动同步重命名。"
        )
        messagebox.showinfo("使用说明", text)

    def _show_about(self):
        text = (
            "AniPrep 动漫媒体文件规范化重命名工具\n"
            "版本 1.5\n\n"
            "将动漫视频及外挂字幕重命名为\n"
            "Emby/Jellyfin 友好格式\n\n"
            "• 支持 TMDB 自动匹配季集信息\n"
            "• 支持 S0 (特典/Specials) 季号\n"
            "• 跨季集号自动偏移\n"
            "• 文件夹同步重命名"
        )
        messagebox.showinfo("关于", text)


# ============================================================================
#  入口
# ============================================================================

def main():
    root = tk.Tk()
    app = AniPrepApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
