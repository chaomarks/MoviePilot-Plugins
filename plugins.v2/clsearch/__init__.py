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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type
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
from pydantic import BaseModel, Field

from app.core.event import Event, EventManager, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.agent.tools.base import MoviePilotTool
from app.schemas.types import MediaType as MMediaType


class ClSearch(_PluginBase):
    """观影磁力搜插件"""

    # 插件名称
    plugin_name = "观影磁力搜"
    # 插件描述
    plugin_desc = "搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    # 插件版本
    plugin_version = "1.5.6"
    # 插件作者
    plugin_author = "chaomarks"
    # 作者主页
    author_url = "https://github.com/jxxghp/MoviePilot"
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

        if not config:
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

        # 恢复仍在等待115完成的离线任务；待智能体整理记录不再混入轮询队列。
        saved_search_history = self.get_data(self._search_history_data_key) or []
        if isinstance(saved_search_history, list):
            self._search_history = [item for item in saved_search_history if isinstance(item, dict)][:20]
        saved_offline_history = self.get_data(self._offline_history_data_key) or []
        if isinstance(saved_offline_history, list):
            self._offline_history = [item for item in saved_offline_history if isinstance(item, dict)][:20]

        saved_offline_pending = self.get_data(self._offline_pending_data_key) or {}
        if isinstance(saved_offline_pending, dict):
            self._pending_tasks = {str(k).lower(): v for k, v in saved_offline_pending.items() if isinstance(v, dict)}
        elif isinstance(saved_offline_pending, list):
            dropped_count = 0
            self._pending_tasks = {}
            for t in saved_offline_pending:
                if isinstance(t, dict) and (t.get("info_hash") or t.get("hash")):
                    self._pending_tasks[str(t.get("info_hash") or t.get("hash")).lower()] = t
                else:
                    dropped_count += 1
            if dropped_count:
                logger.warning(f"恢复离线监控任务时跳过 {dropped_count} 条无效记录")
        else:
            self._pending_tasks = {}
        if self._pending_tasks and self._enabled:
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
                "path": "/ClSearch/task/close",
                "endpoint": self._api_close_task,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "关闭插件首页任务记录",
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
                                        "text": "（此插件仅通过智能体工具调用）",
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
                    # 目录CID + 解析路径（同一行）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
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
                                "props": {"cols": 12, "md": 6},
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
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页，展示搜索和离线历史记录"""
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
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(len(search_items))},
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
                                            {"component": "div", "props": {"class": "text-h5 font-weight-bold"}, "text": str(sum(1 for r in offline_items if r.get("success")))},
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
                                        "text": "清空本地任务",
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
                    # 搜索历史
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": f"搜索历史（{len(search_items)}条）",
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-0"},
                                "content": [{
                                    "component": "VDataTable",
                                    "props": {
                                        "headers": [
                                            {"title": "关键词", "key": "keyword"},
                                            {"title": "结果数", "key": "count", "align": "center"},
                                            {"title": "时间", "key": "time", "align": "end"},
                                        ],
                                        "items": search_items,
                                        "items-per-page": 10,
                                        "hover": True,
                                        "density": "compact",
                                        "hide-default-footer": len(search_items) <= 10,
                                    },
                                }] if search_items else [{
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
                                "props": {"class": "text-subtitle-1"},
                                "text": f"离线下载历史（{len(offline_items)}条）",
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "pa-0"},
                                "content": [{
                                    "component": "VDataTable",
                                    "props": {
                                        "headers": [
                                            {"title": "标题", "key": "title"},
                                            {"title": "状态", "key": "status", "align": "center"},
                                            {"title": "时间", "key": "time", "align": "end"},
                                        ],
                                        "items": [
                                            {
                                                "title": r["title"],
                                                "status": "✅ 成功" if r.get("success") else f"❌ {r.get('error', '失败')}",
                                                "time": r["time"],
                                            }
                                            for r in offline_items
                                        ],
                                        "items-per-page": 10,
                                        "hover": True,
                                        "density": "compact",
                                        "hide-default-footer": len(offline_items) <= 10,
                                    },
                                }] if offline_items else [{
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

    def _persist_site_cookie(self) -> None:
        """将自动登录获得的 Cookie 写回配置，避免重复登录。"""
        if not self._site_cookie:
            return
        try:
            config = {}
            if hasattr(self, "_config") and isinstance(getattr(self, "_config"), dict):
                config = dict(getattr(self, "_config"))
            config["site_cookie"] = self._site_cookie
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

    def _find_download_folder_cid(self, client: P115Client, folder_name: str) -> Optional[str]:
        """在离线下载目录中查找指定文件夹，供轮询和整理共同使用。"""
        folder_name = str(folder_name or "").strip()
        if not folder_name or not self._save_dir_id:
            return None
        offset = 0
        while True:
            result = client.fs_files({"cid": self._save_dir_id, "limit": 1000, "offset": offset})
            items = result.get("data") if isinstance(result, dict) else None
            if not items:
                return None
            for item in items:
                item_name = str(item.get("n") or item.get("name") or "").strip()
                item_cid = item.get("cid")
                if item_cid and item_name == folder_name:
                    return str(item_cid)
            if len(items) < 1000:
                return None
            offset += 1000

    def _find_download_folder_by_task(self, client: P115Client, task_name: str = "", task: dict = None) -> Tuple[str, str]:
        """根据任务信息定位115实际下载目录，返回 (cid, 实际目录名)。"""
        candidates: List[Tuple[str, str]] = []
        if isinstance(task, dict):
            task_cid = self._get_task_folder_cid(task)
            task_display_name = self._get_task_name(task, task_name).strip()
            if task_cid:
                cid_name = self._get_cid_name(task_cid) or task_display_name or task_name
                return str(task_cid), cid_name
            if task_display_name:
                candidates.append((task_display_name, "任务返回名称"))
        if task_name:
            candidates.append((task_name, "原始任务名"))

        seen = set()
        for candidate, source in candidates:
            candidate = str(candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            folder_cid = self._find_download_folder_cid(client, candidate)
            if folder_cid:
                logger.info(f"通过{source}找到下载目录: {candidate}, CID: {folder_cid}")
                return folder_cid, candidate
        return "", ""

    def _get_cid_name(self, cid: str) -> str:
        """通过115目录CID获取当前目录名。"""
        if not cid or not self._p115_cookie:
            return ""
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            resp = requests.get(
                "https://webapi.115.com/files",
                params={"aid": 1, "cid": cid, "show_dir": 1, "limit": 1},
                headers={
                    "Cookie": p115_cookie,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://115.com/",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("state"):
                logger.warning(f"115 CID目录名查询失败: {data}")
                return ""
            path_list = data.get("path", []) if isinstance(data, dict) else []
            if path_list:
                return str(path_list[-1].get("name") or "")
        except Exception as e:
            logger.warning(f"115 CID目录名查询异常: {e}")
        return ""

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

    def _api_create_folder(self, data: dict) -> dict:
        """创建文件夹（优先使用 StorageChain.create_folder，p115client 兜底）

        Args:
            data: {"cid": 父目录CID, "name": 新文件夹名称, "path": 父目录路径}

        Returns:
            创建结果
        """
        parent_cid = str(data.get("cid", "0"))
        folder_name = str(data.get("name", ""))
        parent_path = str(data.get("path", ""))

        # 优先 StorageChain.create_folder
        try:
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            parent_fileitem = FileItem(
                storage="u115",
                fileid=parent_cid,
                type="dir",
                name="",
                path=parent_path,
            )
            logger.info(f"StorageChain.create_folder: parent_cid={parent_cid}, name={folder_name}")
            result = StorageChain().create_folder(parent_fileitem, folder_name)
            logger.info(f"StorageChain.create_folder响应: {result}")

            # create_folder 可能返回 None 但实际创建成功，用 list_files 找回 CID
            new_cid = ""
            if isinstance(result, FileItem):
                new_cid = str(getattr(result, "fileid", "") or "")
            elif isinstance(result, dict) and result:
                new_cid = str(result.get("fileid", "") or result.get("data", {}).get("cid", ""))

            if not new_cid:
                # 返回值为 None，尝试用 list_files 找到刚创建的目录
                logger.info(f"create_folder返回None，尝试list_files查找已创建的目录: {folder_name}")
                try:
                    items = StorageChain().list_files(parent_fileitem)
                    if items:
                        for item in items:
                            item_name = getattr(item, "name", "") or ""
                            item_type = getattr(item, "type", "") or ""
                            if item_type == "dir" and item_name == folder_name:
                                new_cid = str(getattr(item, "fileid", "") or "")
                                logger.info(f"list_files找到目录: {folder_name}, CID={new_cid}")
                                break
                except Exception as e:
                    logger.warning(f"list_files查找目录异常: {e}")

            if new_cid:
                return {"success": True, "message": "文件夹创建成功", "data": {"cid": new_cid}}
            # create_folder 没报异常但没找到 CID，认为目录已存在（不重复创建）
            logger.info(f"create_folder未返回CID，目录可能已存在: {folder_name}")
            return {"success": True, "message": "文件夹已存在", "data": {"cid": ""}}
        except Exception as e:
            logger.warning(f"StorageChain.create_folder失败，尝试p115client: {e}")

        # 兜底 p115client
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            payload = {"pid": parent_cid, "file_name": folder_name}
            logger.info(f"P115Client.fs_mkdir: {payload}")
            result = client.fs_mkdir(payload)
            logger.info(f"P115Client.fs_mkdir响应: {result}")
            if result.get("state"):
                folder_cid = result.get("data", {}).get("cid") or result.get("cid") or result.get("file_id")
                if folder_cid:
                    return {"success": True, "message": "文件夹创建成功", "data": {"cid": str(folder_cid)}}
            return {"success": False, "message": result.get("message", "创建文件夹失败")}
        except Exception as e:
            return {"success": False, "message": f"创建文件夹失败: {str(e)}"}

    def _api_rename(self, data: dict) -> dict:
        """重命名文件或文件夹（优先使用 StorageChain.rename_file，p115client 兜底）

        Args:
            data: {"cid": 文件/文件夹CID, "name": 新名称, "path": 文件路径, "old_name": 旧名称, "type": 文件类型}

        Returns:
            重命名结果
        """
        file_cid = str(data.get("cid", ""))
        new_name = str(data.get("name", ""))
        file_path = str(data.get("path", ""))
        old_name = str(data.get("old_name", ""))
        file_type = str(data.get("type", "file"))

        # 优先 StorageChain.rename_file
        try:
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            fileitem = FileItem(
                storage="u115",
                fileid=file_cid,
                type=file_type,
                name=old_name,
                path=file_path,
            )
            logger.info(f"StorageChain.rename_file: cid={file_cid}, {old_name} -> {new_name}")
            result = StorageChain().rename_file(fileitem, new_name)
            logger.info(f"StorageChain.rename_file响应: {result}")
            if result is not False and result is not None:
                return {"success": True, "message": "重命名成功"}
        except Exception as e:
            logger.warning(f"StorageChain.rename_file失败，尝试p115client: {e}")

        # 兜底 p115client
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            payload = {"cid": file_cid, "name": new_name}
            result = client.fs_rename(payload)
            if result.get("state"):
                return {"success": True, "message": "重命名成功"}
            return {"success": False, "message": result.get("message", "重命名失败")}
        except Exception as e:
            return {"success": False, "message": f"重命名失败: {str(e)}"}

    def _api_move_file(self, data: dict) -> dict:
        """移动文件到指定文件夹（优先使用 StorageChain，p115client 兜底）

        Args:
            data: {"cid": 源文件CID, "target_cid": 目标文件夹CID, "path": 源文件路径, "name": 源文件名, "target_path": 目标路径}

        Returns:
            移动结果
        """
        file_cid = str(data.get("cid", ""))
        target_cid = str(data.get("target_cid", ""))
        file_path = str(data.get("path", ""))
        file_name = str(data.get("name", ""))
        target_path = str(data.get("target_path", ""))
        file_type = str(data.get("type", "file"))

        # StorageChain 没有 move 方法，直接用 p115client
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            # fs_move 参数: file_ids 可以是 int/str/逗号分隔字符串, to_cid 是目标目录id
            # 优先用 pid 参数传目标目录
            logger.info(f"P115Client.fs_move: file_ids={file_cid}, to_cid(pid)={target_cid}")
            result = client.fs_move(str(file_cid), pid=str(target_cid))
            logger.info(f"P115Client.fs_move响应: {result}")
            if result.get("state"):
                return {"success": True, "message": "文件移动成功"}
            return {"success": False, "message": result.get("error", "文件移动失败")}
        except Exception as e:
            return {"success": False, "message": f"文件移动失败: {str(e)}"}

    def _api_delete_files(self, data: dict) -> dict:
        """删除指定文件

        Args:
            data: {"cids": 文件CID列表}

        Returns:
            删除结果
        """
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            cids = data.get("cids", [])
            if not cids:
                return {"success": False, "message": "未指定要删除的文件"}

            result = client.fs_delete(cids)

            if result.get("state"):
                return {"success": True, "message": f"文件删除成功"}
            return {"success": False, "message": result.get("message", "文件删除失败")}
        except Exception as e:
            return {"success": False, "message": f"文件删除失败: {str(e)}"}

    def _api_list_files(self, data: dict = None) -> dict:
        """获取文件夹内的文件和子文件夹列表

        Args:
            data: {"cid": 文件夹CID, "limit": 限制数量, "offset": 偏移量}

        Returns:
            文件列表结果
        """
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            payload = {
                "cid": str(data.get("cid", "0")) if data else "0",
            }
            if data:
                payload["limit"] = self._safe_int(data.get("limit", 100), 100, minimum=1, maximum=1000)
                payload["offset"] = self._safe_int(data.get("offset", 0), 0, minimum=0)

            result = client.fs_files(payload)

            if isinstance(result, dict) and result.get("state"):
                files = result.get("data", [])
                return {"success": True, "data": files if isinstance(files, list) else []}
            return {"success": False, "message": result.get("message", "获取文件列表失败")}
        except Exception as e:
            return {"success": False, "message": f"获取文件列表失败: {str(e)}"}



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
            self.post_message(
                title="115\u79bb\u7ebf\u4efb\u52a1\u5df2\u5b58\u5728",
                content=f"**{title}** \u5df2\u5728\u79bb\u7ebf\u76d1\u63a7\u961f\u5217\u4e2d\uff0c\u5df2\u7ed3\u675f\u672c\u6b21\u91cd\u590d\u63d0\u4ea4\u3002\ninfo_hash: `{local_info_hash}`",
                notification_type=NotificationType.Plugin,
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
                            try:
                                transfer_result = self._trigger_transfer(task_name_for_transfer, task_for_transfer, info_hash=duplicate_hash)
                                if isinstance(transfer_result, dict) and transfer_result.get("needs_agent"):
                                    self._notify_agent(transfer_result.get("agent_message", ""))
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
                self.post_message(
                    title="115离线任务已存在",
                    content=f"**{display_title}** 已在115离线任务中，已跳过重复添加。" + (f"\ninfo_hash: `{tracked_hash}`" if tracked_hash else ""),
                    notification_type=NotificationType.Plugin,
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



    def _api_recursive_rename(self, data: dict = None) -> dict:
        """API: 重命名115目录 + 递归重命名内部文件 + 创建Season子目录。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        data = data or {}
        cid = str(data.get("cid") or data.get("fileid") or "").strip()
        file_path = self._join_u115_path(str(data.get("path") or data.get("file_path") or "").strip())
        new_name = str(data.get("new_name") or data.get("name") or "").strip()
        old_name = self._u115_path_name(file_path) or str(data.get("old_name") or "").strip()
        media_type = str(data.get("media_type") or "").strip().lower()
        title = str(data.get("title") or "").strip()
        # 取消标志的 key 用 info_hash（与 _api_close_task 设置时一致）
        cancel_key = str(data.get("info_hash") or "").strip().lower()

        if not new_name:
            return {"success": False, "message": "缺少新名称 new_name"}
        if not cid:
            return {"success": False, "message": "缺少115目录CID，无法重命名"}
        if not old_name:
            old_name = str(data.get("task_name") or cid).strip()

        is_tv = media_type in ("tv", "show", "电视剧", "series")
        # 用于文件名的基础标题：去掉年份和tmdbid后的纯标题
        file_title = title or new_name
        # 去掉 (年份) 和 [tmdbid=...] 只保留标题部分
        file_title = re.sub(r'\s*\(\d{4}\)\s*', '', file_title)
        file_title = re.sub(r'\s*\[tmdbid=\d+\]\s*', '', file_title).strip()
        if not file_title:
            file_title = new_name

        # ============ Step 1: 重命名顶层文件夹 ============
        folder_renamed = False
        folder_error = None

        # 优先 MP 内部 StorageChain.rename_file
        try:
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            fileitem_payload = {
                "storage": str(data.get("storage") or "u115").lower(),
                "fileid": cid,
                "type": "dir",
                "name": old_name,
                "path": file_path,
            }
            fileitem = FileItem(**fileitem_payload)
            logger.info(f"准备重命名目录: {old_name} -> {new_name}, cid={cid}")
            storage_result = StorageChain().rename_file(fileitem, new_name)
            logger.info(f"StorageChain.rename_file响应: {storage_result}")
            if isinstance(storage_result, dict):
                if storage_result.get("success") is not False and storage_result.get("state") is not False:
                    folder_renamed = True
                else:
                    folder_error = json.dumps(storage_result, ensure_ascii=False)[:500]
            elif storage_result is not False:
                folder_renamed = True
            else:
                folder_error = "rename_file 返回 False"
        except Exception as e:
            folder_error = f"{type(e).__name__}: {e}"
            logger.warning(f"StorageChain.rename_file失败，尝试115 fs_rename: {folder_error}")

        # 兜底 115 fs_rename
        if not folder_renamed:
            try:
                p115_cookie = self._normalize_cookie(self._p115_cookie)
                client = P115Client(p115_cookie)
                result = client.fs_rename({"cid": cid, "name": new_name})
                logger.info(f"115 fs_rename响应: {result}")
                if isinstance(result, dict) and result.get("state"):
                    folder_renamed = True
                else:
                    folder_error = f"fs_rename: {result}"
            except Exception as e:
                folder_error = f"fs_rename异常: {e}"

        if not folder_renamed:
            return {"success": False, "message": f"目录重命名失败: {folder_error}", "files": []}

        logger.info(f"目录重命名成功: {old_name} -> {new_name}")

        # ============ Step 2: 遍历内部文件并重命名 ============
        count = 0
        created_dirs = 0
        file_status_list = []

        try:
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            storage_chain = StorageChain()
            storage_name = str(data.get("storage") or "u115").lower()

            # 用 StorageChain.list_files 列出顶层目录
            top_fileitem = FileItem(
                storage=storage_name,
                fileid=cid,
                type="dir",
                name=old_name,
                path=file_path,
            )
            logger.info(f"准备列出目录文件: cid={cid}, path={file_path}")
            top_items = storage_chain.list_files(top_fileitem)
            if not top_items:
                logger.info(f"目录内无文件需要重命名: {new_name}")
                return {
                    "success": True,
                    "message": f"目录重命名成功: {new_name}（内部无文件）",
                    "files": [],
                    "renamed": 0,
                    "created_dirs": 0,
                }

            # 递归收集所有层级的视频文件
            video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.rmvb', '.rm', '.iso', '.vob', '.mpg', '.mpeg', '.m4v', '.3gp', '.f4v')
            all_media_files = []
            top_sub_folders = []
            visited_cids = set()

            def _collect_files(fileitem: FileItem, depth: int = 0, parent_path: str = ""):
                """递归收集目录内所有视频文件。"""
                # 检查插件是否还在运行
                if not self._enabled or (self._polling_stop and self._polling_stop.is_set()):
                    logger.info("插件已停止，中断递归收集")
                    return
                # 防止重复访问同一个目录
                item_cid = str(fileitem.fileid or "")
                if item_cid in visited_cids:
                    return
                visited_cids.add(item_cid)
                try:
                    items = storage_chain.list_files(fileitem)
                    if not items:
                        return
                    for item in items:
                        # 检查插件状态
                        if not self._enabled or (self._polling_stop and self._polling_stop.is_set()):
                            return
                        item_type = str(getattr(item, "type", "") or "")
                        item_name = getattr(item, "name", "") or ""
                        item_fileid = str(getattr(item, "fileid", "") or "")
                        if item_type == "dir":
                            if depth == 0:
                                top_sub_folders.append(item)
                            _collect_files(item, depth + 1, f"{parent_path}{item_name}/")
                        else:
                            file_ext = os.path.splitext(item_name)[1].lower()
                            if file_ext in video_exts:
                                item._parent_path = parent_path
                                all_media_files.append(item)
                except Exception as e:
                    logger.error(f"递归收集文件异常(cid={item_cid}): {e}")

            _collect_files(top_fileitem)

            logger.info(f"递归收集完成: 视频文件 {len(all_media_files)} 个, 顶层子目录 {len(top_sub_folders)} 个")

            # 缓存 Season 目录 CID，避免重复创建
            season_dir_cache = {}  # {season_number: cid}

            for file_item in all_media_files:
                # 检查插件是否还在运行
                if not self._enabled or (self._polling_stop and self._polling_stop.is_set()):
                    logger.info("插件已停止，中断文件处理")
                    break
                # 检查取消标志
                if cancel_key and self._cancel_flags.get(cancel_key):
                    logger.info(f"任务已取消，中断文件处理: info_hash={cancel_key}")
                    break

                file_cid = str(getattr(file_item, "fileid", "") or "")
                if not file_cid:
                    continue

                original_name = getattr(file_item, "name", "") or ""
                file_ext = os.path.splitext(original_name)[1].lower()

                if file_ext not in video_exts:
                    file_status_list.append({"old_name": original_name, "new_name": original_name, "status": "跳过（非视频）"})
                    continue

                # 检测 SxxExx
                season_match = re.search(r'[Ss](\d+)[Ee](\d+)', original_name)
                # 也检测单独的 Exx 格式
                ep_match = re.search(r'[Ee](\d{2,3})\b', original_name) if not season_match else None

                if season_match:
                    season = int(season_match.group(1))
                    episode = int(season_match.group(2))
                elif ep_match and is_tv:
                    season = 1
                    episode = int(ep_match.group(1))
                else:
                    file_status_list.append({"old_name": original_name, "new_name": original_name, "status": "跳过（无剧集信息）"})
                    logger.info(f"跳过文件（无剧集信息）: {original_name}")
                    continue

                new_file_name = f"{file_title} - S{season:02d}E{episode:02d} - 第 {episode} 集{file_ext}"
                # 115 文件名限制 255 字节，超长才截断标题
                if len(new_file_name.encode('utf-8')) > 255:
                    base = f" - S{season:02d}E{episode:02d} - 第 {episode} 集{file_ext}"
                    max_title_bytes = 255 - len(base.encode('utf-8'))
                    if max_title_bytes > 0:
                        encoded = file_title.encode('utf-8')[:max_title_bytes]
                        while encoded and (encoded[-1] & 0xC0) == 0x80:
                            encoded = encoded[:-1]
                        new_file_name = encoded.decode('utf-8', errors='ignore') + base
                    else:
                        new_file_name = f"S{season:02d}E{episode:02d} - 第 {episode} 集{file_ext}"

                if is_tv:
                    # 电视剧：创建 Season 目录并移动文件
                    season_dir_name = f"Season {season}"
                    # 先从缓存取
                    season_folder_cid = season_dir_cache.get(season)

                    # 缓存没有，检查 Season 目录是否已存在
                    if not season_folder_cid:
                        for folder in top_sub_folders:
                            folder_name = getattr(folder, "name", "") or ""
                            if folder_name == season_dir_name:
                                season_folder_cid = str(getattr(folder, "fileid", "") or "")
                                season_dir_cache[season] = season_folder_cid
                                break

                    # 还没有，创建 Season 目录
                    if not season_folder_cid:
                        create_result = self._api_create_folder({"cid": cid, "name": season_dir_name, "path": file_path})
                        if create_result.get("success"):
                            season_folder_cid = create_result.get("data", {}).get("cid")
                            if season_folder_cid:
                                created_dirs += 1
                                logger.info(f"创建Season目录: {season_dir_name}, CID: {season_folder_cid}")
                                season_dir_cache[season] = season_folder_cid
                        # 如果没有CID，尝试用 list_files 查找已创建的目录
                        if not season_folder_cid:
                            try:
                                from app.chain.storage import StorageChain
                                from app.schemas import FileItem
                                parent_item = FileItem(storage="u115", fileid=cid, type="dir", name=new_name, path=file_path)
                                items = StorageChain().list_files(parent_item)
                                if items:
                                    for item in items:
                                        if getattr(item, "type", "") == "dir" and (getattr(item, "name", "") or "") == season_dir_name:
                                            season_folder_cid = str(getattr(item, "fileid", "") or "")
                                            created_dirs += 1
                                            season_dir_cache[season] = season_folder_cid
                                            logger.info(f"list_files找到Season目录: {season_dir_name}, CID: {season_folder_cid}")
                                            break
                            except Exception as e:
                                logger.warning(f"list_files查找Season目录异常: {e}")
                        if not season_folder_cid:
                            logger.error(f"创建Season目录失败: {create_result.get('message', '未知')}")

                    if season_folder_cid:
                        # 先移动到 Season 目录
                        move_result = self._api_move_file({
                            "cid": file_cid,
                            "target_cid": season_folder_cid,
                            "path": f"{file_path}/{original_name}",
                            "name": original_name,
                            "target_path": f"{file_path}/{season_dir_name}",
                            "type": "file",
                        })
                        if move_result.get("success"):
                            # 再重命名
                            rename_result = self._api_rename({
                                "cid": file_cid,
                                "name": new_file_name,
                                "path": f"{file_path}/{season_dir_name}/{original_name}",
                                "old_name": original_name,
                                "type": "file",
                            })
                            if rename_result.get("success"):
                                count += 1
                                file_status_list.append({"old_name": original_name, "new_name": new_file_name, "status": "已重命名"})
                                logger.info(f"移动到Season并改名: {original_name} -> {new_file_name}")
                            else:
                                file_status_list.append({"old_name": original_name, "new_name": new_file_name, "status": f"移动成功但改名失败: {rename_result.get('message')}"})
                        else:
                            file_status_list.append({"old_name": original_name, "new_name": new_file_name, "status": f"移动失败: {move_result.get('message')}"})
                    else:
                        # Season 目录创建失败，直接改名
                        rename_result = self._api_rename({
                            "cid": file_cid,
                            "name": new_file_name,
                            "path": f"{file_path}/{original_name}",
                            "old_name": original_name,
                            "type": "file",
                        })
                        if rename_result.get("success"):
                            count += 1
                            file_status_list.append({"old_name": original_name, "new_name": new_file_name, "status": "已重命名（无Season目录）"})
                else:
                    # 电影：直接重命名
                    rename_result = self._api_rename({
                        "cid": file_cid,
                        "name": new_file_name,
                        "path": f"{file_path}/{original_name}",
                        "old_name": original_name,
                        "type": "file",
                    })
                    if rename_result.get("success"):
                        count += 1
                        file_status_list.append({"old_name": original_name, "new_name": new_file_name, "status": "已重命名"})
                        logger.info(f"重命名文件: {original_name} -> {new_file_name}")

        except Exception as e:
            logger.error(f"递归重命名内部文件异常: {e}")
            return {
                "success": True,
                "message": f"目录重命名成功，但内部文件处理异常: {str(e)}",
                "files": file_status_list,
                "renamed": count,
                "created_dirs": created_dirs,
            }

        logger.info(f"递归重命名完成: 目录已改名, 文件 {count} 个, 创建Season目录 {created_dirs} 个")
        return {
            "success": True,
            "message": f"递归重命名完成: 目录已改名, 文件 {count} 个, 创建Season目录 {created_dirs} 个",
            "files": file_status_list,
            "renamed": count,
            "created_dirs": created_dirs,
        }


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
                content="请输入搜索关键词，例如: /clsearch 开端",
                notification_type=self._notification_type("Warning"),
            )
            return

        result = self._api_search(keyword)
        if result.get("success") and result.get("data"):
            self._send_search_results(keyword, result["data"])
        else:
            self.post_message(
                title="观影搜",
                content=result.get("message", "搜索失败"),
                notification_type=self._notification_type("Warning"),
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
                        self.post_message(
                            title="115离线下载完成",
                            content=f"**{task_name}** 已离线下载完成",
                            notification_type=NotificationType.Plugin,
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
                    self.post_message(
                        title="115离线下载完成",
                        content=f"**{task_name}** 已离线下载完成",
                        notification_type=NotificationType.Plugin,
                    )
                    if self._auto_transfer:
                        self._update_offline_task_status(info_hash, "自动整理中")
                        transfer_result = self._trigger_transfer(task_name, task, info_hash=info_hash)
                        # 如果需要智能体处理，发消息给智能体
                        if isinstance(transfer_result, dict) and transfer_result.get("needs_agent"):
                            self._notify_agent(transfer_result.get("agent_message", ""))
                        # 整理完成或失败都移出监控队列（失败已记录为待处理）
                        completed_keys.append(info_hash)

                elif self._is_offline_failed(task):
                    completed_keys.append(info_hash)
                    logger.error(f"115离线下载失败: {task_name}")
                    self._record_offline_history(task_name, False, "状态码 -1")
                    self.post_message(
                        title="115离线下载失败",
                        content=f"**{task_name}** 离线下载失败",
                        notification_type=self._notification_type("Warning"),
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
                content=f"检查115离线任务状态失败: {e}",
                notification_type=self._notification_type("Warning"),
            )

    def _trigger_transfer(self, task_name: str, task: dict, info_hash: str = "") -> dict:
        """自动整理：离线完成后调用内置rename API重命名文件，然后通知智能体处理广告删除和移动

        完整流程：
        1. 在115网盘的离线目录中查找下载完成的文件夹
        2. 识别媒体信息，确定规范的文件夹名（含tmdbID）
        3. 调用内置rename API原地重命名文件夹和内部文件（含创建Season子目录）
        4. 记录待整理任务，通知智能体处理广告删除和整体移动
        """
        try:
            if not self._resolved_path:
                logger.info(f"未解析离线目录路径，跳过自动整理: {task_name}")
                self._record_pending_task(task_name, None, {"error": "未解析离线目录路径"})
                self._update_offline_task_status(info_hash, "待处理：未解析离线目录路径")
                return {"success": False}

            logger.info(f"开始自动整理: {task_name}")

            # 检查取消标志（不pop，让取消标志保持到整理完成或中断后）
            if self._cancel_flags.get(info_hash):
                logger.info(f"任务已取消，中断整理: {task_name}")
                return {"success": False}

            if not self._p115_cookie:
                logger.info(f"未配置115 Cookie，跳过自动整理: {task_name}")
                self._record_pending_task(task_name, None, {"error": "未配置115 Cookie"})
                self._update_offline_task_status(info_hash, "待处理：未配置115 Cookie")
                return {"success": False}

            # 创建115客户端
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            # Step 1: 在离线目录中查找下载完成的文件夹，并回填115实际目录名
            folder_cid, actual_folder_name = self._find_download_folder_by_task(client, task_name, task)
            if actual_folder_name:
                task_name = actual_folder_name

            if not folder_cid:
                logger.info(f"未找到下载文件夹: {task_name}，记录为待整理任务")
                self._record_pending_task(task_name, None, None)
                self._update_offline_task_status(info_hash, "待处理：未找到下载文件夹")
                return {"success": False}

            logger.info(f"找到下载文件夹: {task_name}, CID: {folder_cid}")

            # Step 2: 识别媒体信息，确定规范的文件夹名
            from app.chain.media import MediaChain

            media_chain = MediaChain()

            virtual_path = Path(f"/{task_name}")
            context = media_chain.recognize_by_path(virtual_path, obtain_images=False)

            new_folder_name = task_name
            media_type = None
            title = None
            year = None
            tmdb_id = None

            if context and context.media_info:
                mediainfo = context.media_info
                meta = context.meta_info

                # 获取媒体类型
                media_type_val = getattr(mediainfo, 'type', None)
                if isinstance(media_type_val, MMediaType):
                    media_type = media_type_val.to_agent()
                else:
                    if hasattr(media_type_val, 'value'):
                        media_type_val = media_type_val.value
                    media_type = "tv" if str(media_type_val).lower() in ("tv", "show", "电视剧") else "movie"

                # 获取标题和年份
                title = getattr(mediainfo, 'title', None) or (meta and getattr(meta, 'title', None))
                year = getattr(mediainfo, 'year', None) or (meta and getattr(meta, 'year', None))
                tmdb_id = getattr(mediainfo, 'tmdb_id', None)

                # 格式化文件夹名：标题 (年份) [tmdbid=xxx]
                if tmdb_id:
                    year_str = f" ({year})" if year else ""
                    new_folder_name = f"{title}{year_str} [tmdbid={tmdb_id}]"
                elif year and title:
                    new_folder_name = f"{title} ({year})"
                elif title:
                    new_folder_name = title

                logger.info(f"媒体识别成功: {title} ({year}), TMDB ID: {tmdb_id}, 类型: {media_type}")
            else:
                logger.info(f"媒体识别失败，使用原始文件夹名: {task_name}")

            # Step 3: 用 StorageChain.rename_file + 115 API 完成重命名（文件夹+文件+Season目录）
            # 不走 transfer/manual，避免屏蔽词干扰
            # 检查取消标志
            if self._cancel_flags.get(info_hash):
                logger.info(f"任务已取消，中断整理（重命名前）: {task_name}")
                return {"success": False}
            source_path = self._join_u115_path(self._resolved_path, task_name)
            rename_result = self._api_recursive_rename(data={
                "cid": folder_cid,
                "path": source_path,
                "storage": "u115",
                "new_name": new_folder_name,
                "media_type": media_type,
                "title": title,
                "info_hash": info_hash,
            })
            logger.info(f"重命名结果: {rename_result.get('message', '未知')}")
            if not rename_result.get("success"):
                self.post_message(
                    title="115重命名失败",
                    content=(
                        f"**{task_name}** 重命名失败，已停止后续自动整理。\n"
                        f"CID: `{folder_cid}`\n"
                        f"错误: {rename_result.get('message', '未知错误')}"
                    ),
                    notification_type=self._notification_type("Warning"),
                )
                self._record_pending_task(task_name, source_path, {
                    "rename_result": rename_result.get("message"),
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "tmdb_id": tmdb_id,
                    "folder_cid": folder_cid,
                    "rename_failed": True,
                })
                self._update_offline_task_status(info_hash, "重命名失败", folder_cid=folder_cid, path=source_path, error=rename_result.get("message"))
                return {"success": False}

            # Step 4: record pending task; agent decides target directory from MP mapping config
            final_path = self._join_u115_path(self._resolved_path, new_folder_name)
            self._record_pending_task(task_name, final_path, {
                "rename_result": rename_result.get("message"),
                "media_type": media_type,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "folder_cid": folder_cid,
                "needs_agent_target_match": True,
            })
            self._update_offline_task_status(info_hash, "重命名完成，待智能体移动", folder_cid=folder_cid, path=final_path)

            # Step 5: 主动调用智能体处理删广告和移动
            _title_for_notify = re.sub(r"^\[[^\]]*\]\s*", "", task_name)
            _match = re.match(r"^([^\.\[]+)", _title_for_notify)
            _clean_name = _match.group(1).strip() if _match else _title_for_notify.strip()

            # 构造发给智能体的消息
            agent_message = (
                f"115离线下载已完成并重命名，请处理后续整理：\n"
                f"标题: {title}\n"
                f"年份: {year}\n"
                f"TMDB ID: {tmdb_id}\n"
                f"媒体类型: {media_type}\n"
                f"文件夹名: {new_folder_name}\n"
                f"文件路径: {final_path}\n"
                f"CID: {folder_cid}\n"
                f"存储: u115\n"
                f"\n请执行以下操作：\n"
                f"1. 列出该文件夹内的所有文件，识别并删除广告文件（非媒体文件、小文件、广告文本文件等）\n"
                f"2. 根据媒体类型({media_type})和MP目录映射配置，计算目标入库目录\n"
                f"3. 使用115网盘API(fs_move)将整个文件夹移动到目标入库目录\n"
                f"\n注意：MP内置StorageChain没有move方法，移动文件请使用p115client的fs_move(file_ids, pid=目标目录CID)"
                f"\n4. 完成移动后，调用 cl_search_complete_task 工具清除待整理任务记录，参数 task_name 为: {task_name}"
            )

            # 主动发送消息给智能体
            self._notify_agent(agent_message)

            # 同时发MP通知记录
            notify_content = f"**离线下载完成: {task_name}**\n"
            if new_folder_name != task_name:
                notify_content += f"重命名: `{task_name}` -> `{new_folder_name}`\n"
            notify_content += f"文件路径: `{final_path}`\n"
            notify_content += f"CID: `{folder_cid}`\n"
            if media_type:
                notify_content += f"媒体类型: {media_type}\n"
            notify_content += "已通知智能体处理删广告和移动"

            self.post_message(
                title=f"{_clean_name} 重命名完成，待移动",
                content=notify_content,
                notification_type=NotificationType.Plugin,
            )
            # 整理完成，清除取消标志
            self._cancel_flags.pop(info_hash, None)
            # 返回需要智能体处理的信息
            return {
                "needs_agent": True,
                "folder_cid": folder_cid,
                "agent_message": agent_message,
            }

        except Exception as e:
            logger.error(f"自动整理异常: {e}")
            self._record_pending_task(task_name, None, {"error": str(e)})
            self._update_offline_task_status(info_hash, "自动整理异常", error=str(e))
            return {"success": False}

    def _notify_agent(self, message: str) -> bool:
        """主动发送消息给MP智能体处理

        优先用 MP 内部 MessageChain.handle_message()（进程内调用，无需 token），
        HTTP API 作为兜底。
        """
        # 优先用 MP 内部方法
        try:
            from app.chain.message import MessageChain
            from app.schemas.types import MessageChannel
            logger.info(f"通过MessageChain发送消息给智能体: text={message[:100]}...")
            MessageChain().handle_message(
                channel=MessageChannel.Web,
                source="clsearch",
                userid="clsearch",
                username="clsearch",
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
            data = {"text": message, "source": "clsearch"}
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
        self._search_history.insert(0, {
            "keyword": keyword,
            "count": count,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self._search_history = self._search_history[:20]
        self.save_data(self._search_history_data_key, self._search_history)

    def _record_offline_history(self, title: str, success: bool, error: str = "") -> None:
        """记录离线下载历史"""
        self._offline_history.insert(0, {
            "title": title,
            "success": success,
            "error": error,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self._offline_history = self._offline_history[:20]
        self.save_data(self._offline_history_data_key, self._offline_history)

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
            content="\n".join(content_parts),
            notification_type=NotificationType.Plugin,
        )

    def get_agent_tools(self) -> List[type]:
        """获取插件智能体工具，供内置AI智能体调用"""
        return [ClSearchSearchTool, ClSearchOfflineResultTool, ClSearchDetailTool, ClSearchOfflineTool, ClSearchRenameTool]

    def stop_service(self) -> None:
        """停止插件服务"""
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
        logger.info("观影搜插件服务已停止")


# ==================== 智能体工具 ====================

class ClSearchSearchInput(BaseModel):
    """搜索工具入参模型"""
    keyword: str = Field(..., description="要搜索的影视关键词，如'开端'、'流浪地球'")


class ClSearchDetailInput(BaseModel):
    """详情工具入参模型"""
    detail_path: str = Field(..., description="搜索结果中的 detail_path 字段，如 '/bt/xxxxx'")


class ClSearchOfflineInput(BaseModel):
    """离线下载工具入参模型"""
    magnet: str = Field(..., description="磁力链接，从详情工具返回的 magnet 字段获取")
    title: str = Field(default="", description="资源标题，可选；不传时插件自动从磁力链dn参数提取，提取失败用默认名")


class ClSearchOfflineResultInput(BaseModel):
    """搜索结果一键离线工具入参模型"""
    keyword: str = Field(default="", description="搜索关键词，如用户刚搜索的影视名称；detail_path 为空时必填")
    index: int = Field(default=1, description="搜索结果序号，从1开始，例如用户说第二个就传2")
    detail_path: str = Field(default="", description="搜索结果中的 detail_path 字段；有该字段时优先使用")
    search_type: str = Field(default="4", description="搜索类型，默认4=种子")


class ClSearchSearchTool(MoviePilotTool):
    """搜索磁力资源工具"""
    name: str = "cl_search_search"
    description: str = (
        "搜索影视磁力资源。输入关键词，返回搜索结果列表，包含标题、大小、做种数、更新时间"
        "和 detail_path 字段。\n\n"
        "【重要】用户搜索即默认要将资源添加到115离线下载。"
        "搜到结果后请直接调用 cl_search_offline_result 工具，"
        "传入用户选择的序号对应的 detail_path，自动完成离线，无需询问用户。"
    )
    args_schema: Type[BaseModel] = ClSearchSearchInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        keyword = kwargs.get("keyword", "")
        return f"正在搜索磁力资源：{keyword}"

    async def run(self, keyword: str, **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行，请先启用插件并配置站点信息"

            result = plugin._api_search(keyword=keyword)
            if not result.get("success"):
                return f"搜索失败: {result.get('message', '未知错误')}"

            data = result.get("data", [])
            if not data:
                return f"未找到与 '{keyword}' 相关的磁力资源"

            result_lines = [f"搜索 '{keyword}' 找到 {len(data)} 个资源:"]
            for i, item in enumerate(data[:10], 1):
                result_lines.append(
                    f"\n{i}. {item.get('title', '')}\n"
                    f"   大小: {item.get('size', '')} | 做种: {item.get('seeders', '')} | 更新: {item.get('update_time', '')}\n"
                    f"   detail_path: {item.get('detail_path', '')}"
                )

            if len(data) > 10:
                result_lines.append(f"\n... 还有 {len(data) - 10} 个结果未显示")

            return "\n".join(result_lines)
        except Exception as e:
            return f"搜索失败: {str(e)}"


class ClSearchDetailTool(MoviePilotTool):
    """获取磁力资源详情工具"""
    name: str = "cl_search_detail"
    description: str = (
        "获取磁力资源的详细信息，包括磁力链接(magnet)和种子下载链接(torrent_url)。"
        "需要传入搜索结果的 detail_path 字段。"
    )
    args_schema: Type[BaseModel] = ClSearchDetailInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在获取磁力资源详情..."

    async def run(self, detail_path: str, **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            result = plugin._api_detail(detail_path=detail_path)
            if not result.get("success"):
                return f"获取详情失败: {result.get('message', '未知错误')}"

            data = result.get("data", {})
            if not data:
                return "详情数据为空"

            lines = [
                f"📄 {data.get('title', '未知标题')}",
                f"\n🔗 磁力链接: {data.get('magnet', '无')}",
            ]
            if data.get("torrent_url"):
                lines.append(f"⬇️ 种子下载: {data.get('torrent_url')}")

            files = data.get("files", [])
            if files:
                lines.append(f"\n📁 文件列表 ({len(files)}个文件):")
                for f in files[:5]:
                    lines.append(f"  - {f}")
                if len(files) > 5:
                    lines.append(f"  ... 还有 {len(files) - 5} 个文件")

            return "\n".join(lines)
        except Exception as e:
            return f"获取详情失败: {str(e)}"


class ClSearchOfflineResultTool(MoviePilotTool):
    """按搜索结果直接添加115离线下载工具"""
    name: str = "cl_search_offline_result"
    description: str = (
        "将搜索结果直接添加到115网盘离线下载。可传 detail_path 直接下载某条搜索结果；"
        "也可传 keyword 和 index，工具会先搜索并选择对应序号，再获取磁力链接并添加离线。"
        "适合用户说'第二个离线到115'、'下载第3个'这类连续操作。"
    )
    args_schema: Type[BaseModel] = ClSearchOfflineResultInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        detail_path = kwargs.get("detail_path") or ""
        keyword = kwargs.get("keyword") or ""
        index = kwargs.get("index") or 1
        if detail_path:
            return "正在获取资源详情并添加到115离线下载..."
        return f"正在将 '{keyword}' 的第 {index} 个搜索结果添加到115离线下载..."

    async def run(self, keyword: str = "", index: int = 1, detail_path: str = "", search_type: str = "4", **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            selected_title = ""
            if not detail_path:
                if not keyword:
                    return "离线下载失败: 请提供搜索关键词或 detail_path"
                search_result = plugin._api_search(keyword=keyword, search_type=search_type)
                if not search_result.get("success"):
                    return f"搜索失败: {search_result.get('message', '未知错误')}"
                items = search_result.get("data") or []
                if not items:
                    return f"未找到与 '{keyword}' 相关的磁力资源"
                if index < 1 or index > len(items):
                    return f"离线下载失败: 搜索结果序号 {index} 超出范围，共 {len(items)} 个结果"
                selected = items[index - 1]
                detail_path = selected.get("detail_path") or ""
                selected_title = selected.get("title") or ""

            detail_result = plugin._api_detail(detail_path=detail_path)
            if not detail_result.get("success"):
                return f"获取详情失败: {detail_result.get('message', '未知错误')}"

            detail = detail_result.get("data") or {}
            magnet = detail.get("magnet") or ""
            title = detail.get("title") or selected_title
            if not magnet:
                return f"离线下载失败: 未解析到磁力链接，资源: {title or detail_path}"

            offline_result = plugin._api_offline_download(data={
                "magnet": magnet,
                "title": title,
            })
            if offline_result.get("success"):
                return offline_result.get("message", f"已成功添加到115离线下载: {title}")
            return f"离线下载失败: {offline_result.get('message', '未知错误')}"
        except Exception as e:
            return f"离线下载失败: {str(e)}"


class ClSearchOfflineTool(MoviePilotTool):
    """115离线下载工具"""
    name: str = "cl_search_offline"
    description: str = (
        "将磁力链接添加到115网盘离线下载。"
        "magnet 为必填参数；title 为可选参数，不传时插件会自动从磁力链 dn 参数提取标题，提取失败则使用默认名。"
        "使用前请确保已在插件配置中填写115网盘Cookie和下载目录ID。"
    )
    args_schema: Type[BaseModel] = ClSearchOfflineInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        title = kwargs.get("title", "")
        return f"正在将 '{title}' 添加到115离线下载..."

    async def run(self, magnet: str, title: str = "", **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            result = plugin._api_offline_download(data={
                "magnet": magnet,
                "title": title,
            })
            if result.get("success"):
                # 如果需要智能体后续处理（重命名完成，需删广告和移动），返回详细指令
                data = result.get("data") or {}
                if data.get("needs_agent"):
                    return result.get("message", "重命名完成，需智能体处理")
                return result.get("message", f"已成功添加到115离线下载: {title}")
            else:
                return f"离线下载失败: {result.get('message', '未知错误')}"
        except Exception as e:
            return f"离线下载失败: {str(e)}"


class ClSearchRenameInput(BaseModel):
    """递归重命名输入"""
    cid: str = Field(description="115网盘目录ID，例如 '3882019211307386121'")
    path: str = Field(default="", description="115网盘目录完整路径，例如 '/观影磁力搜/目录名/'，可选")
    name: str = Field(default="", description="离线下载目录名，可选；插件会自动拼接离线根目录")
    new_name: str = Field(default="", description="目录最终名称（可选，有值则在子文件重命名后重命名目录本身）")


class ClSearchRenameTool(MoviePilotTool):
    """115网盘递归重命名工具"""
    name: str = "cl_search_rename"
    description: str = (
        "递归重命名115网盘目录内的媒体文件。遍历目录下所有媒体文件，"
        "使用MoviePilot识别引擎智能生成推荐文件名并批量重命名。"
        "可选传入new_name在文件重命名后重命名目录本身。"
    )
    args_schema: Type[BaseModel] = ClSearchRenameInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        cid = kwargs.get("cid", "")
        return f"正在递归重命名115目录 {cid} 内的媒体文件..."

    async def run(self, cid: str, new_name: str = "", path: str = "", name: str = "", **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            result = plugin._api_recursive_rename(data={
                "cid": cid,
                "path": path,
                "name": name,
                "new_name": new_name,
            })

            if result.get("success"):
                msg = f"✅ {result.get('message', '')}\n"
                files = result.get("files", [])
                renamed = [f for f in files if f.get("status") == "✅ 已重命名"]
                if renamed:
                    msg += "\n重命名明细:\n"
                    for f in renamed:
                        msg += f"  {f.get('old_name')} → {f.get('new_name')}\n"
                skipped = [f for f in files if f.get("status") == "无需修改"]
                if skipped:
                    msg += f"\n无需修改: {len(skipped)} 个\n"
                failed = [f for f in files if "失败" in f.get("status", "")]
                if failed:
                    msg += f"\n失败: {len(failed)} 个\n"
                    for f in failed:
                        msg += f"  {f.get('old_name')}: {f.get('status')}\n"
                return msg
            else:
                return f"重命名失败: {result.get('message', '未知错误')}"
        except Exception as e:
            return f"重命名失败: {str(e)}"


class ClSearchCompleteTaskInput(BaseModel):
    task_name: str = Field(default="", description="任务名称（离线下载时的标题或文件夹名），用于匹配待整理任务")


class ClSearchCompleteTaskTool(MoviePilotTool):
    """完成待整理任务工具"""
    name: str = "cl_search_complete_task"
    description: str = (
        "完成并清除观影磁力搜插件的待整理任务。"
        "当智能体完成广告删除和文件移动后，调用此工具清除对应的待整理任务记录。"
        "task_name 为离线下载时的标题或文件夹名。"
    )
    args_schema: Type[BaseModel] = ClSearchCompleteTaskInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        task_name = kwargs.get("task_name", "")
        return f"正在完成待整理任务: {task_name}..."

    async def run(self, task_name: str = "", **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            # 清除待整理任务
            success = plugin.clear_pending_transfer_task(task_name)
            if success:
                return f"已完成并清除待整理任务: {task_name}"
            else:
                return f"清除待整理任务失败: {task_name}"
        except Exception as e:
            return f"完成待整理任务异常: {str(e)}"
