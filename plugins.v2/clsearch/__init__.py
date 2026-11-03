"""
观影磁力搜插件

搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。
支持Cookie认证和账号密码自动登录，自动解析搜索结果和磁力链接。
"""

import json
import hashlib
import os
import re
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse, parse_qs, unquote

import requests
try:
    from p115client import P115Client, P115OpenClient
except ImportError as e:
    P115Client = None
    P115OpenClient = None
    P115_IMPORT_ERROR = e
else:
    P115_IMPORT_ERROR = None

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MessageChannel

from .agent_tools import (
    ClSearchSearchTool,
    ClSearchDetailTool,
    ClSearchOfflineTool,
    ClSearchOfflineResultTool,
    ClSearchRenameTool,
    ClSearchCompleteTaskTool,
    ClSearchMoveFileTool,
    ClSearchListOfflineTool,
    ClSearchOrganizeOfflineTool,
)
from .organizer import OfflineOrganizer


_CLSEARCH_ACTIVE_PLUGIN = None


class ClSearch(_PluginBase):
    """观影磁力搜插件"""

    # 插件名称
    plugin_name = "观影磁力搜"
    # 插件描述
    plugin_desc = "搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    # 插件版本
    plugin_version = "1.6.1"
    # 插件作者
    plugin_author = "chaomarks"
    # 作者主页
    author_url = "https://github.com/chaomarks/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "clsearch_"
    # 加载顺序
    plugin_order = 50
    # 可使用的用户级别
    auth_level = 1

    # 插件属性
    _enabled = False
    _site_url = ""
    _site_cookie = ""
    _site_username = ""
    _site_password = ""
    _p115_cookie = ""
    _save_dir_id = ""
    _resolved_path = ""  # CID 解析出的完整路径
    _auto_transfer = False  # 离线下载完成后自动整理
    _ad_video_max_size_mb = 300  # 小于该大小的无法识别视频才自动当广告删除

    # 运行参数
    _request_timeout = 30
    _poll_interval = 30
    _idle_poll_interval = 60
    _offline_task_timeout = 86400
    _search_cache_limit = 100

    # 搜索 & 离线历史记录
    _search_history: List[Dict[str, Any]] = []
    _offline_history: List[Dict[str, Any]] = []

    # 搜索结果缓存
    _search_cache: Dict[str, Any] = {}
    # 登录会话（账号密码模式下保持会话）
    _session: Optional[requests.Session] = None
    _login_lock = threading.Lock()

    # 后台轮询相关
    _pending_tasks: Dict[str, dict] = {}  # info_hash -> {name, title, add_time}
    _cancel_flags: Dict[str, bool] = {}  # info_hash -> True 表示任务已取消，需中断整理流程
    _polling_thread: Optional[threading.Thread] = None
    _polling_stop: Optional[threading.Event] = None
    _task_lock = threading.RLock()
    _polling_lock = threading.Lock()
    _offline_pending_data_key = "offline_pending_tasks"
    _search_history_data_key = "search_history"
    _offline_history_data_key = "offline_history"

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件"""
        global _CLSEARCH_ACTIVE_PLUGIN
        if not hasattr(self, "_task_lock") or self._task_lock is None:
            self._task_lock = threading.RLock()
        if not hasattr(self, "_polling_lock") or self._polling_lock is None:
            self._polling_lock = threading.Lock()
        self.stop_service()
        self._search_history = []
        self._offline_history = []
        self._search_cache = {}
        self._pending_tasks = {}
        self._enabled = False
        self._site_url = ""
        self._site_cookie = ""
        self._site_username = ""
        self._site_password = ""
        self._p115_cookie = ""
        self._save_dir_id = ""
        self._resolved_path = ""
        self._session = None
        self._auto_transfer = False
        self._ad_video_max_size_mb = 300

        if not config:
            if _CLSEARCH_ACTIVE_PLUGIN is self:
                _CLSEARCH_ACTIVE_PLUGIN = None
            return

        self._enabled = bool(config.get("enabled"))
        self._site_url = str(config.get("site_url") or "").strip().rstrip("/")
        self._site_username = str(config.get("site_username") or "").strip()
        self._site_password = str(config.get("site_password") or "").strip()
        self._p115_cookie = self._normalize_cookie(config.get("p115_cookie") or "")
        self._save_dir_id = str(config.get("save_dir_id") or "").strip()
        # 从配置中读取解析路径
        self._resolved_path = str(config.get("resolved_path") or "")
        self._auto_transfer = bool(config.get("auto_transfer"))
        self._ad_video_max_size_mb = self._safe_int(config.get("ad_video_max_size_mb", 300), 300, minimum=0, maximum=10240)
        self._config = dict(config)
        _CLSEARCH_ACTIVE_PLUGIN = self

        # 恢复仍在等待115完成的离线任务；待智能体整理记录不再混入轮询队列。
        self._reload_history_from_storage()
        self._pending_tasks = self._load_offline_pending_from_storage()
        if self._enabled:
            self._start_polling()

        # CID 变更时自动解析路径并写回配置
        if self._save_dir_id and self._p115_cookie and not self._resolved_path:
            try:
                resolved = self._resolve_cid_path(self._save_dir_id)
                if resolved:
                    self._resolved_path = resolved
                    logger.info(f"CID {self._save_dir_id} 自动解析为: {resolved}")
                    config["resolved_path"] = resolved
                    self.update_config(config)
            except Exception as e:
                logger.warning(f"CID路径自动解析失败: {e}")

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表"""
        return [
            {
                "cmd": "/clsearch",
                "event": EventType.PluginAction,
                "desc": "观影搜",
                "category": "观影搜",
                "data": {
                    "action": "clsearch",
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件API列表"""
        return [
            {
                "path": "/ClSearch/search",
                "endpoint": self._api_search,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "搜索磁力资源",
            },
            {
                "path": "/ClSearch/detail",
                "endpoint": self._api_detail,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取资源详情和磁力链接",
            },
            {
                "path": "/ClSearch/offline",
                "endpoint": self._api_offline_download,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "添加115离线下载",
            },
            {
                "path": "/ClSearch/login",
                "endpoint": self._api_login,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "账号密码登录站点",
            },
            {
                "path": "/ClSearch/resolve_cid",
                "endpoint": self._api_resolve_cid,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "通过CID解析115目录完整路径",
            },
            {
                "path": "/ClSearch/rename",
                "endpoint": self._api_recursive_rename,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "递归重命名115目录内媒体文件",
            },
            {
                "path": "/ClSearch/offline/list",
                "endpoint": self._api_list_offline,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "列出115离线下载目录内容",
            },
            {
                "path": "/ClSearch/organize",
                "endpoint": self._api_organize_offline,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "整理115离线目录中的指定文件夹",
            },
            {
                "path": "/ClSearch/move",
                "endpoint": self._api_move_offline,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "移动115文件夹到入库目录",
            },
            {
                "path": "/ClSearch/task/close",
                "endpoint": self._api_close_task,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "关闭插件首页任务记录",
            },
            {
                "path": "/ClSearch/history/clear",
                "endpoint": self._api_clear_history,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "清空搜索或离线历史记录",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置"""
        return [
            {
                "component": "VForm",
                "props": {"model": "form"},
                "content": [
                    # ========== 顶部开关 ==========
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_transfer",
                                            "label": "自动整理（115离线下载完成时自动触发重命名和整理入库）",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ========== 说明提示 ==========
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                            "class": "mt-2",
                                        },
                                        "text": "（此插件仅通过智能体工具调用，且需登录MP内置115存储）",
                                    }
                                ],
                            },
                        ],
                    },
                    # ========== 分隔标题：网站配置 ==========
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "props": {"class": "text-subtitle-2 font-weight-bold mt-4 mb-1"},
                                        "text": "网站配置",
                                    }
                                ],
                            },
                        ],
                    },
                    # 站点地址 + 用户名 + 密码
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_url",
                                            "label": "站点地址",
                                            "placeholder": "https://www.example.com",
                                            "hint": "资源站点URL，不含末尾斜杠",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_username",
                                            "label": "用户名",
                                            "placeholder": "站点登录用户名",
                                            "hint": "支持用户名或邮箱登录",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_password",
                                            "label": "密码",
                                            "placeholder": "站点登录密码",
                                            "type": "password",
                                            "hint": "密码保存在本地配置中",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ========== 分隔线 ==========
                    {
                        "component": "VDivider",
                        "props": {"class": "mt-4 mb-4"},
                    },
                    # ========== 分隔标题：115网盘配置 ==========
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "props": {"class": "text-subtitle-2 font-weight-bold mb-1"},
                                        "text": "115网盘配置",
                                    }
                                ],
                            },
                        ],
                    },
                    # 115网盘Cookie（独占一行）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "p115_cookie",
                                            "label": "115网盘Cookie",
                                            "placeholder": "粘贴115网盘Cookie",
                                            "hint": "用于115离线下载的认证Cookie",
                                            "persistent-hint": True,
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 目录CID + 解析路径 + 广告阈值（同一行）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "save_dir_id",
                                            "label": "目录CID",
                                            "placeholder": "例如：2835669123456789",
                                            "hint": "从115网盘地址栏获取19位数字，保存后自动解析完整路径",
                                            "persistent-hint": True,
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "resolved_path",
                                            "label": "解析路径",
                                            "placeholder": "例如：/影视/电视剧",
                                            "variant": "outlined",
                                            "density": "compact",
                                            "hint": "保存后自动解析CID对应的完整路径，也可手动输入",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ad_video_max_size_mb",
                                            "label": "广告视频删除阈值(MB)",
                                            "placeholder": "300",
                                            "type": "number",
                                            "hint": "无法识别剧集信息的视频小于该大小才自动删除；0表示不自动删除视频",
                                            "persistent-hint": True,
                                            "density": "compact",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ========== 分隔线 ==========
                    {
                        "component": "VDivider",
                        "props": {"class": "mt-4 mb-4"},
                    },
                ],
            }
        ], {
            "enabled": False,
            "site_url": "",
            "site_username": "",
            "site_password": "",
            "p115_cookie": "",
            "save_dir_id": "",
            "resolved_path": "",
            "auto_transfer": False,
            "ad_video_max_size_mb": 300,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页，展示搜索和离线历史记录"""
        self._reload_history_from_storage()
        self._merge_offline_pending_from_storage()
        search_items = self._search_history[:20]
        offline_items = self._offline_history[:20]
        token = self._get_mp_api_token()
        with self._task_lock:
            pending_snapshot = list(self._pending_tasks.items())
        offline_tasks = [
            {
                "title": t.get("title") or t.get("name") or key,
                "status": t.get("status") or "离线监控中",
                "path": t.get("path") or "",
                "updated_at": t.get("updated_at") or "",
                "action": f"/api/v1/plugin/ClSearch/ClSearch/task/close?type=offline&key={quote(key)}",
            }
            for key, t in pending_snapshot
        ]
        transfer_tasks = [
            {
                "title": t.get("task_name") or t.get("title") or t.get("file_path") or "",
                "status": "重命名失败" if t.get("rename_failed") else ("待智能体整理" if t.get("needs_agent_transfer") else "待处理"),
                "path": t.get("file_path") or "",
                "updated_at": t.get("created_at") or "",
                "action": f"/api/v1/plugin/ClSearch/ClSearch/task/close?type=transfer&key={idx}",
            }
            for idx, t in enumerate(self.get_data("pending_transfer_tasks") or [])
        ]
        all_tasks_close_url = "/api/v1/plugin/ClSearch/ClSearch/task/close?type=all&key=all"

        def _close_href(url: str, remove_all: bool = False) -> str:
            # 后台请求删除，成功后直接移除当前任务行并同步计数，不刷新页面。
            token_param = json.dumps(token or "", ensure_ascii=False)
            url_param = json.dumps(url, ensure_ascii=False)
            remove_js = "document.querySelectorAll('[data-clsearch-task-row=\"1\"]').forEach(e=>e.remove());" if remove_all else "if(row)row.remove();"
            return (
                "javascript:void(async()=>{"
                "const b=document.activeElement;"
                "const row=b&&b.closest('[data-clsearch-task-row=\"1\"]');"
                f"const u={url_param};const tk={token_param};"
                "const api=u+(u.includes('?')?'&':'?')+'token='+encodeURIComponent(tk);"
                "try{const r=await fetch(api,{credentials:'include'});"
                "const d=await r.json().catch(()=>({success:r.ok}));"
                "if(d&&d.success){"
                + remove_js
                + "const c=document.querySelector('[data-clsearch-task-count=\"1\"]');"
                "if(c){const n=document.querySelectorAll('[data-clsearch-task-row=\"1\"]').length;c.textContent='当前任务（'+n+'条）';}"
                + "}else{alert((d&&d.message)||'关闭任务失败');}"
                "}catch(e){alert('关闭任务失败：'+e.message);}})()"
            )

        def _clear_history_href(history_type: str) -> str:
            token_param = json.dumps(token or "", ensure_ascii=False)
            url = f"/api/v1/plugin/ClSearch/ClSearch/history/clear?type={history_type}"
            url_param = json.dumps(url, ensure_ascii=False)
            row_attr = "data-clsearch-search-row" if history_type == "search" else "data-clsearch-offline-row"
            count_attr = "data-clsearch-search-count" if history_type == "search" else "data-clsearch-offline-count"
            stat_attr = "data-clsearch-search-stat" if history_type == "search" else "data-clsearch-offline-stat"
            empty_text = "暂无搜索记录，使用 /clsearch 关键词 开始搜索" if history_type == "search" else "暂无离线下载记录"
            return (
                "javascript:void(async()=>{"
                f"const u={url_param};const tk={token_param};"
                "const api=u+(u.includes('?')?'&':'?')+'token='+encodeURIComponent(tk);"
                "try{const r=await fetch(api,{credentials:'include'});"
                "const d=await r.json().catch(()=>({success:r.ok}));"
                "if(d&&d.success){"
                f"document.querySelectorAll('[{row_attr}=\"1\"]').forEach(e=>e.remove());"
                f"const c=document.querySelector('[{count_attr}=\"1\"]');"
                "if(c){c.textContent='0条';}"
                f"const s=document.querySelector('[{stat_attr}=\"1\"]');"
                "if(s){s.textContent='0';}"
                f"const card=document.querySelector('[{row_attr}=\"card\"]');"
                f"if(card&&!document.querySelector('[{row_attr}=\"1\"]')){{card.innerHTML='';card.textContent='{empty_text}';}}"
                "}else{alert((d&&d.message)||'清空失败');}"
                "}catch(e){alert('清空失败：'+e.message);}})()"
            )

        def _organize_href() -> str:
            token_param = json.dumps(token or "", ensure_ascii=False)
            url = "/api/v1/plugin/ClSearch/ClSearch/offline/list"
            organize_url = "/api/v1/plugin/ClSearch/ClSearch/organize"
            url_param = json.dumps(url, ensure_ascii=False)
            organize_param = json.dumps(organize_url, ensure_ascii=False)
            return (
                "javascript:void(async()=>{"
                "if(document.querySelector('#clsearch-org-dialog'))return;"
                f"const listUrl={url_param};const orgUrl={organize_param};const tk={token_param};"
                "const overlay=document.createElement('div');overlay.id='clsearch-org-dialog';"
                "overlay.style.cssText='position:fixed;inset:0;z-index:3000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5);backdrop-filter:blur(2px);';"
                "overlay.innerHTML=`"
                "<div style='width:min(520px,92vw);background:rgb(var(--v-theme-surface));color:rgb(var(--v-theme-on-surface));border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.4);overflow:hidden;font-family:inherit;'>"
                "<div style='display:flex;align-items:center;justify-content:space-between;padding:16px 20px 8px;'>"
                "<div style='font-size:1.05rem;font-weight:600;'>离线目录整理</div>"
                "<button id='clsearch-org-close' style='border:none;background:transparent;color:rgb(var(--v-theme-on-surface));opacity:.6;cursor:pointer;font-size:20px;line-height:1;'>&times;</button></div>"
                "<div style='padding:8px 20px;font-size:.8rem;opacity:.7;'>选择离线目录中要整理的文件夹，可同时指定移动目标目录。</div>"
                "<div style='padding:12px 20px;'>"
                "<div style='margin-bottom:12px;'><div style='font-size:.75rem;opacity:.7;margin-bottom:4px;'>离线文件夹</div>"
                "<select id='clsearch-org-folder' style='width:100%;padding:10px 12px;border-radius:8px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));background:rgb(var(--v-theme-surface));color:inherit;font-size:.9rem;'><option value=''>加载中...</option></select></div>"
                "<div><div style='font-size:.75rem;opacity:.7;margin-bottom:4px;'>目标目录（可选，整理后移动入库）</div>"
                "<input id='clsearch-org-target' placeholder='如 /媒体库/电影，留空则稍后由智能体处理' style='width:100%;padding:10px 12px;border-radius:8px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));background:rgb(var(--v-theme-surface));color:inherit;font-size:.9rem;'/></div></div>"
                "<div id='clsearch-org-msg' style='padding:0 20px;font-size:.8rem;min-height:18px;color:rgb(var(--v-theme-error));'></div>"
                "<div style='display:flex;justify-content:flex-end;gap:8px;padding:12px 20px 16px;'>"
                "<button id='clsearch-org-cancel' style='padding:8px 16px;border-radius:8px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));background:transparent;color:inherit;cursor:pointer;font-size:.875rem;'>取消</button>"
                "<button id='clsearch-org-ok' style='padding:8px 20px;border-radius:8px;border:none;background:rgb(var(--v-theme-primary));color:#fff;cursor:pointer;font-size:.875rem;'>整理</button></div>"
                "</div>`;"
                "document.body.appendChild(overlay);"
                "const close=()=>overlay.remove();"
                "overlay.addEventListener('click',e=>{if(e.target===overlay)close();});"
                "overlay.querySelector('#clsearch-org-close').onclick=close;"
                "overlay.querySelector('#clsearch-org-cancel').onclick=close;"
                "const sel=overlay.querySelector('#clsearch-org-folder');"
                "const msg=overlay.querySelector('#clsearch-org-msg');"
                "const fetchOpts={credentials:'include'};"
                "try{"
                "const r=await fetch(listUrl,{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword:''})});"
                "const d=await r.json().catch(()=>({success:r.ok}));"
                "const items=(d&&(d.data||d.value&&d.value.data))||[];"
                "if(!d.success&&!items.length){msg.textContent=d.message||'读取离线目录失败';sel.innerHTML='<option value=\"\">加载失败</option>';}"
                "else if(!items.length){msg.textContent='离线目录为空';sel.innerHTML='<option value=\"\">（空）</option>';}"
                "else{sel.innerHTML=items.map(x=>`<option value=\"${x.cid}\">${x.name}${x.type==='dir'?'':'（文件）'}</option>`).join('');}"
                "}catch(e){msg.textContent='读取离线目录失败：'+e.message;}"
                "overlay.querySelector('#clsearch-org-ok').onclick=async()=>{"
                "const cid=sel.value;const opt=sel.selectedIndex>=0?sel.options[sel.selectedIndex].text:'';"
                "if(!cid){msg.textContent='请选择要整理的文件夹';return;}"
                "const target=overlay.querySelector('#clsearch-org-target').value.trim();"
                "const btn=overlay.querySelector('#clsearch-org-ok');btn.disabled=true;btn.textContent='整理中...';msg.textContent='';"
                "try{"
                "const body={cid:cid,folder_name:opt,target_path:target};"
                "const r2=await fetch(orgUrl,Object.assign({method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},{}));"
                "const d2=await r2.json().catch(()=>({success:r2.ok}));"
                "if(d2&&d2.success){btn.textContent='✓ '+(d2.message||'整理完成');setTimeout(()=>{close();location.reload();},800);}"
                "else{msg.textContent=(d2&&d2.message)||'整理失败';}"
                "}catch(e){msg.textContent='整理失败：'+e.message;}"
                "finally{btn.disabled=false;btn.textContent='整理';}};}})()"
            )

        return [
            {
                "component": "div",
                "props": {"class": "pa-2"},
                "content": [
                    # 标题
                    {
                        "component": "div",
                        "props": {"class": "text-h6 font-weight-bold mb-2"},
                        "text": "观影磁力搜",
                    },
                    # 统计卡片
                    {
                        "component": "VRow",
                        "props": {"class": "mb-4"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 6},
                                "content": [{
                                    "component": "VCard",
                                    "props": {"variant": "tonal", "color": "primary"},
                                    "content": [{
                                        "component": "VCardText",
                                        "props": {"class": "pa-3 text-center"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold", "data-clsearch-search-stat": "1"}, "text": str(len(search_items))},
                                            {"component": "div", "props": {"class": "text-caption"}, "text": "搜索记录"},
                                        ],
                                    }],
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 6},
                                "content": [{
                                    "component": "VCard",
                                    "props": {"variant": "tonal", "color": "success"},
                                    "content": [{
                                        "component": "VCardText",
                                        "props": {"class": "pa-3 text-center"},
                                        "content": [
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold", "data-clsearch-offline-stat": "1"}, "text": str(sum(1 for r in offline_items if r.get("success")))},
                                            {"component": "div", "props": {"class": "text-caption"}, "text": "成功离线"},
                                        ],
                                    }],
                                }],
                            },
                        ],
                    },
                    # 当前任务
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1 d-flex align-center justify-space-between"},
                                "content": [
                                    {"component": "span", "props": {"data-clsearch-task-count": "1"}, "text": f"当前任务（{len(offline_tasks) + len(transfer_tasks)}条）"},
                                    {
                                        "component": "VBtn",
                                        "props": {"variant": "text", "size": "small", "color": "error", "href": _close_href(all_tasks_close_url, remove_all=True)},
                                        "text": "清空任务",
                                    },
                                ],
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-2"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {"class": "ma-0 py-2 align-center border-b", "data-clsearch-task-row": "1"},
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 10, "class": "py-1"},
                                                "content": [
                                                    {"component": "div", "props": {"class": "font-weight-medium"}, "text": t["title"]},
                                                    {"component": "div", "props": {"class": "text-caption"}, "text": f"{t['status']} | {t.get('path') or '无路径'} | {t.get('updated_at') or ''}"},
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 2, "class": "py-1 text-md-right"},
                                                "content": [{
                                                    "component": "VBtn",
                                                    "props": {"variant": "tonal", "size": "small", "color": "error", "href": _close_href(t["action"])},
                                                    "text": "关闭",
                                                }],
                                            },
                                        ],
                                    }
                                    for t in (offline_tasks + transfer_tasks)
                                ] if (offline_tasks or transfer_tasks) else [{
                                    "component": "VCardText",
                                    "text": "暂无当前任务",
                                }],
                            },
                        ],
                    },
                    # 离线目录整理
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1 d-flex align-center justify-space-between"},
                                "content": [
                                    {"component": "span", "text": "离线目录整理"},
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "variant": "tonal",
                                            "size": "small",
                                            "color": "primary",
                                            "href": _organize_href(),
                                        },
                                        "text": "整理",
                                    },
                                ],
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-2 text-caption"},
                                "text": "可直接整理手动添加到115离线下载的文件；文件夹名留空时会先列出离线目录。",
                            },
                        ],
                    },
                    # 搜索历史
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1 d-flex align-center justify-space-between"},
                                "content": [
                                    {"component": "span", "props": {"data-clsearch-search-count": "1"}, "text": f"搜索记录（{len(search_items)}条）"},
                                    {
                                        "component": "VBtn",
                                        "props": {"variant": "text", "size": "small", "color": "error", "href": _clear_history_href("search")},
                                        "text": "清空记录",
                                    },
                                ],
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-2", "data-clsearch-search-row": "card"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {"class": "ma-0 py-2 align-center border-b", "data-clsearch-search-row": "1"},
                                        "content": [
                                            {"component": "VCol", "props": {"cols": 7, "class": "py-1"}, "content": [{"component": "div", "props": {"class": "font-weight-medium text-truncate"}, "text": item["keyword"]}]},
                                            {"component": "VCol", "props": {"cols": 2, "class": "py-1 text-center"}, "content": [{"component": "div", "text": f"{item['count']}条"}]},
                                            {"component": "VCol", "props": {"cols": 3, "class": "py-1 text-right"}, "content": [{"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": item["time"]}]},
                                        ],
                                    }
                                    for item in search_items
                                ] if search_items else [{
                                    "component": "VCardText",
                                    "text": "暂无搜索记录，使用 /clsearch 关键词 开始搜索",
                                }],
                            },
                        ],
                    },
                    # 离线下载历史
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1 d-flex align-center justify-space-between"},
                                "content": [
                                    {"component": "span", "props": {"data-clsearch-offline-count": "1"}, "text": f"离线记录（{len(offline_items)}条）"},
                                    {
                                        "component": "VBtn",
                                        "props": {"variant": "text", "size": "small", "color": "error", "href": _clear_history_href("offline")},
                                        "text": "清空记录",
                                    },
                                ],
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-2", "data-clsearch-offline-row": "card"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {"class": "ma-0 py-2 align-center border-b", "data-clsearch-offline-row": "1"},
                                        "content": [
                                            {"component": "VCol", "props": {"cols": 7, "class": "py-1"}, "content": [{"component": "div", "props": {"class": "font-weight-medium text-truncate"}, "text": r["title"]}]},
                                            {"component": "VCol", "props": {"cols": 2, "class": "py-1 text-center"}, "content": [{"component": "div", "text": "✅" if r.get("success") else "❌"}]},
                                            {"component": "VCol", "props": {"cols": 3, "class": "py-1 text-right"}, "content": [{"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": r["time"]}]},
                                        ],
                                    }
                                    for r in offline_items
                                ] if offline_items else [{
                                    "component": "VCardText",
                                    "text": "暂无离线下载记录",
                                }],
                            },
                        ],
                    },
                ],
            }
        ]

    # ==================== PoW 验证 ====================

    def _solve_pow(self, session: requests.Session) -> bool:
        """解决 PoW（工作量证明）挑战，获取 browser_verified Cookie

        Args:
            session: 当前请求会话

        Returns:
            True表示成功，False表示失败
        """
        try:
            # 获取 PoW 挑战
            chal_resp = session.get(f"{self._site_url}/res/pow", timeout=30)
            if chal_resp.status_code != 200:
                logger.error(f"获取PoW挑战失败: HTTP {chal_resp.status_code}")
                return False

            challenge = chal_resp.json()
            if "error" in challenge:
                logger.error(f"PoW挑战错误: {challenge.get('error')}")
                return False

            N_hex = challenge["N"]
            x_hex = challenge["x"]
            t = int(challenge["t"])

            logger.info(f"开始PoW计算: t={t} 次迭代...")

            # 计算 y = x^(2^t) mod N
            bigN = int(N_hex, 16)
            y = int(x_hex, 16)
            for i in range(t):
                y = (y * y) % bigN

            y_hex = hex(y)[2:]
            logger.info(f"PoW计算完成")

            # 提交 PoW 解
            verify_resp = session.post(
                f"{self._site_url}/res/pow",
                data={"y": y_hex},
                timeout=30,
            )
            verify_data = verify_resp.json()
            if verify_data.get("success"):
                logger.info("PoW验证通过，已获取 browser_verified")
                return True
            else:
                logger.error(f"PoW提交失败: {verify_data}")
                return False

        except Exception as e:
            logger.error(f"PoW验证异常: {e}")
            return False

    # ==================== 登录相关方法 ====================

    def _site_login(self) -> Tuple[bool, str]:
        """使用账号密码登录站点，含 PoW 验证

        Returns:
            (success, message) 元组
        """
        with self._login_lock:
            if not self._site_url:
                return False, "未配置站点地址"

            if not self._site_username or not self._site_password:
                return False, "未配置站点用户名或密码"

            try:
                # 创建新的Session
                session = requests.Session()
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                })

                # 1. 先访问首页建立 PoW 会话
                session.get(self._site_url, timeout=30)

                # 2. PoW 验证
                if not self._solve_pow(session):
                    return False, "PoW验证失败，站点安全防护拦截"

                # 3. 登录
                login_url = f"{self._site_url}/user/login"
                session.headers.update({
                    "Referer": login_url,
                    "Origin": self._site_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                })

                login_data = {
                    "username": self._site_username,
                    "password": self._site_password,
                    "cookietime": "10506240",
                    "siteid": "1",
                    "dosubmit": "1",
                }

                resp = session.post(
                    login_url,
                    data=login_data,
                    timeout=30,
                    allow_redirects=True,
                )
                resp.raise_for_status()

                try:
                    result = resp.json()
                    code = result.get("code")
                    msg = result.get("msg", "")

                    if code == 200:
                        cookie_dict = session.cookies.get_dict()
                        if cookie_dict:
                            self._session = session
                            self._site_cookie = "; ".join(
                                f"{k}={v}" for k, v in cookie_dict.items()
                            )
                            logger.info(f"站点登录成功，获取到 {len(cookie_dict)} 个Cookie字段")
                            self._persist_site_cookie()
                            return True, "登录成功"
                        else:
                            return False, "登录成功但未获取到Cookie"
                    else:
                        error_msg = msg or f"错误码: {code}"
                        logger.error(f"站点登录失败: {error_msg}")
                        return False, f"登录失败: {error_msg}"

                except (json.JSONDecodeError, ValueError):
                    if resp.history and resp.cookies:
                        cookie_str = "; ".join(
                            f"{k}={v}" for k, v in resp.cookies.items()
                        )
                        self._session = session
                        self._site_cookie = cookie_str
                        logger.info("站点登录成功（通过重定向判断）")
                        self._persist_site_cookie()
                        return True, "登录成功"
                    return False, "无法解析登录响应"

            except requests.RequestException as e:
                logger.error(f"站点登录请求失败: {e}")
                return False, f"登录请求失败: {str(e)}"
            except Exception as e:
                logger.error(f"站点登录异常: {e}")
                return False, f"登录异常: {str(e)}"

    def _ensure_authenticated(self) -> bool:
        """确保站点已认证

        优先使用已有Cookie，Cookie失效则自动用账号密码登录并更新Cookie。

        Returns:
            True表示已认证，False表示认证失败
        """
        # 1. 如果已有Session（账号密码登录成功过），直接使用
        if self._session:
            return True

        # 2. 如果有Cookie，尝试使用Cookie模式
        if self._site_cookie:
            return True

        # 3. 如果有账号密码，自动登录获取Cookie
        if self._site_username and self._site_password:
            success, msg = self._site_login()
            if success:
                logger.info("自动登录成功，Cookie已更新")
                return True
            else:
                logger.error(f"自动登录失败: {msg}")
                return False

        return False

    def _get_headers(self) -> dict:
        """构建带Cookie的请求头"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if self._site_cookie:
            headers["Cookie"] = self._normalize_cookie(self._site_cookie)
        return headers

    @staticmethod
    def _normalize_cookie(cookie: str) -> str:
        """清理复制 Cookie 时带入的首尾空白和换行，避免非法 Header。"""
        if not cookie:
            return ""
        return " ".join(str(cookie).replace("\r", " ").replace("\n", " ").split()).strip()

    @staticmethod
    def _join_u115_path(parent: str, child: str = "", trailing_slash: bool = False) -> str:
        """安全拼接115路径，避免父目录和子文件名直接相连。"""
        parent = str(parent or "").strip().replace("\\", "/")
        child = str(child or "").strip().replace("\\", "/")
        if not parent:
            result = child
        elif not child:
            result = parent
        else:
            result = f"{parent.rstrip('/')}/{child.lstrip('/')}"
        while "//" in result:
            result = result.replace("//", "/")
        if result and not result.startswith("/"):
            result = f"/{result}"
        if trailing_slash and result and not result.endswith("/"):
            result = f"{result}/"
        return result

    @staticmethod
    def _u115_path_name(file_path: str) -> str:
        """从115路径中提取最后一级名称，兼容目录尾部斜杠。"""
        return str(file_path or "").strip().replace("\\", "/").rstrip("/").split("/")[-1]

    @staticmethod
    def _fileitem_path(fileitem: Any, parent_path: str = "") -> str:
        """Return a usable full path for a FileItem-like object."""
        raw_path = str(getattr(fileitem, "path", "") or "").strip().replace("\\", "/")
        name = str(getattr(fileitem, "name", "") or "").strip()
        if raw_path and (not name or raw_path.rstrip("/").endswith(f"/{name}") or raw_path.rstrip("/") == name):
            return raw_path
        return ClSearch._join_u115_path(parent_path, name)

    def _persist_site_cookie(self) -> None:
        """将自动登录获得的 Cookie 写回配置，避免重复登录。"""
        if not self._site_cookie:
            return
        try:
            config = {
                "enabled": self._enabled,
                "site_url": self._site_url,
                "site_username": self._site_username,
                "site_password": self._site_password,
                "p115_cookie": self._p115_cookie,
                "save_dir_id": self._save_dir_id,
                "resolved_path": self._resolved_path,
                "auto_transfer": self._auto_transfer,
                "ad_video_max_size_mb": self._ad_video_max_size_mb,
            }
            if hasattr(self, "_config") and isinstance(getattr(self, "_config"), dict):
                config.update(dict(getattr(self, "_config")))
            config["site_cookie"] = self._site_cookie
            self._config = dict(config)
            self.update_config(config)
        except Exception as persist_e:
            logger.warning(f"Cookie持久化失败: {persist_e}")

    def _safe_int(self, value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        number = max(minimum, number)
        if maximum is not None:
            number = min(number, maximum)
        return number

    def _get_file_size_bytes(self, item: Any) -> int:
        """Best-effort extraction of file size from MoviePilot/115 file items."""
        for attr in ("size", "file_size", "filesize", "fileSize", "fs", "bytes"):
            value = getattr(item, attr, None)
            if value is None and isinstance(item, dict):
                value = item.get(attr)
            size = self._parse_size_bytes(value)
            if size > 0:
                return size
        return 0

    @staticmethod
    def _parse_size_bytes(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return 0
        compact = text.replace(",", "").replace(" ", "")
        try:
            if compact.isdigit():
                return int(compact)
        except Exception:
            pass
        match = re.match(r"(?i)^([0-9]+(?:\.[0-9]+)?)(b|kb|k|mb|m|gb|g|tb|t)?$", compact)
        if not match:
            return 0
        number = float(match.group(1))
        unit = (match.group(2) or "b").lower()
        factor = {
            "b": 1,
            "k": 1024,
            "kb": 1024,
            "m": 1024 ** 2,
            "mb": 1024 ** 2,
            "g": 1024 ** 3,
            "gb": 1024 ** 3,
            "t": 1024 ** 4,
            "tb": 1024 ** 4,
        }.get(unit, 1)
        return int(number * factor)

    def _is_small_ad_video(self, item: Any) -> Tuple[bool, int]:
        size_bytes = self._get_file_size_bytes(item)
        threshold_mb = self._safe_int(self._ad_video_max_size_mb, 300, minimum=0, maximum=10240)
        if threshold_mb <= 0 or size_bytes <= 0:
            return False, size_bytes
        return size_bytes <= threshold_mb * 1024 * 1024, size_bytes

    @staticmethod
    def _format_size_text(size_bytes: int) -> str:
        if size_bytes <= 0:
            return "大小未知"
        if size_bytes >= 1024 ** 3:
            return f"{size_bytes / 1024 / 1024 / 1024:.2f}GB"
        if size_bytes >= 1024 ** 2:
            return f"{size_bytes / 1024 / 1024:.1f}MB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes}B"

    def _cache_search_results(self, cache_key: str, results: List[dict]) -> None:
        self._search_cache[cache_key] = results
        while len(self._search_cache) > self._search_cache_limit:
            oldest_key = next(iter(self._search_cache), None)
            if oldest_key is None:
                break
            self._search_cache.pop(oldest_key, None)

    def _do_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发起HTTP请求（简单版，不处理PoW）"""
        kwargs.setdefault("timeout", 30)

        if self._session:
            return self._session.request(method, url, **kwargs)
        else:
            headers = kwargs.pop("headers", {})
            merged_headers = self._get_headers()
            merged_headers.update(headers)
            kwargs["headers"] = merged_headers
            return requests.request(method, url, **kwargs)

    def _request_with_pow(self, method: str, url: str, **kwargs) -> requests.Response:
        """发起请求，自动处理 PoW 拦截。"""
        kwargs.setdefault("timeout", self._request_timeout)
        kwargs.setdefault("allow_redirects", True)

        def _send(use_session: bool = True) -> requests.Response:
            request_kwargs = dict(kwargs)
            if use_session and self._session:
                return self._session.request(method, url, **request_kwargs)
            headers = request_kwargs.pop("headers", {})
            merged_headers = self._get_headers()
            merged_headers.update(headers)
            request_kwargs["headers"] = merged_headers
            return requests.request(method, url, **request_kwargs)

        try:
            resp = _send(use_session=True)
        except requests.RequestException as e:
            logger.error(f"站点请求失败: {e}")
            raise

        text = resp.text[:5000] if resp.text else ""
        is_pow_page = ("powSolve" in text and "_obj" in text) or "安全验证" in text
        if is_pow_page:
            logger.info("请求被PoW拦截，尝试解决...")
            try:
                if self._session and self._solve_pow(self._session):
                    return _send(use_session=True)
                if self._site_username and self._site_password:
                    logger.info("Cookie模式受到PoW拦截，尝试用账号密码登录...")
                    success, msg = self._site_login()
                    if success and self._session:
                        logger.info("账号密码登录成功，Cookie已自动更新，使用Session重试")
                        return _send(use_session=True)
                    logger.error(f"账号密码登录失败: {msg}")
                else:
                    with requests.Session() as temp_session:
                        temp_session.headers.update(self._get_headers())
                        temp_session.get(self._site_url, timeout=self._request_timeout)
                        if self._solve_pow(temp_session):
                            new_cookies = temp_session.cookies.get_dict()
                            if new_cookies:
                                existing = {}
                                if self._site_cookie:
                                    for item in self._site_cookie.split("; "):
                                        if "=" in item:
                                            k, v = item.split("=", 1)
                                            existing[k] = v
                                existing.update(new_cookies)
                                self._site_cookie = "; ".join(f"{k}={v}" for k, v in existing.items())
                            return _send(use_session=False)
            except requests.RequestException as e:
                logger.error(f"PoW重试请求失败: {e}")
                raise
            raise requests.RequestException("PoW验证失败，无法继续请求")

        if self._session and self._is_login_page(resp):
            logger.info("Session已过期（检测到登录页），尝试自动重新登录...")
            self._session = None
            if self._site_username and self._site_password:
                success, msg = self._site_login()
                if success and self._session:
                    logger.info("重新登录成功，重试请求")
                    return _send(use_session=True)
                logger.error(f"重新登录失败: {msg}")
            raise requests.RequestException("Session已过期且自动重新登录失败")

        return resp

    @staticmethod
    def _is_login_page(resp) -> bool:
        """检测响应是否为登录页面"""
        # 检查URL是否被重定向到登录页
        if "/user/login" in resp.url:
            return True
        # 检查HTML中是否包含登录表单
        text = resp.text[:2000] if resp.text else ""
        if 'name="username"' in text or 'name="password"' in text:
            if 'name="dosubmit"' in text or 'id="login"' in text.lower():
                return True
        return False

    def _api_login(self) -> dict:
        """API: 手动触发站点登录"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not self._site_username or not self._site_password:
            return {"success": False, "message": "未配置站点用户名和密码"}

        success, msg = self._site_login()
        return {"success": success, "message": msg}

    @staticmethod
    def _notification_type(name: str = "Plugin"):
        return getattr(NotificationType, name, getattr(NotificationType, "Plugin", None))

    @staticmethod
    def _get_mp_api_base_url() -> str:
        for key in ("MOVIEPILOT_API_BASE_URL", "MP_API_BASE_URL", "MOVIEPILOT_URL", "MP_URL"):
            value = os.environ.get(key)
            if value:
                return value.strip().rstrip("/")
        try:
            from app.core.config import settings
            for key in ("API_BASE_URL", "WEB_API_BASE_URL", "MOVIEPILOT_URL", "SITE_URL"):
                value = getattr(settings, key, None)
                if value:
                    return str(value).strip().rstrip("/")
        except Exception:
            pass
        return "http://127.0.0.1:3000"

    def _get_mp_api_base_urls(self) -> List[str]:
        # 优先用 127.0.0.1:3000（MP v2 容器内部端口），其他地址兜底
        urls = ["http://127.0.0.1:3000", self._get_mp_api_base_url()]
        result = []
        for url in urls:
            if url and url not in result:
                result.append(url)
        return result

    @staticmethod
    def _get_mp_api_token() -> str:
        for key in ("API_TOKEN", "MOVIEPILOT_API_TOKEN", "MP_API_TOKEN"):
            value = os.environ.get(key)
            if value:
                return value.strip()
        try:
            from app.core.config import settings
            for key in ("API_TOKEN", "TOKEN"):
                value = getattr(settings, key, None)
                if value:
                    return str(value).strip()
        except Exception:
            pass
        logger.warning("未获取到 MoviePilot API Token，相关内部API调用可能失败")
        return ""

    @staticmethod
    def _normalize_info_hash(value: Any) -> str:
        """从115任务字段或磁力链接中提取可匹配的 info_hash。"""
        if value is None:
            return ""
        text = str(value).strip().lower()
        if not text:
            return ""
        magnet_match = re.search(r"btih:([a-f0-9]{32,40})", text, re.IGNORECASE)
        if magnet_match:
            return magnet_match.group(1).lower()
        hash_match = re.search(r"\b([a-f0-9]{32,40})\b", text, re.IGNORECASE)
        return hash_match.group(1).lower() if hash_match else ""

    def _get_task_info_hash(self, task: dict) -> str:
        """兼容不同115接口返回的 hash 字段名。"""
        if not isinstance(task, dict):
            return ""
        for key in ("info_hash", "hash", "btih", "torrent_hash", "file_id", "url"):
            info_hash = self._normalize_info_hash(task.get(key))
            if info_hash:
                return info_hash
        return ""

    @staticmethod
    def _get_magnet_display_name(magnet: str) -> str:
        """从磁力链接 dn 参数提取显示名，提取失败返回空字符串。"""
        if not magnet or not str(magnet).startswith("magnet:?"):
            return ""
        try:
            query = urlparse(magnet).query
            dn_values = parse_qs(query).get("dn") or []
            if not dn_values:
                return ""
            return unquote(str(dn_values[0] or "")).strip()
        except Exception:
            return ""

    @staticmethod
    def _get_task_folder_cid(task: dict) -> str:
        """兼容不同115接口返回的下载目录 CID 字段。"""
        if not isinstance(task, dict):
            return ""
        for key in ("folder_cid", "cid", "file_id", "fid", "wp_path_id", "save_path_id"):
            value = str(task.get(key) or "").strip()
            if value and value not in ("0", "None", "null"):
                return value
        return ""

    @staticmethod
    def _get_task_name(task: dict, fallback: str = "") -> str:
        """兼容不同115接口返回的任务名称字段。"""
        if not isinstance(task, dict):
            return fallback
        return str(
            task.get("name")
            or task.get("file_name")
            or task.get("title")
            or task.get("save_name")
            or fallback
            or ""
        )

    @staticmethod
    def _is_offline_completed(task: dict) -> bool:
        """判断115离线任务是否完成，兼容数字和字符串状态。"""
        status = task.get("status")
        if isinstance(status, str):
            status_text = status.strip().lower()
            if status_text in ("2", "done", "finish", "finished", "success", "completed"):
                return True
        if status == 2:
            return True

        percent = task.get("percentDone") or task.get("percent") or task.get("progress")
        try:
            if percent is not None and float(str(percent).rstrip("%")) >= 100:
                return True
        except (TypeError, ValueError):
            pass

        return bool(task.get("done") or task.get("finish") or task.get("is_finish"))

    @staticmethod
    def _is_offline_failed(task: dict) -> bool:
        """判断115离线任务是否失败。115的错误码为负数，status也可能是负数字符串。"""
        status = task.get("status")
        if isinstance(status, str):
            status_str = status.strip().lower()
            # 匹配 -1, -2, fail, failed, error 等
            if status_str in ("fail", "failed", "error"):
                return True
            try:
                return int(status_str) < 0
            except (ValueError, TypeError):
                return False
        if isinstance(status, (int, float)):
            return status < 0
        return False

    def _save_offline_pending_tasks(self) -> None:
        with self._task_lock:
            data = {str(k): dict(v) for k, v in self._pending_tasks.items() if isinstance(v, dict)}
        self.save_data(self._offline_pending_data_key, data)
        logger.info(f"离线监控任务已保存: {len(data)} 个")

    def _load_offline_pending_from_storage(self) -> Dict[str, dict]:
        saved_offline_pending = self.get_data(self._offline_pending_data_key) or {}
        if isinstance(saved_offline_pending, dict):
            return {str(k).lower(): v for k, v in saved_offline_pending.items() if isinstance(v, dict)}
        if isinstance(saved_offline_pending, list):
            restored: Dict[str, dict] = {}
            dropped_count = 0
            for t in saved_offline_pending:
                if isinstance(t, dict) and (t.get("info_hash") or t.get("hash")):
                    restored[str(t.get("info_hash") or t.get("hash")).lower()] = t
                else:
                    dropped_count += 1
            if dropped_count:
                logger.warning(f"恢复离线监控任务时跳过 {dropped_count} 条无效记录")
            return restored
        return {}

    def _merge_offline_pending_from_storage(self) -> None:
        stored_tasks = self._load_offline_pending_from_storage()
        if not stored_tasks:
            return
        with self._task_lock:
            added = 0
            for key, task in stored_tasks.items():
                if key not in self._pending_tasks:
                    self._pending_tasks[key] = task
                    added += 1
        if added:
            logger.info(f"从持久化数据恢复离线监控任务: {added} 个")

    def _reload_history_from_storage(self) -> None:
        saved_search_history = self.get_data(self._search_history_data_key) or []
        if isinstance(saved_search_history, list):
            self._search_history = [item for item in saved_search_history if isinstance(item, dict)][:20]
        saved_offline_history = self.get_data(self._offline_history_data_key) or []
        if isinstance(saved_offline_history, list):
            self._offline_history = [item for item in saved_offline_history if isinstance(item, dict)][:20]

    def _iter_u115_offline_tasks(self, result: Any) -> List[dict]:
        """兼容不同115接口返回结构，拉平离线任务列表。"""
        tasks: List[dict] = []
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    tasks.append(item)
                else:
                    tasks.extend(self._iter_u115_offline_tasks(item))
        elif isinstance(result, dict):
            for key in ("tasks", "data", "list", "items"):
                child = result.get(key)
                if child is not None and child is not result:
                    tasks.extend(self._iter_u115_offline_tasks(child))
        return tasks

    def _find_u115_offline_task(self, client: P115Client, info_hash: str) -> Optional[dict]:
        """按 info_hash 查找115离线任务记录。"""
        info_hash = self._normalize_info_hash(info_hash)
        if not info_hash:
            return None
        try:
            result = P115OpenClient.clouddownload_task_list(client)
            for task in self._iter_u115_offline_tasks(result):
                if self._get_task_info_hash(task) == info_hash:
                    return task
        except Exception as e:
            logger.warning(f"查找115离线任务失败: {e}")
        return None

    def _delete_u115_offline_task_record(self, client: P115Client, task: dict, info_hash: str = "") -> bool:
        """只删除115离线任务记录，不删除网盘文件；兼容不同 p115client 方法名和参数。"""
        info_hash = self._normalize_info_hash(info_hash or self._get_task_info_hash(task))
        task_id = str(task.get("id") or task.get("task_id") or task.get("fid") or task.get("file_id") or "").strip() if isinstance(task, dict) else ""
        task_name = self._get_task_name(task, info_hash) if isinstance(task, dict) else info_hash
        method_names = (
            "clouddownload_task_delete",
            "clouddownload_task_del",
            "clouddownload_task_remove",
            "clouddownload_task_cancel",
            "clouddownload_task_clear",
        )
        payloads = []
        if task_id:
            payloads.extend([
                {"id": task_id, "delete_file": 0},
                {"task_id": task_id, "delete_file": 0},
                {"ids": [task_id], "delete_file": 0},
                {"task_ids": [task_id], "delete_file": 0},
            ])
        if info_hash:
            payloads.extend([
                {"info_hash": info_hash, "delete_file": 0},
                {"hash": info_hash, "delete_file": 0},
                {"btih": info_hash, "delete_file": 0},
            ])
        if not payloads:
            logger.warning(f"无法删除115旧离线任务记录，缺少任务ID/hash: {task}")
            return False

        for method_name in method_names:
            method = getattr(P115OpenClient, method_name, None)
            if not callable(method):
                continue
            for payload in payloads:
                try:
                    result = method(client, payload)
                    logger.info(f"删除115旧离线任务记录: method={method_name}, payload={payload}, result={result}")
                    if isinstance(result, dict) and (result.get("state") is False or result.get("success") is False):
                        continue
                    return True
                except Exception as e:
                    logger.debug(f"删除115旧离线任务记录尝试失败: method={method_name}, payload={payload}, error={e}")
        logger.warning(f"删除115旧离线任务记录失败: {task_name}, info_hash={info_hash}, task_id={task_id}")
        return False


    def _update_offline_task_status(self, info_hash: str, status: str, **extra) -> None:
        with self._task_lock:
            if not info_hash or info_hash not in self._pending_tasks:
                return
            self._pending_tasks[info_hash].update({
                "status": status,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **extra,
            })
        self._save_offline_pending_tasks()

    def _resolve_cid_path(self, cid: str) -> str:
        """通过115官方API查询CID对应的完整目录路径

        Args:
            cid: 115目录的CID

        Returns:
            完整路径字符串，如 "/云下载/需入库/临时/mp"，失败返回空字符串
        """
        if not cid or not self._p115_cookie:
            return ""

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            url = "https://webapi.115.com/files"
            headers = {
                "Cookie": p115_cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://115.com/",
            }
            params = {
                "aid": 1,
                "cid": cid,
                "show_dir": 1,
                "limit": 10,
            }
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("state"):
                logger.warning(f"115 API返回错误: {data}")
                return ""

            # 从 path 数组获取完整目录层级，如 [{"cid":"0","name":"根目录"},{"cid":"123","name":"云下载"},...]
            path_list = data.get("path", []) if isinstance(data, dict) else []
            if not path_list:
                return ""

            # 跳过第一项"根目录"，拼接完整路径
            parts = [p.get("name", "") for p in path_list if p.get("name")]
            if parts:
                return "/" + "/".join(parts[1:] if parts[0] == "根目录" else parts)
            return ""

        except requests.RequestException as e:
            logger.warning(f"CID路径解析HTTP请求失败: {e}")
            return ""
        except Exception as e:
            logger.warning(f"CID路径解析异常: {e}")
            return ""

    def _api_resolve_cid(self, data: dict = None) -> dict:
        """API: 通过CID解析115目录完整路径"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not self._p115_cookie:
            return {"success": False, "message": "未配置115网盘Cookie"}

        cid = (data or {}).get("cid") or self._save_dir_id
        if not cid:
            return {"success": False, "message": "请提供CID"}

        resolved = self._resolve_cid_path(cid)
        if resolved:
            self._resolved_path = resolved
            return {"success": True, "path": resolved}
        else:
            return {"success": False, "message": "未能解析CID路径，请检查Cookie和CID是否正确"}

    def _api_close_task(self, data: dict = None, type: str = "", key: str = "", info_hash: str = "", file_path: str = "", task_name: str = "") -> dict:
        """关闭首页显示的任务记录。"""
        data = data or {}
        task_type = str(data.get("type") or data.get("task_type") or type or "").strip()
        key = str(data.get("key") or data.get("info_hash") or data.get("file_path") or data.get("task_name") or key or info_hash or file_path or task_name or "").strip()
        if not task_type or not key:
            return {"success": False, "message": "请提供 type 和 key"}

        if task_type == "all":
            # 标记所有正在整理的任务为取消
            self._cancel_flags = {k: True for k in self._cancel_flags}
            # 停止轮询线程并等待退出
            if self._polling_stop:
                self._polling_stop.set()
            old_thread = self._polling_thread
            if old_thread and old_thread.is_alive():
                try:
                    old_thread.join(timeout=5)
                except Exception:
                    pass
            with self._task_lock:
                self._pending_tasks.clear()
            self._save_offline_pending_tasks()
            self.save_data("pending_transfer_tasks", [])
            self._polling_thread = None
            return {"success": True, "message": "本地任务已清空"}

        if task_type == "offline":
            with self._task_lock:
                removed = self._pending_tasks.pop(key, None)
            # 设置取消标志，中断正在执行的整理流程
            self._cancel_flags[key] = True
            if removed:
                self._save_offline_pending_tasks()
                return {"success": True, "message": "离线监控任务已关闭"}
            return {"success": False, "message": "未找到离线监控任务"}

        if task_type == "transfer":
            pending_tasks = self.get_data("pending_transfer_tasks") or []
            removed_task = None
            if key.isdigit():
                index = int(key)
                if 0 <= index < len(pending_tasks):
                    removed_task = pending_tasks.pop(index)
                    self.save_data("pending_transfer_tasks", pending_tasks)
                    # 设置取消标志
                    if removed_task:
                        cancel_key = str(removed_task.get("info_hash") or removed_task.get("folder_cid") or removed_task.get("task_name") or "")
                        if cancel_key:
                            self._cancel_flags[cancel_key] = True
                    return {"success": True, "message": f"待整理任务已关闭: {removed_task.get('task_name') or removed_task.get('file_path') or index}"}
            new_tasks = []
            for t in pending_tasks:
                if t.get("file_path") == key or t.get("task_name") == key or str(t.get("folder_cid") or "") == key:
                    removed_task = t
                    continue
                new_tasks.append(t)
            if len(new_tasks) != len(pending_tasks):
                self.save_data("pending_transfer_tasks", new_tasks)
                # 设置取消标志
                if removed_task:
                    cancel_key = str(removed_task.get("info_hash") or removed_task.get("folder_cid") or removed_task.get("task_name") or "")
                    if cancel_key:
                        self._cancel_flags[cancel_key] = True
                return {"success": True, "message": "待整理任务已关闭"}
            return {"success": False, "message": "未找到待整理任务"}

        return {"success": False, "message": f"未知任务类型: {task_type}"}

    def _api_clear_history(self, data: dict = None, type: str = "") -> dict:
        """清空搜索或离线历史记录"""
        data = data or {}
        history_type = str(data.get("type") or type or "").strip()
        if history_type == "search":
            self._search_history = []
            self.save_data(self._search_history_data_key, [])
            logger.info("搜索历史已清空")
            return {"success": True, "message": "搜索历史已清空"}
        elif history_type == "offline":
            self._offline_history = []
            self.save_data(self._offline_history_data_key, [])
            logger.info("离线下载历史已清空")
            return {"success": True, "message": "离线下载历史已清空"}
        elif history_type == "all":
            self._search_history = []
            self._offline_history = []
            self.save_data(self._search_history_data_key, [])
            self.save_data(self._offline_history_data_key, [])
            logger.info("所有历史记录已清空")
            return {"success": True, "message": "所有历史记录已清空"}
        return {"success": False, "message": "请提供 type 参数（search/offline/all）"}

    def _api_search(self, keyword: str = "", page: int = 1, search_type: str = "4") -> dict:
        """API: 搜索磁力资源

        Args:
            keyword: 搜索关键词
            page: 页码，默认1
            search_type: 搜索类型 (1=电影, 2=剧集, 3=动漫, 4=种子, 5=网盘)，默认4=种子
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not self._site_url:
            return {"success": False, "message": "未配置站点地址"}

        # 确保已认证
        if not self._ensure_authenticated():
            return {"success": False, "message": "站点认证失败，请检查Cookie或账号密码配置"}

        if not keyword:
            return {"success": False, "message": "请输入搜索关键词"}

        try:
            search_url = f"{self._site_url}/search"
            params = {
                "q": keyword,
                "type": search_type,
            }
            if page > 1:
                params["page"] = page

            logger.info(f"搜索影视磁力资源: {keyword}, URL: {search_url}")

            resp = self._request_with_pow("GET", search_url, params=params)
            resp.raise_for_status()

            # 解析搜索结果页面
            results = self._parse_search_page(resp.text)

            # 缓存搜索结果
            cache_key = f"{keyword}:{search_type}:{page}"
            self._cache_search_results(cache_key, results)

            if not results:
                return {"success": True, "message": "未找到相关资源", "data": []}

            self._record_search_history(keyword, len(results))

            return {
                "success": True,
                "message": f"找到 {len(results)} 个结果",
                "data": results,
                "page": page,
            }

        except requests.RequestException as e:
            logger.error(f"搜索请求失败: {e}")
            return {"success": False, "message": f"搜索请求失败: {str(e)}"}
        except Exception as e:
            logger.error(f"搜索异常: {e}")
            return {"success": False, "message": f"搜索异常: {str(e)}"}

    def _parse_search_page(self, html_content: str) -> List[dict]:
        """解析搜索结果页面，从 _obj.search JSON 中提取种子列表"""
        results = []

        try:
            s_str = self._extract_json_obj(html_content, "search")
            if not s_str:
                logger.warning("未找到 _obj.search JSON数据")
                return results

            data = json.loads(s_str)
            items = data.get("l", {})
            titles = items.get("title", [])
            sizes = items.get("size", [])
            seeds = items.get("seeds", [])
            times = items.get("time", [])
            detail_ids = items.get("i", [])
            detail_types = items.get("d", [])

            for i, title in enumerate(titles):
                try:
                    size_text = sizes[i] if i < len(sizes) else ""
                    seeders = seeds[i] if i < len(seeds) else 0
                    update_time = times[i] if i < len(times) else ""
                    detail_id = detail_ids[i] if i < len(detail_ids) else ""
                    detail_type = detail_types[i] if i < len(detail_types) else "bt"

                    if not title or not detail_id:
                        continue

                    detail_path = f"/{detail_type}/{detail_id}"
                    unique_id = hashlib.sha256(detail_path.encode()).hexdigest()[:16]

                    results.append({
                        "id": unique_id,
                        "title": title,
                        "size": size_text,
                        "seeders": str(seeders),
                        "update_time": update_time,
                        "detail_path": detail_path,
                        "detail_url": f"{self._site_url}{detail_path}",
                    })

                except Exception as e:
                    logger.warning(f"解析搜索结果项失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"解析搜索页面失败: {e}")

        return results

    def _api_detail(self, detail_path: str = "") -> dict:
        """API: 获取资源详情和磁力链接

        Args:
            detail_path: 资源详情页路径，如 /bt/9Exwr
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not detail_path:
            return {"success": False, "message": "请提供资源详情路径"}
        if not self._is_safe_detail_path(detail_path):
            return {"success": False, "message": "资源详情路径不合法"}

        # 确保已认证
        if not self._ensure_authenticated():
            return {"success": False, "message": "站点认证失败，请检查Cookie或账号密码配置"}

        try:
            detail_url = f"{self._site_url}{detail_path}"
            logger.info(f"获取资源详情: {detail_url}")

            resp = self._request_with_pow("GET", detail_url)
            resp.raise_for_status()

            # 解析详情页面
            detail = self._parse_detail_page(resp.text)

            if not detail:
                return {"success": False, "message": "无法解析资源详情"}

            return {"success": True, "data": detail}

        except requests.RequestException as e:
            logger.error(f"获取详情失败: {e}")
            return {"success": False, "message": f"获取详情失败: {str(e)}"}
        except Exception as e:
            logger.error(f"获取详情异常: {e}")
            return {"success": False, "message": f"获取详情异常: {str(e)}"}

    @staticmethod
    def _is_safe_detail_path(detail_path: str) -> bool:
        if not isinstance(detail_path, str):
            return False
        path = detail_path.strip()
        if not path.startswith("/") or path.startswith("//"):
            return False
        if ".." in path or "\\" in path or any(ch in path for ch in ("\r", "\n", "\t")):
            return False
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc:
            return False
        return bool(re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", path))

    @staticmethod
    def _extract_json_obj(text: str, key: str) -> Optional[str]:
        """从JS文本中提取 _obj.key = {...}; 的JSON字符串，正确处理嵌套括号

        Args:
            text: 包含JS代码的HTML文本
            key: _obj.xxx 中的 xxx 键名

        Returns:
            JSON字符串，失败返回None
        """
        start_marker = f"_obj.{key}="
        idx = text.find(start_marker)
        if idx < 0:
            return None
        idx += len(start_marker)
        while idx < len(text) and text[idx] != '{':
            idx += 1
        if idx >= len(text):
            return None
        depth = 0
        buf = []
        in_string = False
        escape = False
        quote_char = ""
        max_len = 2 * 1024 * 1024
        for i in range(idx, min(len(text), idx + max_len)):
            ch = text[i]
            buf.append(ch)
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote_char:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                quote_char = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return ''.join(buf)
        return None

    def _parse_detail_page(self, html_content: str) -> Optional[dict]:
        """解析详情页面，从 _obj.d JSON 提取磁力链接和文件信息"""
        try:
            d_str = self._extract_json_obj(html_content, "d")
            if not d_str:
                logger.warning("未找到 _obj.d JSON数据")
                return None

            data = json.loads(d_str)

            magnet = data.get("magnet", "")

            torrent_hash = data.get("torrent", "")
            torrent_url = ""
            if torrent_hash:
                torrent_url = f"{self._site_url}/dbt/{torrent_hash}"

            filelist = data.get("filelist", [])
            files = []
            for f in filelist:
                if isinstance(f, str):
                    files.append(f)

            return {
                "title": data.get("title", ""),
                "magnet": magnet,
                "torrent_url": torrent_url,
                "offline_url": "",
                "files": files,
            }

        except Exception as e:
            logger.error(f"解析详情页面失败: {e}")
            return None


    def _api_offline_download(self, data: dict = None) -> dict:
        """API: add 115 offline download task."""
        if not self._enabled:
            return {"success": False, "message": "\u63d2\u4ef6\u672a\u542f\u7528"}

        if not self._p115_cookie:
            return {"success": False, "message": "\u672a\u914d\u7f6e115\u7f51\u76d8Cookie"}

        if not self._save_dir_id:
            return {"success": False, "message": "\u672a\u914d\u7f6e115\u79bb\u7ebf\u4e0b\u8f7d\u76ee\u5f55ID"}

        if not data:
            return {"success": False, "message": "\u8bf7\u63d0\u4f9b\u4e0b\u8f7d\u4fe1\u606f"}

        magnet = (data.get("magnet") or "").strip()
        title = str(data.get("title") or data.get("name") or "").strip()

        if not magnet:
            return {"success": False, "message": "\u8bf7\u63d0\u4f9b\u6709\u6548\u7684\u78c1\u529b\u94fe\u63a5"}

        if not (magnet.startswith("magnet:?") or magnet.startswith("http")):
            return {"success": False, "message": "\u4e0d\u652f\u6301\u7684\u94fe\u63a5\u683c\u5f0f\uff0c\u8bf7\u4f7f\u7528\u78c1\u529b\u94fe\u63a5(magnet:)\u6216HTTP\u94fe\u63a5"}

        title = title or self._get_magnet_display_name(magnet)
        display_title = title or "未命名磁力任务"

        local_info_hash = self._normalize_info_hash(magnet)
        with self._task_lock:
            duplicate_pending = bool(local_info_hash and local_info_hash in self._pending_tasks)
        if duplicate_pending:
            logger.info(f"\u79bb\u7ebf\u4efb\u52a1\u5df2\u5728\u8f6e\u8be2\u961f\u5217\u4e2d\uff0c\u8df3\u8fc7\u91cd\u590d\u63d0\u4ea4: {title} ({local_info_hash[:12]}...)")
            self._start_polling()
            _session = data.get("_session") or {}
            self.post_message(
                title="115\u79bb\u7ebf\u4efb\u52a1\u5df2\u5b58\u5728",
                text=f"**{title}** \u5df2\u5728\u79bb\u7ebf\u76d1\u63a7\u961f\u5217\u4e2d\uff0c\u5df2\u7ed3\u675f\u672c\u6b21\u91cd\u590d\u63d0\u4ea4\u3002\ninfo_hash: `{local_info_hash}`",
                mtype=NotificationType.Plugin,
                channel=self._convert_channel(_session.get("channel")),
                userid=_session.get("userid"),
            )
            return {
                "success": True,
                "message": f"\u4efb\u52a1\u5df2\u5b58\u5728\uff0c\u5df2\u7ed3\u675f\u91cd\u590d\u63d0\u4ea4: {title}",
                "data": {"info_hash": local_info_hash, "duplicate": True},
            }

        try:
            save_dir_id = int(self._save_dir_id)
        except (ValueError, TypeError):
            logger.error(f"\u65e0\u6548\u7684115\u76ee\u5f55ID: {self._save_dir_id}")
            return {"success": False, "message": f"\u65e0\u6548\u7684115\u76ee\u5f55ID: {self._save_dir_id}\uff0c\u8bf7\u68c0\u67e5\u914d\u7f6e"}

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            payload = {
                "urls": magnet,
                "wp_path_id": save_dir_id,
            }

            logger.info(f"\u6dfb\u52a0115\u79bb\u7ebf\u4e0b\u8f7d: {title}, \u76ee\u6807\u76ee\u5f55CID: {save_dir_id}")
            result = P115OpenClient.clouddownload_task_add_urls(client, payload)
            if not isinstance(result, dict):
                logger.error(f"115离线下载返回异常格式: {type(result).__name__}")
                return {"success": False, "message": "115接口返回异常格式"}

            state = result.get("state", False) if result else False
            errcode = result.get("errcode") or result.get("code") if result else None
            error_msg = (result.get("error_msg") or result.get("error") or result.get("message") or "") if result else ""

            if state and not error_msg:
                data_items = result.get("data") or []
                if isinstance(data_items, list):
                    for item in data_items:
                        if isinstance(item, dict) and not item.get("state", True):
                            state = False
                            error_msg = item.get("message") or item.get("error_msg") or f"\u5355\u6761\u4efb\u52a1\u5931\u8d25 (code={item.get('code')})"
                            break

            def _iter_result_items(value):
                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            yield row
                elif isinstance(value, dict):
                    for key in ("tasks", "data", "list", "items"):
                        child = value.get(key)
                        if child is not value:
                            yield from _iter_result_items(child)

            def _track_offline_task(reason: str) -> str:
                info_hash = ""
                task_name = title
                task_folder_cid = ""
                for item in _iter_result_items(result):
                    info_hash = self._get_task_info_hash(item) or info_hash
                    task_name = self._get_task_name(item, task_name)
                    task_folder_cid = self._get_task_folder_cid(item) or task_folder_cid
                    if info_hash and (task_name or task_folder_cid):
                        break
                task_name = str(task_name or "").strip()
                if not task_name:
                    task_name = display_title
                if not info_hash:
                    info_hash = self._normalize_info_hash(magnet)
                    if info_hash:
                        logger.info(f"\u4ece\u78c1\u529b\u94fe\u63a5\u63d0\u53d6 info_hash \u7528\u4e8e\u8f6e\u8be2: {info_hash[:12]}...")
                if not info_hash:
                    logger.warning(f"\u672a\u80fd\u83b7\u53d6 info_hash\uff0c\u65e0\u6cd5\u52a0\u5165\u79bb\u7ebf\u5b8c\u6210\u8f6e\u8be2\u76d1\u63a7: {title}")
                    return ""
                with self._task_lock:
                    self._pending_tasks[info_hash] = {
                        "name": task_name or display_title,
                        "title": title,
                        "display_title": display_title,
                        "folder_cid": task_folder_cid,
                        "info_hash": info_hash,
                        "status": "已加入离线监控",
                        "add_time": time.time(),
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "session": data.get("_session") or {},
                    }
                self._save_offline_pending_tasks()
                logger.info(f"\u5df2\u52a0\u5165\u79bb\u7ebf\u5b8c\u6210\u8f6e\u8be2\u76d1\u63a7({reason}): {title} ({info_hash[:12]}...)")
                self._start_polling()
                return info_hash

            if errcode == 10008:
                logger.info(f"115离线下载任务已存在: {display_title}")
                duplicate_hash = self._normalize_info_hash(magnet)
                duplicate_task = self._find_u115_offline_task(client, duplicate_hash) if duplicate_hash else None
                retry_after_delete = bool(data.get("_retry_after_delete"))

                if duplicate_task:
                    duplicate_name = self._get_task_name(duplicate_task, display_title)
                    folder_cid, actual_folder_name = self._find_download_folder_by_task(client, duplicate_name, duplicate_task)
                    if folder_cid:
                        task_name_for_transfer = actual_folder_name or duplicate_name or display_title
                        task_for_transfer = dict(duplicate_task)
                        task_for_transfer.update({"folder_cid": folder_cid, "name": task_name_for_transfer})
                        logger.info(f"115已存在任务对应文件夹存在，异步触发重命名整理: {task_name_for_transfer}, CID={folder_cid}")
                        self._record_offline_history(display_title, True)
                        # 异步执行重命名整理，避免阻塞工具调用
                        def _async_transfer():
                            _tsession = data.get("_session") or {}
                            try:
                                transfer_result = self._trigger_transfer(task_name_for_transfer, task_for_transfer, info_hash=duplicate_hash)
                                if isinstance(transfer_result, dict) and transfer_result.get("needs_agent"):
                                    self._notify_agent(
                                        transfer_result.get("agent_message", ""),
                                        channel=_tsession.get("channel"),
                                        source="clsearch",
                                        userid=_tsession.get("userid", "clsearch"),
                                        username=_tsession.get("username", "clsearch"),
                                    )
                            except Exception as e:
                                logger.error(f"异步重命名整理异常: {e}")
                        threading.Thread(target=_async_transfer, daemon=True).start()
                        return {
                            "success": True,
                            "message": f"任务已存在，正在后台整理: {task_name_for_transfer}",
                            "data": {"info_hash": duplicate_hash, "folder_cid": folder_cid, "duplicate": True, "transfer_triggered": True},
                        }
                    if not retry_after_delete:
                        logger.info(f"115已存在任务未找到对应文件夹，将尝试清理旧任务记录后重新离线: {display_title}")
                        if self._delete_u115_offline_task_record(client, duplicate_task, duplicate_hash):
                            logger.info(f"旧115离线任务记录已删除，重新提交磁力链接: {display_title}")
                            retry_data = dict(data)
                            retry_data["_retry_after_delete"] = True
                            return self._api_offline_download(retry_data)
                        logger.warning(f"115旧离线任务记录删除失败，保留已存在任务并加入轮询: {display_title}")

                self._record_offline_history(display_title, True)
                tracked_hash = _track_offline_task("duplicate")
                _session = data.get("_session") or {}
                self.post_message(
                    title="115离线任务已存在",
                    text=f"**{display_title}** 已在115离线任务中，已跳过重复添加。" + (f"\ninfo_hash: `{tracked_hash}`" if tracked_hash else ""),
                    mtype=NotificationType.Plugin,
                    channel=self._convert_channel(_session.get("channel")),
                    userid=_session.get("userid"),
                )
                return {
                    "success": True,
                    "message": f"任务已存在，跳过重复添加: {display_title}",
                    "data": result,
                }

            if state:
                logger.info(f"115\u79bb\u7ebf\u4e0b\u8f7d\u6dfb\u52a0\u6210\u529f: {title}")
                self._record_offline_history(display_title, True)
                _track_offline_task("added")
                return {
                    "success": True,
                    "message": f"\u5df2\u6dfb\u52a0\u5230115\u79bb\u7ebf\u4e0b\u8f7d: {title}",
                    "data": result,
                }

            if not error_msg:
                error_msg = f"\u672a\u77e5\u9519\u8bef (errcode={errcode})" if errcode else "\u672a\u77e5\u9519\u8bef"
            logger.error(f"115\u79bb\u7ebf\u4e0b\u8f7d\u6dfb\u52a0\u5931\u8d25: {error_msg}")
            self._record_offline_history(display_title, False, error_msg)
            return {
                "success": False,
                "message": f"\u6dfb\u52a0\u5931\u8d25: {error_msg}",
                "data": result,
            }

        except Exception as e:
            logger.error(f"115\u79bb\u7ebf\u4e0b\u8f7d\u5f02\u5e38: {e}")
            return {"success": False, "message": f"\u4e0b\u8f7d\u5f02\u5e38: {str(e)}"}


    @eventmanager.register(EventType.PluginAction)
    def handle_event(self, event: Event) -> None:
        """处理插件命令事件"""
        event_data = event.event_data or {}
        if event_data.get("action") != "clsearch":
            return

        # MoviePilot 将 /clsearch xxx 的参数放在 event_data["data"]["arg_str"]
        keyword = (
            event_data.get("data", {}).get("arg_str", "")
            or event_data.get("arg_str", "")
            or event_data.get("keyword", "")
        ).strip()

        logger.info(f"观影搜命令触发: keyword='{keyword}', event_data keys={list(event_data.keys())}")

        if not keyword:
            self.post_message(
                title="观影搜",
                text="请输入搜索关键词，例如: /clsearch 开端",
                mtype=self._notification_type("Warning"),
            )
            return

        result = self._api_search(keyword)
        if result.get("success") and result.get("data"):
            self._send_search_results(keyword, result["data"])
        else:
            self.post_message(
                title="观影搜",
                text=result.get("message", "搜索失败"),
                mtype=self._notification_type("Warning"),
            )

    # ==================== 后台轮询 ====================

    def _start_polling(self) -> None:
        """启动后台轮询线程，监控115离线任务完成状态。"""
        with self._polling_lock:
            if self._polling_thread and self._polling_thread.is_alive():
                return
            self._polling_stop = threading.Event()
            self._polling_thread = threading.Thread(
                target=self._polling_worker,
                name="115OfflinePolling",
                daemon=True,
            )
            self._polling_thread.start()
        logger.info("115离线下载轮询线程已启动")

    def _stop_polling(self) -> None:
        """停止后台轮询线程。"""
        with self._polling_lock:
            stop_event = self._polling_stop
            thread = self._polling_thread
            if stop_event:
                stop_event.set()
        if thread:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("115离线下载轮询线程未在5秒内退出")
        with self._polling_lock:
            self._polling_thread = None
            self._polling_stop = None
        logger.info("115离线下载轮询线程已停止")

    def _polling_worker(self) -> None:
        """后台轮询工作线程。"""
        while self._polling_stop and not self._polling_stop.is_set():
            try:
                self._merge_offline_pending_from_storage()
                with self._task_lock:
                    has_pending = bool(self._pending_tasks)
                if has_pending:
                    self._check_task_status()
                else:
                    self._polling_stop.wait(self._idle_poll_interval)
                    continue
            except Exception as e:
                logger.error(f"115离线轮询异常: {e}")
            self._polling_stop.wait(self._poll_interval)

    def _check_task_status(self) -> None:
        """检查115离线任务状态，标记已完成的任务。"""
        with self._task_lock:
            pending_snapshot = list(self._pending_tasks.items())
        if not pending_snapshot:
            return

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            result = P115OpenClient.clouddownload_task_list(client)

            if not isinstance(result, dict) or not result.get("state"):
                logger.warning(f"115离线任务列表获取失败或Cookie失效: {result}")
                return

            data = result.get("data") or {}
            if isinstance(data, list):
                tasks = data
            elif isinstance(data, dict):
                tasks = data.get("tasks") or data.get("list") or data.get("items") or []
            else:
                tasks = []
            logger.info(f"115离线轮询: 待监控 {len(pending_snapshot)} 个，接口返回 {len(tasks)} 个任务")

            now = time.time()
            task_map = {}
            for task in tasks:
                ih = self._get_task_info_hash(task)
                if ih:
                    task_map[ih] = task

            completed_keys = []
            expired_keys = []
            terminal_statuses = {"重命名失败", "整理失败", "离线失败", "异常", "失败"}

            for info_hash, task_info in pending_snapshot:
                current_status = str(task_info.get("status") or "").strip()
                if current_status in terminal_statuses or current_status.startswith("待处理："):
                    expired_keys.append(info_hash)
                    logger.info(f"离线监控任务已进入终态，移出轮询队列: {task_info.get('title') or info_hash} ({current_status})")
                    continue
                task = task_map.get(info_hash)
                if not task:
                    task_name = self._get_task_name(task_info, task_info.get("title", ""))
                    folder_cid, actual_folder_name = self._find_download_folder_by_task(client, task_name, task_info)
                    if folder_cid:
                        if actual_folder_name:
                            task_name = actual_folder_name
                        if not self._auto_transfer:
                            completed_keys.append(info_hash)
                        logger.info(f"115离线任务列表未匹配到hash，但下载目录已找到文件夹，按完成处理: {task_name}, CID: {folder_cid}")
                        self._update_offline_task_status(info_hash, "离线完成", folder_cid=folder_cid, path=self._join_u115_path(self._resolved_path, task_name) if self._resolved_path else task_name)
                        self._record_offline_history(task_name, True)
                        # 自动整理模式下跳过此通知，由 _trigger_transfer 统一发"重命名完成"
                        if not self._auto_transfer:
                            _session = task_info.get("session") or {}
                            self.post_message(
                                title="115离线下载完成",
                                text=f"**{task_name}** 已离线下载完成",
                                mtype=NotificationType.Plugin,
                                channel=self._convert_channel(_session.get("channel")),
                                userid=_session.get("userid"),
                            )
                        if self._auto_transfer:
                            task_info["folder_cid"] = folder_cid
                            self._update_offline_task_status(info_hash, "自动整理中")
                            keep_task = self._trigger_transfer(task_name, task_info, info_hash=info_hash)
                            if not keep_task:
                                completed_keys.append(info_hash)
                        continue
                    add_time = self._safe_int(task_info.get("add_time", 0), 0)
                    if add_time and now - add_time > self._offline_task_timeout:
                        expired_keys.append(info_hash)
                        logger.warning(f"115离线监控超时，已移出队列: {task_info.get('title') or info_hash}")
                    continue

                task_name = self._get_task_name(task, task_info.get("name") or task_info.get("title") or task_info.get("display_title") or "")
                task_folder_cid, actual_folder_name = self._find_download_folder_by_task(client, task_name, task)
                if actual_folder_name:
                    task_name = actual_folder_name

                if self._is_offline_completed(task):
                    if not self._auto_transfer:
                        completed_keys.append(info_hash)
                    logger.info(f"115离线下载完成: {task_name}")
                    self._update_offline_task_status(info_hash, "离线完成", folder_cid=task_folder_cid, name=task_name)
                    self._record_offline_history(task_name, True)
                    # 自动整理模式下跳过此通知，由 _trigger_transfer 统一发"重命名完成"
                    if not self._auto_transfer:
                        _session = task_info.get("session") or {}
                        self.post_message(
                            title="115离线下载完成",
                            text=f"**{task_name}** 已离线下载完成",
                            mtype=NotificationType.Plugin,
                            channel=self._convert_channel(_session.get("channel")),
                            userid=_session.get("userid"),
                        )
                    if self._auto_transfer:
                        self._update_offline_task_status(info_hash, "自动整理中")
                        transfer_result = self._trigger_transfer(task_name, task, info_hash=info_hash)
                        # 如果需要智能体处理，发消息给智能体
                        if isinstance(transfer_result, dict) and transfer_result.get("needs_agent"):
                            _tsession = task_info.get("session") or {}
                            self._notify_agent(
                                transfer_result.get("agent_message", ""),
                                channel=_tsession.get("channel"),
                                source="clsearch",
                                userid=_tsession.get("userid", "clsearch"),
                                username=_tsession.get("username", "clsearch"),
                            )
                        # 整理完成或失败都移出监控队列（失败已记录为待处理）
                        completed_keys.append(info_hash)

                elif self._is_offline_failed(task):
                    completed_keys.append(info_hash)
                    logger.error(f"115离线下载失败: {task_name}")
                    self._record_offline_history(task_name, False, "状态码 -1")
                    _session = task_info.get("session") or {}
                    self.post_message(
                        title="115离线下载失败",
                        text=f"**{task_name}** 离线下载失败",
                        mtype=self._notification_type("Warning"),
                        channel=self._convert_channel(_session.get("channel")),
                        userid=_session.get("userid"),
                    )

            if completed_keys or expired_keys:
                with self._task_lock:
                    for k in completed_keys:
                        self._pending_tasks.pop(k, None)
                    for k in expired_keys:
                        self._pending_tasks.pop(k, None)
                self._save_offline_pending_tasks()

        except Exception as e:
            logger.error(f"115离线任务状态检查异常: {e}")
            self.post_message(
                title="115离线轮询异常",
                text=f"检查115离线任务状态失败: {e}",
                mtype=self._notification_type("Warning"),
            )


    def _convert_channel(self, channel):
        """将字符串channel转换为MessageChannel枚举

        MoviePilotTool 的 self._channel 是字符串（如 'WebAgent'），
        但 MessageChain.handle_message() 和 post_message() 需要 MessageChannel 枚举。
        """
        from app.schemas.types import MessageChannel
        if channel is None:
            return MessageChannel.Web
        if isinstance(channel, MessageChannel):
            return channel
        # 先按枚举名查找（如 'WebAgent' -> MessageChannel.WebAgent）
        try:
            return MessageChannel[channel]
        except (KeyError, ValueError):
            pass
        # 再按枚举值查找（如 '微信' -> MessageChannel.Wechat）
        try:
            return MessageChannel(channel)
        except (KeyError, ValueError):
            pass
        logger.warning(f"未知消息渠道: {channel}, 使用Web默认")
        return MessageChannel.Web

    def _notify_agent(self, message: str, channel=None, source: str = "clsearch", userid: str = "clsearch", username: str = "clsearch") -> bool:
        """主动发送消息给MP智能体处理

        优先用 MP 内部 MessageChain.handle_message()（进程内调用，无需 token），
        HTTP API 作为兜底。
        """
        channel = self._convert_channel(channel)
        # 优先用 MP 内部方法
        try:
            from app.chain.message import MessageChain
            logger.info(f"通过MessageChain发送消息给智能体: text={message[:100]}..., channel={channel}")
            MessageChain().handle_message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                text=message,
            )
            logger.info("MessageChain消息发送成功")
            return True
        except Exception as e:
            logger.warning(f"MessageChain发送失败，尝试HTTP API兜底: {e}")

        # HTTP API 兜底（用 /api/v1/message/ 接口，API token 可用）
        try:
            token = self._get_mp_api_token()
            headers = {"X-API-KEY": token, "Authorization": f"Bearer {token}"} if token else {}
            data = {"text": message, "source": source, "channel": channel.name if channel else None, "userid": userid, "username": username}
            last_error = None
            for base_url in self._get_mp_api_base_urls():
                url = f"{base_url}/api/v1/message/"
                try:
                    logger.info(f"发送消息给智能体(HTTP): url={url}, text={message[:100]}...")
                    resp = requests.post(url, headers=headers, json=data, timeout=30)
                    logger.info(f"智能体消息响应: status={resp.status_code}, body={resp.text[:500]}")
                    resp.raise_for_status()
                    result = resp.json() if resp.text else {}
                    if isinstance(result, dict) and result.get("success"):
                        logger.info("智能体消息发送成功(HTTP)")
                        return True
                except Exception as e:
                    last_error = e
                    logger.warning(f"发送智能体消息失败(HTTP): {url}, {e}")
                    continue
            logger.error(f"智能体消息发送失败: {last_error}")
            return False
        except Exception as e:
            logger.error(f"发送智能体消息异常: {e}")
            return False

    def _record_pending_task(self, task_name: str, file_path: Optional[str], extra_info: Optional[dict]) -> None:
        """记录待整理任务到插件数据"""
        pending_tasks = self.get_data("pending_transfer_tasks") or []

        # 检查是否已存在
        for index, t in enumerate(pending_tasks):
            if t.get("task_name") == task_name:
                pending_tasks.pop(index)
                logger.info(f"待整理任务已存在，将更新记录: {task_name}")
                break

        default_path = self._join_u115_path(self._resolved_path, task_name) if self._resolved_path else task_name
        new_task = {
            "file_path": file_path or default_path,
            "task_name": task_name,
            "storage": "u115",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "needs_agent_transfer": True,
        }
        if extra_info:
            new_task.update(extra_info)

        pending_tasks.append(new_task)
        self.save_data("pending_transfer_tasks", pending_tasks)
        logger.info(f"待整理任务已记录: {task_name}，当前待处理: {len(pending_tasks)} 个")

    def get_pending_transfer_tasks(self) -> List[dict]:
        """获取待整理任务列表

        Returns:
            待整理任务列表
        """
        return self.get_data("pending_transfer_tasks") or []

    def clear_pending_transfer_task(self, source_path: str) -> bool:
        """清除已处理的待整理任务

        Args:
            source_path: 源文件路径或任务名

        Returns:
            是否成功
        """
        try:
            pending_tasks = self.get_data("pending_transfer_tasks") or []
            new_tasks = [
                t for t in pending_tasks
                if t.get("file_path") != source_path
                and t.get("task_name") != source_path
            ]
            self.save_data("pending_transfer_tasks", new_tasks)
            return True
        except Exception as e:
            logger.error(f"清除待整理任务异常: {e}")
            return False

    # ==================== 历史记录 ====================

    def _record_search_history(self, keyword: str, count: int) -> None:
        """记录搜索历史"""
        self._reload_history_from_storage()
        self._search_history.insert(0, {
            "keyword": keyword,
            "count": count,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self._search_history = self._search_history[:20]
        self.save_data(self._search_history_data_key, self._search_history)
        logger.info(f"搜索历史已记录: {keyword}, 结果 {count} 条")

    def _record_offline_history(self, title: str, success: bool, error: str = "") -> None:
        """记录离线下载历史"""
        self._reload_history_from_storage()
        self._offline_history.insert(0, {
            "title": title,
            "success": success,
            "error": error,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self._offline_history = self._offline_history[:20]
        self.save_data(self._offline_history_data_key, self._offline_history)
        logger.info(f"离线下载历史已记录: {title}, success={success}, error={error or '无'}")

    def _send_search_results(self, keyword: str, results: List[dict]) -> None:
        """发送搜索结果通知，附带 detail_path 信息供智能体直接走115离线下载"""
        content_parts = [f"**搜索结果: {keyword}**\n"]

        for i, item in enumerate(results[:10], 1):
            title = item.get("title", "")
            size = item.get("size", "")
            seeders = item.get("seeders", "")
            update_time = item.get("update_time", "")
            detail_path = item.get("detail_path", "")

            content_parts.append(
                f"{i}. **{title}**\n"
                f"   大小: {size} | 做种: {seeders} | 更新: {update_time}\n"
                f"   detail_path: {detail_path}"
            )

        if len(results) > 10:
            content_parts.append(f"\n... 还有 {len(results) - 10} 个结果")

        content_parts.append(
            "\n---\n"
            "用户使用本插件搜索即表示要将资源添加到115离线下载。"
            "请调用 cl_search_offline_result 工具，传入用户选择的序号对应的 detail_path，自动完成离线。"
        )

        self.post_message(
            title="观影搜结果",
            text="\n".join(content_parts),
            mtype=NotificationType.Plugin,
        )

    def get_agent_tools(self) -> List[type]:
        """获取插件智能体工具，供内置AI智能体调用"""
        return [
            ClSearchSearchTool,
            ClSearchDetailTool,
            ClSearchOfflineResultTool,
            ClSearchOfflineTool,
            ClSearchListOfflineTool,
            ClSearchOrganizeOfflineTool,
            ClSearchRenameTool,
            ClSearchMoveFileTool,
            ClSearchCompleteTaskTool,
        ]

    # ==================== 整理模块委托 ====================

    @property
    def _organizer(self) -> OfflineOrganizer:
        organizer = getattr(self, "_offline_organizer", None)
        if organizer is None:
            organizer = OfflineOrganizer(self)
            self._offline_organizer = organizer
        return organizer

    def list_offline_entries(self, keyword: str = "") -> dict:
        """列出115离线目录内容，供智能体和API调用。"""
        return self._organizer.list_offline_entries(keyword)

    def organize_offline(self, folder_name: str = "", cid: str = "", target_path: str = "", move: bool = False) -> dict:
        """整理离线目录中的指定文件夹，供智能体和API调用。"""
        return self._organizer.organize_offline(folder_name, cid, target_path, move)

    def move_offline_folder(self, cid: str, target_path: str, task_name: str = "") -> dict:
        """移动离线文件夹到入库目录。"""
        return self._organizer.move_folder(cid, target_path, task_name)

    def _api_recursive_rename(self, data: dict = None) -> dict:
        return self._organizer._api_recursive_rename(data)

    def _api_mp_native_recursive_rename(self, data: dict = None) -> dict:
        return self._organizer._api_mp_native_recursive_rename(data)

    def _api_create_folder(self, data: dict) -> dict:
        return self._organizer._api_create_folder(data)

    def _api_rename(self, data: dict) -> dict:
        return self._organizer._api_rename(data)

    def _api_move_file(self, data: dict) -> dict:
        return self._organizer._api_move_file(data)

    def _api_delete_files(self, data: dict) -> dict:
        return self._organizer._api_delete_files(data)

    def _api_list_files(self, data: dict = None) -> dict:
        return self._organizer._api_list_files(data)

    def _find_cid_by_path(self, target_path: str):
        return self._organizer._find_cid_by_path(target_path)

    def _find_download_folder_cid(self, client, folder_name: str):
        return self._organizer._find_download_folder_cid(client, folder_name)

    def _find_download_folder_by_task(self, client, task_name: str = "", task: dict = None):
        return self._organizer._find_download_folder_by_task(client, task_name, task)

    def _get_cid_name(self, cid: str) -> str:
        return self._organizer._get_cid_name(cid)

    def _trigger_transfer(self, task_name: str, task: dict, info_hash: str = "", notify: bool = True) -> dict:
        return self._organizer._trigger_transfer(task_name, task, info_hash, notify)

    def _api_list_offline(self, keyword: str = "", **kwargs) -> dict:
        return self.list_offline_entries(keyword)

    def _api_organize_offline(self, data: dict = None, folder_name: str = "", cid: str = "", target_path: str = "", move: bool = False) -> dict:
        data = data or {}
        move_value = data.get("move") if "move" in data else move
        move_flag = move_value if isinstance(move_value, bool) else str(move_value).lower() in ("1", "true", "yes", "on")
        return self.organize_offline(
            folder_name=str(data.get("folder_name") or data.get("name") or folder_name or ""),
            cid=str(data.get("cid") or cid or ""),
            target_path=str(data.get("target_path") or target_path or ""),
            move=move_flag,
        )

    def _api_move_offline(self, data: dict = None, cid: str = "", source_cid: str = "", target_path: str = "", task_name: str = "") -> dict:
        data = data or {}
        return self.move_offline_folder(
            cid=str(data.get("cid") or data.get("source_cid") or cid or source_cid or ""),
            target_path=str(data.get("target_path") or target_path or ""),
            task_name=str(data.get("task_name") or task_name or ""),
        )

    def stop_service(self) -> None:
        """停止插件服务"""
        global _CLSEARCH_ACTIVE_PLUGIN
        self._stop_polling()
        with self._task_lock:
            has_pending = bool(self._pending_tasks)
        if has_pending:
            self._save_offline_pending_tasks()
        with self._task_lock:
            self._pending_tasks.clear()
        self._search_cache.clear()
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        if _CLSEARCH_ACTIVE_PLUGIN is self:
            _CLSEARCH_ACTIVE_PLUGIN = None
        logger.info("观影搜插件服务已停止")
