# AniPrep — 动漫媒体文件规范化重命名工具

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

将动漫视频文件及外挂字幕一键重命名为 **Emby / Jellyfin** 标准格式。

```
扫描前：S1/One Piece - 01.mkv
扫描后：S01E01_One Piece - 01.mkv
```

---

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| **智能扫描** | 自动识别 `S1`/`Season 1`/`第1季` 等分季文件夹 |
| **集号提取** | 支持 `[01]`、`EP01`、`第01话`、`SP01`、`- 01` 等 7 种命名格式 |
| **字幕同步** | 自动匹配 `.ass`/`.srt`/`.ssa`/`.sub`/`.vtt` 等字幕文件并同步重命名 |
| **TMDB 集成** | 自动匹配 TMDB 剧集信息，获取官方集名和分季数据 |
| **跨季偏移** | 检测全局顺序编号（S2/13~24 → S2 E01~E12），自动按季重置集号 |
| **文件夹重命名** | 可选同步重命名根目录为 `{剧名} ({年份})`，分季目录为 `Season XX` |
| **双扩展名字幕** | 支持 `.sc.ass` / `.tc.ass`（简繁外挂）等双扩展名格式 |
| **冲突检测** | 自动检测命名冲突并高亮警告 |
| **路径长度检查** | Windows 路径过长时自动提醒 |

---

## 📸 界面预览

```
┌─────────────────────────────────────────────────────────┐
│  AniPrep  动漫媒体规范化重命名                              │
│─────────────────────────────────────────────────────────│
│  文件夹: [________________________] [浏览] [扫描]  季号: 01│
│  [🔍 TMDB 搜索]  已匹配: One Piece (1999)                  │
│                          ☑ 同步重命名文件夹  [全选][取消][⚠]│
│─────────────────────────────────────────────────────────│
│  ☑ │ 季 │ 集 │ 原文件名          │ 新文件名           │TMDB│
│  ● │ 01 │ 01 │ One Piece - 01.mkv│ S01E01_One Piece.. │冒险│
│  └ │    │    │   xxx.sc.ass     │ S01E01_xxx.sc.ass │    │
│  ○ │ 02 │ 01 │ One Piece - 13.mkv│ S02E01_One Piece.. │登陆│
│─────────────────────────────────────────────────────────│
│  📄 12  📝 24  ⚠️ 0  ❌ 0      共 12 视频 · 24 字幕   [执行]│
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 方式一：直接运行 EXE（推荐）

下载 `AniPrep.exe`，双击运行。**无需安装 Python**。

### 方式二：源码运行

```bash
# 1. 克隆仓库
git clone https://github.com/yourname/AniPrep.git
cd AniPrep

# 2. 安装依赖（TMDB 功能需要）
pip install requests

# 3. 运行
python AniPrep.py
```

---

## 🔑 TMDB API Key（可选）

TMDB 自动匹配功能需要一个**免费的** API Key：

1. 访问 [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
2. 注册账号（免费）
3. 申请 API Key（选择 Developer）
4. 在 AniPrep 菜单栏中：**设置 → 🔑 TMDB API Key 设置...** 填入 Key

> **不填 API Key 也不影响本地核心功能**（扫描、重命名、字幕同步都正常使用）

---

## 📋 支持的命名格式

### 分季文件夹
| 格式 | 示例 |
|------|------|
| S{数字} | `S1`, `S01`, `S0` |
| Season {数字} | `Season 1`, `season 01` |
| 第{数字}季 | `第1季`, `第 2 季` |

### 集号提取
| 格式 | 示例 | 优先级 |
|------|------|--------|
| `[数字]` | `[01]`, `[720]` | 最高 |
| `第{数字}话` | `第01话`, `第720話` | |
| `EP{数字}` | `EP01`, `Ep001` | |
| `SP{数字}` | `SP01`, `sp_02`, `SP 03` | ← 特典/S0 专用 |
| `- 数字` | `- 01` | |
| `.数字.ext` | `.001.mkv` | |
| `_数字` | `_001` | 最低 |

### 输出格式

```
S{季号2位}E{集号2-3位}_{原文件名}.{扩展名}

示例：
  One Piece - 01.mkv  →  S01E01_One Piece - 01.mkv
  MyAnime_SP01.mkv    →  S00E01_MyAnime_SP01.mkv
```

---

## 🗂️ 项目结构

```
AniPrep/
├── AniPrep.py                  # 主程序（单文件，约 2000 行）
├── aniprep_config.json         # 配置文件（自动生成）
├── README.md                   # 本文件
├── LICENSE                     # 开源协议
└── dist/
    └── AniPrep.exe             # 打包好的可执行文件
```

---

## ⚙️ 配置文件

`aniprep_config.json`（自动生成于程序同目录）：

```json
{
  "tmdb_api_key": "",
  "tmdb_language": "zh-CN",
  "last_root_folder": "",
  "rename_folders": false,
  "window_geometry": "1200x760"
}
```

---

## 🔧 从源码打包

```bash
pip install pyinstaller
cd AniPrep
pyinstaller --onefile --windowed --name AniPrep AniPrep.py
# 输出在 dist/AniPrep.exe
```

---

## 📝 使用流程

```
1. 点击「浏览」选择动漫根文件夹（或直接在路径框输入）
   ↓
2. 点击「扫描」→ 自动识别分季文件夹 + 提取集号
   ↓
3. （可选）菜单设置 → 填入 TMDB API Key → 自动匹配剧集信息
   ↓
4. 在表格中审核结果：
   - 单击 ●/○ 勾选/取消文件
   - 双击「季」或「集」列直接编辑
   - ⚠️ 标记表示可疑集号
   ↓
5. （可选）勾选「同步重命名文件夹」
   ↓
6. 点击「执行重命名」→ 完成！
```

---

## 🐛 常见问题

| 问题 | 解决方法 |
|------|----------|
| 字幕文件没被识别 | 确认字幕与视频**同目录**且**主文件名一致**，支持 `.sc.ass` 双扩展名 |
| SP 特典集号全是 S00E00 | 文件名需包含 `SP01` / `sp_02` 等明确标记 |
| 第二季集号从 13 开始 | 启用 TMDB 后自动偏移，或手动双击集号列修改 |
| TMDB 搜索卡住 | 网络问题或 API Key 无效，不影响本地功能 |
| 重命名后 Emby 没识别 | 检查文件夹结构：根/剧名 (年份)/Season 01/S01E01_xxx.mkv |

---

## 📄 License

MIT © 2025

---

## 🙏 致谢

- [The Movie Database (TMDB)](https://www.themoviedb.org/) — 提供免费 API
- [Emby](https://emby.media/) / [Jellyfin](https://jellyfin.org/) — 命名规范参考
