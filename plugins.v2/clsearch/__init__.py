"""
观影磁力搜插件

搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。
支持Cookie认证和账号密码自动登录，自动解析搜索结果和磁力链接。
"""

import re
import json
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type
from urllib.parse import urljoin, quote

import requests
from pydantic import BaseModel, Field

from app.core.event import Event, eventmanager
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
    plugin_version = "1.5.0"
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

    # 搜索 & 离线历史记录
    _search_history: List[Dict[str, Any]] = []
    _offline_history: List[Dict[str, Any]] = []

    # 搜索结果缓存
    _search_cache: Dict[str, Any] = {}
    # 登录会话（账号密码模式下保持会话）
    _session: Optional[requests.Session] = None
    _login_lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件"""
        self.stop_service()
        self._enabled = False
        self._site_url = ""
        self._site_cookie = ""
        self._site_username = ""
        self._site_password = ""
        self._session = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._site_url = str(config.get("site_url") or "").rstrip("/")
        self._site_username = str(config.get("site_username") or "")
        self._site_password = str(config.get("site_password") or "")

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
                "path": "search",
                "endpoint": self._api_search,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "搜索磁力资源",
            },
            {
                "path": "detail",
                "endpoint": self._api_detail,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取资源详情和磁力链接",
            },
            {
                "path": "offline",
                "endpoint": self._api_offline_download,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "添加115离线下载",
            },
            {
                "path": "login",
                "endpoint": self._api_login,
                "methods": ["POST"],
                "auth": "bear",
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
                                            "hint": "从115网盘地址栏获取19位数字",
                                            "persistent-hint": True,
                                            "density": "compact",
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
            headers["Cookie"] = self._site_cookie
        return headers

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

    def _api_offline_download(self, data: dict = None) -> dict:
        """API: 添加115离线下载

        Args:
            data: 下载信息，包含 magnet 和 title
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

        try:
            url = "https://webapi.115.com/cloud_download/add_task_url"
            headers = {
                "Cookie": self._p115_cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://115.com/",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            payload = {
                "url": magnet,
                "wp_path_id": self._save_dir_id,
            }

            logger.info(f"添加115离线下载: {title}")

            resp = requests.post(url, data=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            result = resp.json()

            if result and result.get("state"):
                logger.info(f"115离线下载添加成功: {title}")
                self._record_offline_history(title, True)
                return {
                    "success": True,
                    "message": f"已添加到115离线下载: {title}",
                    "data": result,
                }
            else:
                error_msg = result.get("error") or result.get("message") or "未知错误"
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
        """发送搜索结果通知"""
        content_parts = [f"**搜索结果: {keyword}**\n"]

        for i, item in enumerate(results[:10], 1):
            title = item.get("title", "")
            size = item.get("size", "")
            seeders = item.get("seeders", "")
            update_time = item.get("update_time", "")

            content_parts.append(
                f"{i}. **{title}**\n"
                f"   大小: {size} | 做种: {seeders} | 更新: {update_time}"
            )

        if len(results) > 10:
            content_parts.append(f"\n... 还有 {len(results) - 10} 个结果")

        self.post_message(
            title="观影搜结果",
            content="\n".join(content_parts),
            notification_type=NotificationType.Information,
        )

    def get_agent_tools(self) -> List[type]:
        """获取插件智能体工具，供内置AI智能体调用"""
        return [ClSearchSearchTool, ClSearchDetailTool, ClSearchOfflineTool]

    def stop_service(self) -> None:
        """停止插件服务"""
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


class ClSearchSearchTool(MoviePilotTool):
    """搜索磁力资源工具"""
    name: str = "cl_search_search"
    description: str = (
        "搜索影视磁力资源。输入关键词，返回搜索结果列表，包含标题、大小、做种数、更新时间"
        "和 detail_path 字段。detail_path 可用于后续获取磁力链接详情。"
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
                    f"\n{i}. {item['title']}\n"
                    f"   大小: {item['size']} | 做种: {item['seeders']} | 更新: {item['update_time']}\n"
                    f"   detail_path: {item['detail_path']}"
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
                return f"✅ 已成功添加到115离线下载: {title}\n{result.get('message', '')}"
            else:
                return f"离线下载失败: {result.get('message', '未知错误')}"
        except Exception as e:
            return f"离线下载失败: {str(e)}"
