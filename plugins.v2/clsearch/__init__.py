"""
观影磁力搜插件

搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。
支持Cookie认证和账号密码自动登录，自动解析搜索结果和磁力链接。
"""

import re
import json
import hashlib
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, quote

import requests

from app.core.event import Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class ClSearch(_PluginBase):
    """观影磁力搜插件"""

    # 插件名称
    plugin_name = "观影磁力搜"
    # 插件描述
    plugin_desc = "搜索影视磁力资源，选择版本后直接添加到115网盘离线下载。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    # 插件版本
    plugin_version = "1.2.2"
    # 插件作者
    plugin_author = "local"
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
    _site_auth_mode = "cookie"  # "cookie" 或 "account"
    _site_cookie = ""
    _site_username = ""
    _site_password = ""
    _p115_cookie = ""
    _save_dir_id = ""
    _save_path = ""
    _use_mp_rename = False

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
        self._site_auth_mode = "cookie"
        self._site_cookie = ""
        self._site_username = ""
        self._site_password = ""
        self._p115_cookie = ""
        self._save_dir_id = ""
        self._save_path = ""
        self._use_mp_rename = False
        self._session = None

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._site_url = str(config.get("site_url") or "").rstrip("/")
        self._site_auth_mode = str(config.get("site_auth_mode") or "cookie")
        self._site_cookie = str(config.get("site_cookie") or "")
        self._site_username = str(config.get("site_username") or "")
        self._site_password = str(config.get("site_password") or "")
        self._p115_cookie = str(config.get("p115_cookie") or "")
        self._save_dir_id = str(config.get("save_dir_id") or "")
        self._save_path = str(config.get("save_path") or "")
        self._use_mp_rename = bool(config.get("use_mp_rename"))

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表"""
        return [
            {
                "cmd": "/clsearch",
                "event": "clsearch",
                "desc": "观影搜",
                "category": "观影搜",
                "data": {
                    "action": "search",
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
                "summary": "账号密码登录站点",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "use_mp_rename",
                                            "label": "使用MP重命名整理",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ========== Tab导航 ==========
                    {
                        "component": "VTabs",
                        "props": {
                            "model": "_tabs",
                            "style": {
                                "margin-top": "8px",
                                "margin-bottom": "16px",
                            },
                            "stacked": True,
                            "fixed-tabs": True,
                        },
                        "content": [
                            {
                                "component": "VTab",
                                "props": {"value": "site_tab"},
                                "text": "网站配置",
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "p115_tab"},
                                "text": "115网盘配置",
                            },
                        ],
                    },
                    # ========== Tab内容 ==========
                    {
                        "component": "VWindow",
                        "props": {"model": "_tabs"},
                        "content": [
                            # ---------- 网站配置 ----------
                            {
                                "component": "VWindowItem",
                                "props": {"value": "site_tab"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {
                                            "style": {"margin-top": "8px"},
                                        },
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "site_url",
                                                            "label": "站点地址",
                                                            "placeholder": "https://www.example.com",
                                                            "hint": "影视资源站点的基础URL，不含末尾斜杠",
                                                            "persistent-hint": True,
                                                        },
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
                                                "props": {"cols": 12},
                                                "content": [
                                                    {
                                                        "component": "VRadioGroup",
                                                        "props": {
                                                            "model": "site_auth_mode",
                                                            "inline": True,
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "VRadio",
                                                                "props": {
                                                                    "label": "账号密码登录（推荐）",
                                                                    "value": "account",
                                                                },
                                                            },
                                                            {
                                                                "component": "VRadio",
                                                                "props": {
                                                                    "label": "手动Cookie",
                                                                    "value": "cookie",
                                                                },
                                                            },
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    # 账号密码登录区域
                                    {
                                        "component": "div",
                                        "props": {
                                            "v-if": "site_auth_mode === 'account'",
                                        },
                                        "content": [
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
                                                                    "model": "site_username",
                                                                    "label": "站点用户名",
                                                                    "placeholder": "输入站点登录用户名",
                                                                    "hint": "支持用户名或邮箱登录",
                                                                    "persistent-hint": True,
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
                                                                    "model": "site_password",
                                                                    "label": "站点密码",
                                                                    "placeholder": "输入站点登录密码",
                                                                    "type": "password",
                                                                    "hint": "密码仅用于自动登录，保存在本地配置中",
                                                                    "persistent-hint": True,
                                                                },
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
                                                        "props": {"cols": 12},
                                                        "content": [
                                                            {
                                                                "component": "VAlert",
                                                                "props": {
                                                                    "type": "info",
                                                                    "variant": "tonal",
                                                                    "density": "compact",
                                                                },
                                                                "text": "账号密码模式下，插件会自动登录获取Cookie并保持会话。若遇到验证码，可在浏览器登录后切换为手动Cookie模式。",
                                                            }
                                                        ],
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                    # 手动Cookie区域
                                    {
                                        "component": "div",
                                        "props": {
                                            "v-if": "site_auth_mode === 'cookie'",
                                        },
                                        "content": [
                                            {
                                                "component": "VRow",
                                                "content": [
                                                    {
                                                        "component": "VCol",
                                                        "props": {"cols": 12},
                                                        "content": [
                                                            {
                                                                "component": "VTextarea",
                                                                "props": {
                                                                    "model": "site_cookie",
                                                                    "label": "站点Cookie",
                                                                    "placeholder": "粘贴从浏览器复制的完整Cookie字符串",
                                                                    "hint": "从浏览器开发者工具(F12)中复制完整Cookie，需包含app_auth和browser_verified等字段",
                                                                    "persistent-hint": True,
                                                                    "rows": 3,
                                                                    "variant": "outlined",
                                                                    "density": "compact",
                                                                },
                                                            }
                                                        ],
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                            # ---------- 115网盘配置 ----------
                            {
                                "component": "VWindowItem",
                                "props": {"value": "p115_tab"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {
                                            "style": {"margin-top": "8px"},
                                        },
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12},
                                                "content": [
                                                    {
                                                        "component": "VTextarea",
                                                        "props": {
                                                            "model": "p115_cookie",
                                                            "label": "115网盘Cookie",
                                                            "placeholder": "输入115网盘的Cookie",
                                                            "hint": "用于115离线下载的认证Cookie，建议粘贴完整的Cookie字符串",
                                                            "persistent-hint": True,
                                                            "rows": 3,
                                                            "variant": "outlined",
                                                            "density": "compact",
                                                        },
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
                                                "props": {"cols": 12, "md": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "save_dir_id",
                                                            "label": "离线下载目录ID",
                                                            "placeholder": "例如：123456789",
                                                            "hint": "115网盘中保存离线下载文件的目录ID",
                                                            "persistent-hint": True,
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
                                                            "model": "save_path",
                                                            "label": "保存路径（可选）",
                                                            "placeholder": "例如：/影视/电影",
                                                            "hint": "在目录ID下的相对路径，留空则保存到目录根",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "site_url": "",
            "site_auth_mode": "account",
            "site_cookie": "",
            "site_username": "",
            "site_password": "",
            "p115_cookie": "",
            "save_dir_id": "",
            "save_path": "",
            "use_mp_rename": False,
            "_tabs": "site_tab",
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页"""
        return []

    # ==================== 登录相关方法 ====================

    def _site_login(self) -> Tuple[bool, str]:
        """使用账号密码登录站点，获取Cookie

        Returns:
            (success, message) 元组
        """
        with self._login_lock:
            if not self._site_url:
                return False, "未配置站点地址"

            if not self._site_username or not self._site_password:
                return False, "未配置站点用户名或密码"

            try:
                login_url = f"{self._site_url}/user/login"
                logger.info(f"正在登录站点: {login_url}")

                # 创建新的Session以自动管理Cookie
                session = requests.Session()
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Referer": login_url,
                    "Origin": self._site_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                })

                # 构建登录表单数据
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

                # 尝试解析JSON响应
                try:
                    result = resp.json()
                    code = result.get("code")
                    msg = result.get("msg", "")

                    if code == 200:
                        # 登录成功，提取Cookie字符串
                        cookie_dict = session.cookies.get_dict()
                        if cookie_dict:
                            cookie_str = "; ".join(
                                f"{k}={v}" for k, v in cookie_dict.items()
                            )
                            self._session = session
                            self._site_cookie = cookie_str
                            logger.info(f"站点登录成功，获取到 {len(cookie_dict)} 个Cookie字段")
                            return True, "登录成功"
                        else:
                            # 检查Set-Cookie头
                            if resp.cookies:
                                cookie_str = "; ".join(
                                    f"{k}={v}" for k, v in resp.cookies.items()
                                )
                                self._session = session
                                self._site_cookie = cookie_str
                                logger.info("站点登录成功（从Set-Cookie获取）")
                                return True, "登录成功"
                            return False, "登录响应成功但未获取到Cookie"
                    else:
                        # 登录失败
                        error_msg = msg or f"错误码: {code}"
                        logger.error(f"站点登录失败: {error_msg}")
                        return False, f"登录失败: {error_msg}"

                except (json.JSONDecodeError, ValueError):
                    # 非JSON响应，检查是否通过Cookie判断登录成功
                    # 某些站点登录成功后会302重定向
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
        """确保站点已认证（Cookie有效）

        账号密码模式下，如果没有有效的Session则自动登录。
        Cookie模式下，检查Cookie是否已配置。

        Returns:
            True表示已认证，False表示认证失败
        """
        if self._site_auth_mode == "account":
            # 账号密码模式：如果Session不存在，尝试登录
            if not self._session:
                success, msg = self._site_login()
                if not success:
                    logger.error(f"自动登录失败: {msg}")
                    return False
            return True
        else:
            # Cookie模式：检查Cookie是否已配置
            return bool(self._site_cookie)

    def _get_headers(self) -> dict:
        """构建带Cookie的请求头"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if self._site_cookie:
            headers["Cookie"] = self._site_cookie
        return headers

    def _do_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发起HTTP请求，自动处理认证

        账号密码模式下使用Session（自动携带Cookie），
        Cookie模式下使用普通请求。

        Args:
            method: HTTP方法 (GET, POST等)
            url: 请求URL
            **kwargs: 传递给requests的其他参数

        Returns:
            requests.Response对象
        """
        kwargs.setdefault("timeout", 30)

        if self._site_auth_mode == "account" and self._session:
            # 使用已登录的Session
            return self._session.request(method, url, **kwargs)
        else:
            # 使用普通请求 + Cookie头
            headers = kwargs.pop("headers", {})
            merged_headers = self._get_headers()
            merged_headers.update(headers)
            kwargs["headers"] = merged_headers
            return requests.request(method, url, **kwargs)

    def _api_login(self) -> dict:
        """API: 手动触发站点登录"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        if self._site_auth_mode != "account":
            return {"success": False, "message": "当前为Cookie模式，无需登录"}

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
            if self._site_auth_mode == "account":
                return {"success": False, "message": "站点自动登录失败，请检查用户名和密码"}
            else:
                return {"success": False, "message": "未配置站点Cookie，请从浏览器开发者工具中复制"}

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

            resp = self._do_request("GET", search_url, params=params)
            resp.raise_for_status()

            # 检查是否返回了登录页面或验证页面
            if "browser_verified" in resp.text and "PoW" in resp.text:
                # 账号密码模式下尝试重新登录
                if self._site_auth_mode == "account":
                    self._session = None
                    success, msg = self._site_login()
                    if success:
                        resp = self._do_request("GET", search_url, params=params)
                        resp.raise_for_status()
                    else:
                        return {"success": False, "message": f"Cookie失效且重新登录失败: {msg}"}
                else:
                    return {
                        "success": False,
                        "message": "Cookie已失效或缺少browser_verified字段，请重新从浏览器复制完整Cookie",
                    }

            # 解析搜索结果页面
            results = self._parse_search_page(resp.text)

            # 缓存搜索结果
            cache_key = f"{keyword}:{search_type}:{page}"
            self._search_cache[cache_key] = results

            if not results:
                return {"success": True, "message": "未找到相关资源", "data": []}

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
        """解析搜索结果页面，提取种子列表"""
        results = []

        try:
            # 使用正则解析表格行
            # 匹配每个种子行：标题链接、大小、做种数、更新时间
            # 格式: <td><a href="/bt/xxxxx" ...>标题</a></td><td>大小</td><td>做种</td><td>时间</td>
            row_pattern = re.compile(
                r'<td[^>]*>\s*<a\s+href="(/bt/[^"]+)"[^>]*>(.*?)</a>\s*</td>'
                r'\s*<td[^>]*>(.*?)</td>'
                r'\s*<td[^>]*>(.*?)</td>'
                r'\s*<td[^>]*>(.*?)</td>',
                re.DOTALL,
            )

            for match in row_pattern.finditer(html_content):
                try:
                    detail_path = match.group(1).strip()
                    # 清理标题中的HTML标签
                    title_raw = match.group(2)
                    title = re.sub(r'<[^>]+>', '', title_raw).strip()
                    size_text = re.sub(r'<[^>]+>', '', match.group(3)).strip()
                    seeders = re.sub(r'<[^>]+>', '', match.group(4)).strip()
                    update_time = re.sub(r'<[^>]+>', '', match.group(5)).strip()

                    if not title or not detail_path:
                        continue

                    # 生成唯一ID
                    unique_id = hashlib.md5(detail_path.encode()).hexdigest()[:12]

                    results.append({
                        "id": unique_id,
                        "title": title,
                        "size": size_text,
                        "seeders": seeders,
                        "update_time": update_time,
                        "detail_path": detail_path,
                        "detail_url": f"{self._site_url}{detail_path}",
                    })

                except Exception as e:
                    logger.warning(f"解析搜索结果行失败: {e}")
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
            if self._site_auth_mode == "account":
                return {"success": False, "message": "站点自动登录失败，请检查用户名和密码"}
            else:
                return {"success": False, "message": "未配置站点Cookie"}

        try:
            detail_url = f"{self._site_url}{detail_path}"
            logger.info(f"获取资源详情: {detail_url}")

            resp = self._do_request("GET", detail_url)
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

    def _parse_detail_page(self, html_content: str) -> Optional[dict]:
        """解析详情页面，提取磁力链接和文件信息"""
        try:
            # 提取标题
            title = ""
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()

            # 提取磁力链接
            magnet = ""
            magnet_match = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', html_content)
            if magnet_match:
                magnet = magnet_match.group(1)

            # 提取种子下载链接
            torrent_url = ""
            torrent_match = re.search(r'href="(/dbt/[^"]+)"', html_content)
            if torrent_match:
                torrent_url = f"{self._site_url}{torrent_match.group(1)}"

            # 提取离线下载链接
            offline_url = ""
            offline_match = re.search(r'href="(https?://keepshare\.org/[^"]+)"', html_content)
            if offline_match:
                offline_url = offline_match.group(1)

            # 提取文件列表
            files = []
            file_pattern = re.compile(r'<li[^>]*class="[^"]*file[^"]*"[^>]*>(.*?)</li>', re.DOTALL)
            for file_match in file_pattern.finditer(html_content):
                file_name = re.sub(r'<[^>]+>', '', file_match.group(1)).strip()
                if file_name:
                    files.append(file_name)

            # 提取文件大小信息（从文件列表中）
            file_size_pattern = re.compile(r'\((\d+\.?\d*\s*[KMGT]B)\)')
            file_sizes = []
            for size_match in file_size_pattern.finditer(html_content):
                file_sizes.append(size_match.group(1))

            return {
                "title": title,
                "magnet": magnet,
                "torrent_url": torrent_url,
                "offline_url": offline_url,
                "files": files,
                "file_sizes": file_sizes,
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
            from p115client import P115Client

            # 创建115客户端
            client = P115Client(self._p115_cookie)

            # 构建离线下载参数
            params = {
                "url": magnet,
                "savepath": self._save_path or "",
                "wp_path_id": self._save_dir_id,
            }

            logger.info(f"添加115离线下载: {title}")

            # 调用离线下载API
            result = client.clouddownload_task_add_url(**params)

            if result and result.get("state"):
                logger.info(f"115离线下载添加成功: {title}")
                return {
                    "success": True,
                    "message": f"已添加到115离线下载: {title}",
                    "data": result,
                }
            else:
                error_msg = result.get("error") or result.get("message") or "未知错误"
                logger.error(f"115离线下载添加失败: {error_msg}")
                return {
                    "success": False,
                    "message": f"添加失败: {error_msg}",
                    "data": result,
                }

        except ImportError:
            return {"success": False, "message": "p115client未安装，请先安装依赖"}
        except Exception as e:
            logger.error(f"115离线下载异常: {e}")
            return {"success": False, "message": f"下载异常: {str(e)}"}

    def handle_event(self, event: Event) -> None:
        """处理事件"""
        if event.event_type == "clsearch":
            data = event.event_data or {}
            action = data.get("action")

            if action == "search":
                keyword = data.get("keyword") or ""
                if keyword:
                    result = self._api_search(keyword)
                    # 发送搜索结果通知
                    if result.get("success") and result.get("data"):
                        self._send_search_results(keyword, result["data"])
                    else:
                        self.post_message(
                            title="观影搜",
                            content=result.get("message", "搜索失败"),
                            notification_type=NotificationType.Warning,
                        )

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

    def stop_service(self) -> None:
        """停止插件服务"""
        self._search_cache.clear()
        self._session = None
        logger.info("观影搜插件服务已停止")
