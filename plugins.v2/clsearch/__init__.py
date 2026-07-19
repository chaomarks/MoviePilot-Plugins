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
from urllib.parse import urljoin

import requests
from p115client import P115Client, P115OpenClient
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
    plugin_version = "1.5.3.1"
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
    _polling_thread: Optional[threading.Thread] = None
    _polling_stop: Optional[threading.Event] = None

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件"""
        self.stop_service()
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

        # 恢复待整理任务并启动轮询
        saved_pending = self.get_data("pending_transfer_tasks") or []
        if saved_pending:
            for t in saved_pending:
                task_name = t.get("task_name", "")
                if task_name:
                    self._pending_tasks[f"restored_{task_name}"] = {
                        "name": task_name,
                        "title": t.get("task_name", ""),
                        "add_time": time.time(),
                    }
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
                    # 115网盘配置（一行：Cookie + CID + 解析路径）
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
                        ],
                    },
                    # ========== 分隔线 ==========
                    {
                        "component": "VDivider",
                        "props": {"class": "mt-4 mb-4"},
                    },
                    # ========== 分隔标题：离线完成自动整理 ==========
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
                                        "text": "离线完成自动整理",
                                    }
                                ],
                            },
                        ],
                    },
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
                                            "model": "auto_transfer",
                                            "label": "完成后自动整理",
                                            "color": "primary",
                                            "hint": "开启后，115离线下载完成时自动触发MoviePilot整理入库",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
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
                            # 持久化 Cookie 到配置
                            try:
                                from app.core.plugin import PluginManager
                                plugin_obj = PluginManager().running_plugins.get("ClSearch") or PluginManager().running_plugins.get("clsearch")
                                if plugin_obj and hasattr(plugin_obj, '_config'):
                                    cfg = dict(plugin_obj._config)
                                    cfg["site_cookie"] = self._site_cookie
                                    plugin_obj.update_config(cfg)
                            except Exception as persist_e:
                                logger.warning(f"Cookie持久化失败: {persist_e}")
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
                        # 持久化 Cookie
                        try:
                            from app.core.plugin import PluginManager
                            plugin_obj = PluginManager().running_plugins.get("ClSearch") or PluginManager().running_plugins.get("clsearch")
                            if plugin_obj and hasattr(plugin_obj, '_config'):
                                cfg = dict(plugin_obj._config)
                                cfg["site_cookie"] = self._site_cookie
                                plugin_obj.update_config(cfg)
                        except Exception as persist_e:
                            logger.warning(f"Cookie持久化失败: {persist_e}")
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
        """发起请求，自动处理 PoW 拦截

        流程：
        1. 优先使用Session（账号登录后的会话）
        2. 否则使用Cookie Header
        3. 被PoW拦截时：
           - 有Session → 在Session上解决PoW，重试
           - 有账号密码 → 完整登录（PoW+登录），自动填入Cookie，重试
           - 仅有Cookie → 解决PoW，合并browser_verified到Cookie，重试

        Args:
            method: HTTP方法
            url: 请求URL
            **kwargs: 传递给requests的其他参数

        Returns:
            requests.Response对象
        """
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)

        if self._session:
            resp = self._session.request(method, url, **kwargs)
        else:
            headers = kwargs.pop("headers", {})
            merged_headers = self._get_headers()
            merged_headers.update(headers)
            kwargs["headers"] = merged_headers
            resp = requests.request(method, url, **kwargs)

        # 检查是否被 PoW 拦截
        if "powSolve" in resp.text or "安全验证" in resp.text:
            logger.info("请求被PoW拦截，尝试解决...")

            # 情况1：有Session → 在Session上解决PoW
            if self._session:
                if self._solve_pow(self._session):
                    return self._session.request(method, url, **kwargs)

            # 情况2：有账号密码 → 完整登录（PoW+登录），自动填入Cookie
            elif self._site_username and self._site_password:
                logger.info("Cookie模式受到PoW拦截，尝试用账号密码登录...")
                success, msg = self._site_login()
                if success:
                    logger.info("账号密码登录成功，Cookie已自动更新，使用Session重试")
                    return self._session.request(method, url, **kwargs)
                else:
                    logger.error(f"账号密码登录失败: {msg}")

            # 情况3：仅有Cookie → 解决PoW，合并browser_verified
            else:
                temp_session = requests.Session()
                temp_session.headers.update(self._get_headers())
                temp_session.get(self._site_url, timeout=30)
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
                        self._site_cookie = "; ".join(
                            f"{k}={v}" for k, v in existing.items()
                        )
                    headers = kwargs.pop("headers", {})
                    merged_headers = self._get_headers()
                    merged_headers.update(headers)
                    kwargs["headers"] = merged_headers
                    return requests.request(method, url, **kwargs)

        # 检查是否 Session 过期（被重定向到登录页或返回登录页HTML）
        if self._session and self._is_login_page(resp):
            logger.info("Session已过期（检测到登录页），尝试自动重新登录...")
            self._session = None
            if self._site_username and self._site_password:
                success, msg = self._site_login()
                if success:
                    logger.info("重新登录成功，重试请求")
                    return self._session.request(method, url, **kwargs)
                else:
                    logger.error(f"重新登录失败: {msg}")

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
            self._search_cache[cache_key] = results

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

            for i in range(len(titles)):
                try:
                    title = titles[i]
                    size_text = sizes[i] if i < len(sizes) else ""
                    seeders = seeds[i] if i < len(seeds) else 0
                    update_time = times[i] if i < len(times) else ""
                    detail_id = detail_ids[i] if i < len(detail_ids) else ""
                    detail_type = detail_types[i] if i < len(detail_types) else "bt"

                    if not title or not detail_id:
                        continue

                    detail_path = f"/{detail_type}/{detail_id}"
                    unique_id = hashlib.md5(detail_path.encode()).hexdigest()[:12]

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
        for i in range(idx, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    buf.append(ch)
                    break
            buf.append(ch)
        return ''.join(buf)

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
        """创建文件夹

        Args:
            data: {"cid": 父目录CID, "name": 新文件夹名称}

        Returns:
            创建结果
        """
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            payload = {
                "cid": str(data.get("cid", "0")),
                "name": str(data.get("name", "")),
            }

            result = client.fs_create(payload)

            if result.get("state"):
                folder_cid = result.get("data", {}).get("cid") or result.get("cid")
                if folder_cid:
                    return {"success": True, "message": f"文件夹创建成功", "data": {"cid": str(folder_cid)}}
            return {"success": False, "message": result.get("message", "创建文件夹失败")}
        except Exception as e:
            return {"success": False, "message": f"创建文件夹失败: {str(e)}"}

    def _api_rename(self, data: dict) -> dict:
        """重命名文件或文件夹

        Args:
            data: {"cid": 文件/文件夹CID, "name": 新名称}

        Returns:
            重命名结果
        """
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            payload = {
                "cid": str(data.get("cid", "")),
                "name": str(data.get("name", "")),
            }

            result = client.fs_rename(payload)

            if result.get("state"):
                return {"success": True, "message": f"重命名成功"}
            return {"success": False, "message": result.get("message", "重命名失败")}
        except Exception as e:
            return {"success": False, "message": f"重命名失败: {str(e)}"}

    def _api_move_file(self, data: dict) -> dict:
        """移动文件到指定文件夹

        Args:
            data: {"cid": 源文件CID, "target_cid": 目标文件夹CID}

        Returns:
            移动结果
        """
        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            payload = {
                "cid": str(data.get("cid", "")),
                "p_cid": str(data.get("target_cid", "")),
            }

            result = client.fs_move(payload)

            if result.get("state"):
                return {"success": True, "message": f"文件移动成功"}
            return {"success": False, "message": result.get("message", "文件移动失败")}
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
                payload["limit"] = int(data.get("limit", 100))
                payload["offset"] = int(data.get("offset", 0))

            result = client.fs_files(payload)

            if result.get("state"):
                return {"success": True, "data": result.get("data", [])}
            return {"success": False, "message": result.get("message", "获取文件列表失败")}
        except Exception as e:
            return {"success": False, "message": f"获取文件列表失败: {str(e)}"}

    def _api_offline_download(self, data: dict = None) -> dict:
        """API: 添加115离线下载

        Args:
            data: 下载信息，包含 magnet 和 title

        Returns:
            含 success、message、data 的字典
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not self._p115_cookie:
            return {"success": False, "message": "未配置115网盘Cookie"}

        if not self._save_dir_id:
            return {"success": False, "message": "未配置115离线下载目录ID"}

        if not data:
            return {"success": False, "message": "请提供下载信息"}

        magnet = data.get("magnet") or ""
        title = data.get("title") or ""

        if not magnet:
            return {"success": False, "message": "请提供有效的磁力链接"}

        # 验证磁力链接格式（简单前缀检查）
        magnet = magnet.strip()
        if not (magnet.startswith("magnet:?") or magnet.startswith("http")):
            return {"success": False, "message": "不支持的链接格式，请使用磁力链接(magnet:)或HTTP链接"}

        # 验证目录ID是否为有效数字
        try:
            save_dir_id = int(self._save_dir_id)
        except (ValueError, TypeError):
            logger.error(f"无效的115目录ID: {self._save_dir_id}")
            return {"success": False, "message": f"无效的115目录ID: {self._save_dir_id}，请检查配置"}

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            # 使用 p115client 库调用离线下载 API
            # 必须使用 P115OpenClient 的方法（走 proapi.115.com/open/offline/add_task_urls）
            # P115Client 的覆盖版本使用 clouddownload.115.com SSP 接口，对某些磁力链返回"错误的链接"
            client = P115Client(p115_cookie)
            payload = {
                "urls": magnet,
                "wp_path_id": save_dir_id,
            }

            logger.info(f"添加115离线下载: {title}, 目标目录CID: {save_dir_id}")

            result = P115OpenClient.clouddownload_task_add_urls(client, payload)

            # 检查返回结果（兼容 Open API 和 SSP API 两种格式）
            state = result.get("state", False) if result else False
            errcode = result.get("errcode") or result.get("code") if result else None
            error_msg = (result.get("error_msg") or result.get("error") or
                         result.get("message") or "")
            # Open API 成功时 data 数组内也可能有单条失败
            if state and not error_msg:
                data_items = result.get("data") or []
                if isinstance(data_items, list):
                    for item in data_items:
                        if isinstance(item, dict) and not item.get("state", True):
                            state = False
                            error_msg = item.get("message") or item.get("error_msg") or f"单条任务失败 (code={item.get('code')})"
                            break

            # 任务已存在（重复添加）
            if errcode == 10008:
                logger.info(f"115离线下载任务已存在: {title}")
                self._record_offline_history(title, True)
                return {
                    "success": True,
                    "message": f"任务已存在，跳过重复添加: {title}",
                    "data": result,
                }

            if state:
                logger.info(f"115离线下载添加成功: {title}")
                self._record_offline_history(title, True)

                # 从返回结果中提取 info_hash，加入轮询监控
                data_items = result.get("data") or []
                if isinstance(data_items, list):
                    for item in data_items:
                        if isinstance(item, dict):
                            info_hash = item.get("info_hash") or ""
                            if info_hash:
                                self._pending_tasks[info_hash] = {
                                    "name": item.get("name", title),
                                    "title": title,
                                    "add_time": time.time(),
                                }
                                logger.info(f"已加入离线完成轮询监控: {title} ({info_hash[:12]}...)")
                # 启动后台轮询
                self._start_polling()

                return {
                    "success": True,
                    "message": f"已添加到115离线下载: {title}",
                    "data": result,
                }

            # 其他业务错误
            if not error_msg:
                error_msg = f"未知错误 (errcode={errcode})" if errcode else "未知错误"
            logger.error(f"115离线下载添加失败: {error_msg}")
            self._record_offline_history(title, False, error_msg)
            return {
                "success": False,
                "message": f"添加失败: {error_msg}",
                "data": result,
            }

        except Exception as e:
            logger.error(f"115离线下载异常: {e}")
            return {"success": False, "message": f"下载异常: {str(e)}"}

    def _api_recursive_rename(self, data: dict = None) -> dict:
        """API: 递归重命名115网盘目录内媒体文件（Open API + MP推荐命名）

        使用 MoviePilot 的 MediaChain/TransferChain 生成推荐命名，使用115 Open API完成列目录、
        重命名、建目录和移动，避免走 webapi batch_rename。
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if not self._p115_cookie:
            return {"success": False, "message": "未配置115 Cookie"}

        data = data or {}
        cid = str(data.get("cid") or "").strip()
        new_name = str(data.get("new_name") or "").strip()
        if not cid:
            return {"success": False, "message": "请提供cid"}

        try:
            from app.chain.media import MediaChain
            from app.chain.transfer import TransferChain

            cookie = self._normalize_cookie(self._p115_cookie)
            try:
                client = P115Client(cookies=cookie)
            except TypeError:
                client = P115Client(cookie)

            def _invoke_open(method_name: str, attempts: list) -> dict:
                """Try P115OpenClient and client method variants for p115client version compatibility."""
                funcs = []
                method = getattr(P115OpenClient, method_name, None)
                if method:
                    funcs.append(lambda *args, **kwargs: method(client, *args, **kwargs))
                client_method = getattr(client, method_name, None)
                if client_method:
                    funcs.append(client_method)

                last_error = None
                for func in funcs:
                    for args, kwargs in attempts:
                        try:
                            result = func(*args, **kwargs)
                        except TypeError as e:
                            last_error = e
                            continue
                        except Exception as e:
                            last_error = e
                            continue
                        if _is_api_ok(result):
                            return result
                        last_error = result
                return {"state": False, "message": str(last_error) if last_error else f"{method_name} 调用失败"}

            def _is_api_ok(result: dict) -> bool:
                if not result or not isinstance(result, dict):
                    return False
                if result.get("error") or result.get("errno") or result.get("errcode"):
                    return False
                if result.get("state") is False or result.get("success") is False:
                    return False
                return True

            def _extract_items(result: dict) -> list:
                data_obj = result.get("data") if isinstance(result, dict) else None
                if isinstance(data_obj, list):
                    return data_obj
                if isinstance(data_obj, dict):
                    for key in ("list", "items", "files", "data"):
                        value = data_obj.get(key)
                        if isinstance(value, list):
                            return value
                for key in ("list", "items", "files"):
                    value = result.get(key) if isinstance(result, dict) else None
                    if isinstance(value, list):
                        return value
                return []

            def _item_name(item: dict) -> str:
                return str(item.get("n") or item.get("name") or item.get("file_name") or item.get("filename") or "")

            def _item_id(item: dict) -> str:
                return str(item.get("fid") or item.get("file_id") or item.get("id") or item.get("cid") or "")

            def _is_dir_item(item: dict, current_cid: str) -> bool:
                if item.get("is_dir") in (True, 1, "1"):
                    return True
                if str(item.get("type", "")).lower() in ("1", "folder", "dir", "directory"):
                    return True
                # web/open responses may expose folder id as cid and file id as fid.
                return bool(item.get("cid") and not item.get("fid") and str(item.get("cid")) != str(current_cid))

            def _open_list(folder_id: str, offset: int = 0) -> dict:
                payloads = [
                    {"cid": str(folder_id), "limit": 1000, "offset": offset},
                    {"file_id": str(folder_id), "limit": 1000, "offset": offset},
                    {"parent_id": str(folder_id), "limit": 1000, "offset": offset},
                ]
                attempts = []
                for payload in payloads:
                    attempts.extend([((payload,), {}), ((), payload)])
                return _invoke_open("fs_files_open", attempts)

            def _open_rename(file_id: str, file_name: str) -> dict:
                payloads = [
                    {"file_id": str(file_id), "file_name": file_name},
                    {"file_id": str(file_id), "name": file_name},
                    {"fid": str(file_id), "file_name": file_name},
                    {"fid": str(file_id), "name": file_name},
                ]
                attempts = [((str(file_id), file_name), {})]
                for payload in payloads:
                    attempts.extend([((payload,), {}), ((), payload)])
                return _invoke_open("fs_rename_open", attempts)

            def _open_mkdir(parent_id: str, dir_name: str) -> dict:
                payloads = [
                    {"parent_id": str(parent_id), "file_name": dir_name},
                    {"parent_id": str(parent_id), "name": dir_name},
                    {"pid": str(parent_id), "cname": dir_name},
                    {"cid": str(parent_id), "name": dir_name},
                ]
                attempts = [((str(parent_id), dir_name), {})]
                for payload in payloads:
                    attempts.extend([((payload,), {}), ((), payload)])
                return _invoke_open("fs_mkdir_open", attempts)

            def _open_move(file_id: str, target_parent_id: str) -> dict:
                payloads = [
                    {"file_id": str(file_id), "parent_id": str(target_parent_id)},
                    {"file_id": str(file_id), "to_parent_id": str(target_parent_id)},
                    {"fid": str(file_id), "pid": str(target_parent_id)},
                    {"fid": str(file_id), "target_cid": str(target_parent_id)},
                ]
                attempts = [((str(file_id),), {"pid": str(target_parent_id)}), ((str(file_id),), {"parent_id": str(target_parent_id)})]
                for payload in payloads:
                    attempts.extend([((payload,), {}), ((), payload)])
                return _invoke_open("fs_move_open", attempts)

            def _created_dir_id(result: dict) -> str:
                for key in ("file_id", "cid", "id"):
                    if result.get(key):
                        return str(result[key])
                data_obj = result.get("data") if isinstance(result, dict) else None
                if isinstance(data_obj, dict):
                    for key in ("file_id", "cid", "id"):
                        if data_obj.get(key):
                            return str(data_obj[key])
                return ""

            media_exts = ('.mkv', '.mp4', '.avi', '.ts', '.rmvb', '.flv', '.wmv', '.mov', '.m4v', '.iso', '.strm')
            all_files = []
            root_dirs = {}
            visited_dirs = set()
            dir_stack = [(cid, "")]

            while dir_stack:
                current_cid, parent_path = dir_stack.pop()
                if current_cid in visited_dirs:
                    continue
                visited_dirs.add(current_cid)
                offset = 0
                while True:
                    list_result = _open_list(current_cid, offset)
                    if not _is_api_ok(list_result):
                        return {"success": False, "message": f"列目录失败: {list_result}", "files": []}
                    items = _extract_items(list_result)
                    if not items:
                        break
                    for item in items:
                        item_name = _item_name(item)
                        if not item_name:
                            continue
                        if _is_dir_item(item, current_cid):
                            dir_id = str(item.get("cid") or item.get("file_id") or item.get("id") or "")
                            if not dir_id:
                                continue
                            if current_cid == cid:
                                root_dirs[item_name] = dir_id
                            sub_path = f"{parent_path}/{item_name}" if parent_path else item_name
                            dir_stack.append((dir_id, sub_path))
                            continue
                        if item_name.lower().endswith(media_exts):
                            file_id = _item_id(item)
                            if file_id:
                                all_files.append({"file_id": file_id, "name": item_name, "parent_path": parent_path})
                    if len(items) < 1000:
                        break
                    offset += 1000

            if not all_files:
                return {"success": False, "message": f"CID {cid} 下未找到媒体文件", "files": []}

            logger.info(f"找到 {len(all_files)} 个媒体文件，开始使用MP推荐命名...")
            media_chain = MediaChain()
            transfer_chain = TransferChain()
            results = []
            rename_jobs = []
            move_jobs = []

            for file_item in all_files:
                file_id = file_item["file_id"]
                file_name = file_item["name"]
                parent_path = file_item["parent_path"]
                try:
                    virtual_path = Path(f"/{parent_path}/{file_name}" if parent_path else f"/{file_name}")
                    context = media_chain.recognize_by_path(virtual_path, obtain_images=False)
                    if not context or not context.media_info:
                        results.append({"file_id": file_id, "old_name": file_name, "status": "识别失败"})
                        continue

                    recommend_path = transfer_chain.recommend_name(meta=context.meta_info, mediainfo=context.media_info)
                    if not recommend_path:
                        results.append({"file_id": file_id, "old_name": file_name, "status": "推荐名称为空"})
                        continue

                    rec_path = Path(recommend_path)
                    new_filename = rec_path.name
                    if not new_filename:
                        results.append({"file_id": file_id, "old_name": file_name, "status": "推荐文件名为空"})
                        continue

                    orig_ext = Path(file_name).suffix
                    rec_ext = Path(new_filename).suffix
                    if orig_ext and rec_ext and orig_ext.lower() != rec_ext.lower():
                        new_filename = new_filename[:-len(rec_ext)] + orig_ext

                    relative_dir = ""
                    if len(rec_path.parts) > 2:
                        relative_dir = rec_path.parts[1]

                    res = {"file_id": file_id, "old_name": file_name, "new_name": new_filename, "status": "待处理"}
                    results.append(res)
                    if new_filename != file_name:
                        rename_jobs.append((file_id, new_filename, res))
                    if relative_dir:
                        move_jobs.append((file_id, relative_dir, res))
                    if new_filename == file_name and not relative_dir:
                        res["status"] = "无需修改"
                except Exception as e:
                    results.append({"file_id": file_id, "old_name": file_name, "status": f"识别异常: {str(e)[:80]}"})

            rename_success = 0
            rename_fail = 0
            for file_id, target_name, res in rename_jobs:
                rename_result = _open_rename(file_id, target_name)
                if _is_api_ok(rename_result):
                    rename_success += 1
                    res["status"] = "✅ 已重命名"
                else:
                    rename_fail += 1
                    res["status"] = f"重命名失败: {rename_result}"

            dir_cid_map = dict(root_dirs)
            created_dirs = 0
            move_success = 0
            move_fail = 0
            for file_id, dir_name, res in move_jobs:
                target_cid = dir_cid_map.get(dir_name)
                if not target_cid:
                    mkdir_result = _open_mkdir(cid, dir_name)
                    target_cid = _created_dir_id(mkdir_result) if _is_api_ok(mkdir_result) else ""
                    if target_cid:
                        dir_cid_map[dir_name] = target_cid
                        created_dirs += 1
                        logger.info(f"创建子目录: {dir_name}, CID: {target_cid}")
                    else:
                        move_fail += 1
                        res["status"] = f"目录创建失败: {dir_name}, result={mkdir_result}"
                        continue
                move_result = _open_move(file_id, target_cid)
                if _is_api_ok(move_result):
                    move_success += 1
                    res["status"] = f"{res.get('status', '已处理')} -> {dir_name}/"
                else:
                    move_fail += 1
                    res["status"] = f"移动失败: {move_result}"

            dir_renamed = False
            dir_rename_error = ""
            if new_name:
                dir_result = _open_rename(cid, new_name)
                if _is_api_ok(dir_result):
                    dir_renamed = True
                    logger.info(f"目录已重命名为: {new_name}")
                else:
                    dir_rename_error = f"目录重命名失败: {dir_result}"
                    logger.error(dir_rename_error)

            failed_statuses = [r for r in results if any(word in str(r.get("status", "")) for word in ("失败", "异常"))]
            ok = rename_fail == 0 and move_fail == 0 and not dir_rename_error and not failed_statuses
            msg = f"处理完成: 共 {len(all_files)} 个文件，重命名成功 {rename_success} 个，重命名失败 {rename_fail} 个，创建目录 {created_dirs} 个，移动成功 {move_success} 个，移动失败 {move_fail} 个"
            if dir_renamed:
                msg += f"，目录已重命名为: {new_name}"
            if dir_rename_error:
                msg += f"，{dir_rename_error}"

            return {
                "success": ok,
                "message": msg,
                "files": results,
                "renamed": rename_success,
                "created_dirs": created_dirs,
                "moved": move_success,
            }

        except Exception as e:
            logger.error(f"递归重命名失败: {e}")
            return {"success": False, "message": f"操作失败: {str(e)}", "files": []}

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
                notification_type=NotificationType.Warning,
            )
            return

        result = self._api_search(keyword)
        if result.get("success") and result.get("data"):
            self._send_search_results(keyword, result["data"])
        else:
            self.post_message(
                title="观影搜",
                content=result.get("message", "搜索失败"),
                notification_type=NotificationType.Warning,
            )

    # ==================== 后台轮询 ====================

    def _start_polling(self) -> None:
        """启动后台轮询线程，监控115离线任务完成状态"""
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
        """停止后台轮询线程"""
        if self._polling_stop:
            self._polling_stop.set()
        if self._polling_thread:
            self._polling_thread.join(timeout=5)
            self._polling_thread = None
        logger.info("115离线下载轮询线程已停止")

    def _polling_worker(self) -> None:
        """后台轮询工作线程：每30秒检查一次离线任务状态"""
        while not self._polling_stop.is_set():
            try:
                if self._pending_tasks:
                    self._check_task_status()
                else:
                    # 没有待监控任务，休眠60秒再检查
                    self._polling_stop.wait(60)
                    continue
            except Exception as e:
                logger.error(f"115离线轮询异常: {e}")
            # 每次查询间隔30秒
            self._polling_stop.wait(30)

    def _check_task_status(self) -> None:
        """检查115离线任务状态，标记已完成的任务"""
        if not self._pending_tasks:
            return

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            result = P115OpenClient.clouddownload_task_list(client)

            if not result or not result.get("state"):
                return

            data = result.get("data") or {}
            tasks = data.get("tasks") or []

            now = time.time()
            # 将任务列表按 info_hash 建立索引
            task_map = {}
            for task in tasks:
                ih = task.get("info_hash")
                if ih:
                    task_map[ih] = task

            completed_keys = []
            expired_keys = []

            for info_hash, task_info in list(self._pending_tasks.items()):
                task = task_map.get(info_hash)
                if not task:
                    # 检查是否超时（超过24小时）
                    if now - task_info.get("add_time", 0) > 86400:
                        expired_keys.append(info_hash)
                    continue

                status = task.get("status")
                task_name = task.get("name", task_info.get("title", ""))

                if status == 2:  # 下载成功
                    completed_keys.append(info_hash)
                    logger.info(f"115离线下载完成: {task_name}")
                    self._record_offline_history(task_name, True)
                    # 发送通知
                    self.post_message(
                        title="115离线下载完成",
                        content=f"**{task_name}** 已离线下载完成",
                        notification_type=NotificationType.Information,
                    )
                    # 触发自动整理
                    if self._auto_transfer:
                        self._trigger_transfer(task_name, task)

                elif status == -1:  # 下载失败
                    completed_keys.append(info_hash)
                    logger.error(f"115离线下载失败: {task_name}")
                    self._record_offline_history(task_name, False, f"状态码 -1")
                    self.post_message(
                        title="115离线下载失败",
                        content=f"**{task_name}** 离线下载失败",
                        notification_type=NotificationType.Warning,
                    )

            # 清理已完成和过期任务
            for k in completed_keys:
                self._pending_tasks.pop(k, None)
            for k in expired_keys:
                self._pending_tasks.pop(k, None)

        except Exception as e:
            logger.error(f"115离线任务状态检查异常: {e}")

    def _trigger_transfer(self, task_name: str, task: dict) -> None:
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
                return

            logger.info(f"开始自动整理: {task_name}")

            if not self._p115_cookie:
                logger.info(f"未配置115 Cookie，跳过自动整理: {task_name}")
                return

            # 创建115客户端
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)

            # Step 1: 在离线目录中查找下载完成的文件夹
            save_cid = self._save_dir_id
            folder_cid = None
            offset = 0

            while True:
                result = client.fs_files({"cid": save_cid, "limit": 1000, "offset": offset})
                if not result.get("data"):
                    break
                for item in result["data"]:
                    item_name = item.get("n") or item.get("name") or ""
                    item_cid = item.get("cid")
                    if item_cid and item_name == task_name:
                        folder_cid = str(item_cid)
                        break
                if folder_cid:
                    break
                if len(result["data"]) < 1000:
                    break
                offset += 1000

            if not folder_cid:
                logger.info(f"未找到下载文件夹: {task_name}，记录为待整理任务")
                self._record_pending_task(task_name, None, None)
                return

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

            # Step 3: 调用内置rename API重命名文件夹和内部文件（含Season子目录创建）
            rename_result = self._api_recursive_rename(data={
                "cid": folder_cid,
                "new_name": new_folder_name,
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
                    notification_type=NotificationType.Warning,
                )
                self._record_pending_task(task_name, f"{self._resolved_path}/{task_name}", {
                    "rename_result": rename_result.get("message"),
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "tmdb_id": tmdb_id,
                    "folder_cid": folder_cid,
                    "rename_failed": True,
                })
                return

            # Step 4: 查询目标目录并记录待整理任务（供智能体后续处理广告删除和移动）
            final_path = f"{self._resolved_path}/{new_folder_name}"
            target_dir = None
            if media_type:
                target_dir_info = self._get_target_directory(media_type, year, title)
                if target_dir_info:
                    target_dir = target_dir_info.get("library_path")
                    logger.info(f"查询到目标目录: {target_dir}")

            self._record_pending_task(task_name, final_path, {
                "rename_result": rename_result.get("message"),
                "media_type": media_type,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "folder_cid": folder_cid,
                "target_dir": target_dir,
            })

            # Step 5: 通知智能体处理广告删除和移动入库
            _title_for_notify = re.sub(r'^【[^】]*】\s*', '', task_name)
            _title_for_notify = re.sub(r'^\[[^\]]*\]\s*', '', _title_for_notify)
            _match = re.match(r'^([^\.\[]+)', _title_for_notify)
            _clean_name = _match.group(1).strip() if _match else _title_for_notify.strip()

            notify_content = f"**离线下载完成: {task_name}**\n"
            if new_folder_name != task_name:
                notify_content += f"重命名: `{task_name}` → `{new_folder_name}`\n"
            notify_content += f"文件路径: `{final_path}`\n"
            notify_content += f"CID: `{folder_cid}`\n"
            if media_type:
                notify_content += f"媒体类型: {media_type}\n"
            if target_dir:
                notify_content += f"目标目录: `{target_dir}`\n"
            notify_content += f"\n请智能体处理：\n1. 识别并删除广告文件\n2. 用115 API将文件夹整体移动到目标目录"

            self.post_message(
                title=f"{_clean_name} 重命名完成，待整理",
                content=notify_content,
                notification_type=NotificationType.Plugin,
            )

        except Exception as e:
            logger.error(f"自动整理异常: {e}")

    def _record_pending_task(self, task_name: str, file_path: Optional[str], extra_info: Optional[dict]) -> None:
        """记录待整理任务到插件数据"""
        pending_tasks = self.get_data("pending_transfer_tasks") or []

        # 检查是否已存在
        for t in pending_tasks:
            if t.get("task_name") == task_name:
                logger.info(f"任务已存在，跳过: {task_name}")
                return

        default_path = f"{self._resolved_path}/{task_name}" if self._resolved_path else task_name
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

    def _get_target_directory(self, media_type: str, year: str, title: str) -> Optional[dict]:
        """根据媒体类型和年份获取目标目录配置

        Args:
            media_type: 媒体类型 (movie/tv)
            year: 年份
            title: 媒体标题

        Returns:
            目录配置字典
        """
        try:
            from app.core.config import settings

            directories = settings.TRANSFER_DIRECTORY_CONF or []

            matched_dirs = []
            for dir_conf in directories:
                if dir_conf.get("library_storage") != "u115":
                    continue

                conf_media_type = dir_conf.get("media_type", "")
                if conf_media_type and conf_media_type != media_type:
                    continue

                media_category = dir_conf.get("media_category", "")
                if self._match_media_category(media_category, media_type, year, title):
                    matched_dirs.append(dir_conf)

            if matched_dirs:
                return matched_dirs[0]

            for dir_conf in directories:
                if dir_conf.get("library_storage") == "u115":
                    if dir_conf.get("media_type") == media_type:
                        return dir_conf

            return None

        except Exception as e:
            logger.error(f"获取目标目录配置异常: {e}")
            return None

    def _match_media_category(self, category: str, media_type: str, year: str, title: str) -> bool:
        """判断媒体是否匹配指定分类

        Args:
            category: 分类名称（如"国产剧"、"欧美剧"等）
            media_type: 媒体类型
            year: 年份
            title: 媒体标题

        Returns:
            是否匹配
        """
        if not category:
            return True

        category_lower = category.lower()

        if "国产" in category_lower:
            return media_type == "tv"
        elif "欧美" in category_lower:
            return media_type == "tv"
        elif "日韩" in category_lower:
            return media_type == "tv"
        elif "电影" in category_lower:
            return media_type == "movie"

        return True

    def get_target_directory_for_transfer(self, media_type: str, year: str = None, title: str = None) -> Optional[dict]:
        """供智能体调用的目标目录查找方法

        Args:
            media_type: 媒体类型 (movie/tv)
            year: 年份（可选）
            title: 媒体标题（可选）

        Returns:
            目标目录配置字典，包含：
            - library_path: 媒体库路径
            - library_storage: 目标存储类型
            - download_path: 下载路径
            - storage: 源存储类型
            - transfer_type: 整理方式
        """
        return self._get_target_directory(media_type, year, title)

    def get_pending_transfer_tasks(self) -> List[dict]:
        """获取待整理任务列表

        Returns:
            待整理任务列表
        """
        return self.get_data("pending_transfer_tasks") or []

    def clear_pending_transfer_task(self, source_path: str) -> bool:
        """清除已处理的待整理任务

        Args:
            source_path: 源文件路径

        Returns:
            是否成功
        """
        try:
            pending_tasks = self.get_data("pending_transfer_tasks") or []
            new_tasks = [t for t in pending_tasks if t.get("source_path") != source_path]
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

    def _record_offline_history(self, title: str, success: bool, error: str = "") -> None:
        """记录离线下载历史"""
        self._offline_history.insert(0, {
            "title": title,
            "success": success,
            "error": error,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self._offline_history = self._offline_history[:20]

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
            notification_type=NotificationType.Information,
        )

    def get_agent_tools(self) -> List[type]:
        """获取插件智能体工具，供内置AI智能体调用"""
        return [ClSearchSearchTool, ClSearchOfflineResultTool, ClSearchDetailTool, ClSearchOfflineTool, ClSearchRenameTool]

    def stop_service(self) -> None:
        """停止插件服务"""
        self._stop_polling()
        self._pending_tasks.clear()
        self._search_cache.clear()
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
    title: str = Field(default="", description="资源标题，用于115离线下载的文件名")


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
        "将磁力链接添加到115网盘离线下载。需要传入磁力链接(magnet)和资源标题(title)。"
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
                return result.get("message", f"已成功添加到115离线下载: {title}")
            else:
                return f"离线下载失败: {result.get('message', '未知错误')}"
        except Exception as e:
            return f"离线下载失败: {str(e)}"


class ClSearchRenameInput(BaseModel):
    """递归重命名输入"""
    cid: str = Field(description="115网盘目录ID，例如 '3882019211307386121'")
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

    async def run(self, cid: str, new_name: str = "", **kwargs) -> str:
        try:
            from app.core.plugin import PluginManager
            plugins = PluginManager().running_plugins
            plugin = plugins.get("ClSearch") or plugins.get("clsearch")
            if not plugin:
                return "观影磁力搜插件未运行"

            result = plugin._api_recursive_rename(data={
                "cid": cid,
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
