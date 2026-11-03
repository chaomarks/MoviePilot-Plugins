"""观影磁力搜 —— MoviePilot 智能体工具。

工具只负责参数解析和结果格式化，具体能力全部委托给插件与 OfflineOrganizer。
"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.log import logger


def _get_clsearch_plugin():
    """Return the live ClSearch plugin instance used by agent tools."""
    try:
        from . import _CLSEARCH_ACTIVE_PLUGIN
    except ImportError:
        _CLSEARCH_ACTIVE_PLUGIN = None

    plugin = _CLSEARCH_ACTIVE_PLUGIN
    if plugin and getattr(plugin, "_enabled", False):
        return plugin
    try:
        from app.core.plugin import PluginManager

        plugins = PluginManager().running_plugins
        plugin = plugins.get("ClSearch") or plugins.get("clsearch")
        if plugin:
            import sys

            package = sys.modules.get(__package__ or "")
            if package is not None:
                package._CLSEARCH_ACTIVE_PLUGIN = plugin
        return plugin
    except Exception as e:
        logger.warning(f"获取观影磁力搜运行实例失败: {e}")
        return None


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
        "只有用户明确要求下载/离线到115时，才在用户选定资源后调用 cl_search_offline_result；"
        "用户只是想看看有哪些资源时，只需展示搜索结果，不要自动离线。\n"
        "如果用户要整理已经下载好的离线文件，请改用 cl_search_list_offline 和 cl_search_organize_offline。"
    )
    args_schema: Type[BaseModel] = ClSearchSearchInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        keyword = kwargs.get("keyword", "")
        return f"正在搜索磁力资源：{keyword}"

    async def run(self, keyword: str, **kwargs) -> str:
        try:
            plugin = _get_clsearch_plugin()
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
            plugin = _get_clsearch_plugin()
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
            plugin = _get_clsearch_plugin()
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
                "_session": {
                    "channel": self._channel,
                    "source": self._source,
                    "userid": self._user_id,
                    "username": self._username,
                },
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
            plugin = _get_clsearch_plugin()
            if not plugin:
                return "观影磁力搜插件未运行"

            result = plugin._api_offline_download(data={
                "magnet": magnet,
                "title": title,
                "_session": {
                    "channel": self._channel,
                    "source": self._source,
                    "userid": self._user_id,
                    "username": self._username,
                },
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
            plugin = _get_clsearch_plugin()
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
            plugin = _get_clsearch_plugin()
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

class ClSearchMoveFileInput(BaseModel):
    """移动文件输入"""
    source_cid: str = Field(description="要移动的源文件夹CID（115网盘目录ID）")
    target_path: str = Field(description="目标入库目录的115网盘完整路径，如 '/媒体库/电影' 或 '/媒体库/电视剧'")
    task_name: str = Field(default="", description="任务名称（离线下载时的标题），移动成功后自动清除待整理记录")

class ClSearchMoveFileTool(MoviePilotTool):
    """115网盘文件移动工具"""
    name: str = "cl_search_move_file"
    description: str = (
        "将115网盘文件夹移动到指定目标目录。"
        "插件会根据目标路径自动查找目录CID并执行移动，无需智能体拥有文件操作权限。"
        "source_cid为源文件夹CID，target_path为目标目录的115网盘完整路径。"
        "移动成功后会自动清除对应的待整理任务记录。"
    )
    args_schema: Type[BaseModel] = ClSearchMoveFileInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        target_path = kwargs.get("target_path", "")
        return f"正在移动文件到: {target_path}..."

    async def run(self, source_cid: str, target_path: str, task_name: str = "", **kwargs) -> str:
        try:
            plugin = _get_clsearch_plugin()
            if not plugin:
                return "观影磁力搜插件未运行"

            # 1. 根据目标路径查找CID
            target_cid = plugin._find_cid_by_path(target_path)
            if not target_cid:
                return f"移动失败：未找到目标目录路径: {target_path}，请确认路径是否正确"

            # 2. 执行移动
            move_result = plugin._api_move_file({
                "cid": source_cid,
                "target_cid": target_cid,
                "target_path": target_path,
                "type": "folder",
            })
            if move_result.get("success"):
                msg = f"✅ 文件移动成功: CID {source_cid} -> {target_path} (CID: {target_cid})"
                # 3. 自动清除待整理任务记录
                if task_name:
                    plugin.clear_pending_transfer_task(task_name)
                    msg += f"\n已清除待整理任务: {task_name}"
                return msg
            else:
                return f"❌ 文件移动失败: {move_result.get('message', '未知错误')}"
        except Exception as e:
            return f"移动文件异常: {str(e)}"

class ClSearchListOfflineInput(BaseModel):
    """列出离线目录入参"""

    keyword: str = Field(default="", description="可选，按名称过滤文件夹，例如剧名")


class ClSearchListOfflineTool(MoviePilotTool):
    """列出115离线下载目录内容工具"""

    name: str = "cl_search_list_offline"
    description: str = (
        "列出插件配置的115离线下载目录中的文件夹和文件，包含名称、CID、大小和修改时间。\n\n"
        "当用户说'看看离线目录里有什么'、'整理我手动离线下载的xxx'时，"
        "先调用此工具找到目标文件夹，再把 folder_name 或 cid 传给 cl_search_organize_offline。"
    )
    args_schema: Type[BaseModel] = ClSearchListOfflineInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        keyword = kwargs.get("keyword") or ""
        return f"正在查看115离线目录{f'（筛选: {keyword}）' if keyword else ''}..."

    async def run(self, keyword: str = "", **kwargs) -> str:
        try:
            plugin = _get_clsearch_plugin()
            if not plugin:
                return "观影磁力搜插件未运行，请先启用插件并配置站点信息"
            result = plugin.list_offline_entries(keyword=keyword)
            if not result.get("success"):
                return f"读取离线目录失败: {result.get('message', '未知错误')}"
            items = result.get("data") or []
            if not items:
                return f"115离线目录为空{'，未找到匹配项' if keyword else ''}"
            lines = [f"115离线目录（{result.get('path') or ''}）共 {len(items)} 项:"]
            for index, item in enumerate(items[:50], 1):
                kind = "文件夹" if item.get("type") == "dir" else "文件"
                extra = []
                if item.get("size_text"):
                    extra.append(item["size_text"])
                if item.get("modified"):
                    extra.append(item["modified"])
                suffix = f" | {' | '.join(extra)}" if extra else ""
                lines.append(
                    f"{index}. [{kind}] {item.get('name')} | CID: {item.get('cid')}{suffix}"
                )
            if len(items) > 50:
                lines.append(f"... 还有 {len(items) - 50} 项未显示")
            lines.append("\n需要整理时，把文件夹名或CID传给 cl_search_organize_offline。")
            return "\n".join(lines)
        except Exception as e:
            return f"读取离线目录失败: {str(e)}"


class ClSearchOrganizeOfflineInput(BaseModel):
    """整理离线目录入参"""

    folder_name: str = Field(default="", description="离线目录中的文件夹名称，例如 '流浪地球2.2023'")
    cid: str = Field(default="", description="文件夹CID；已知时优先使用，可替代 folder_name")
    target_path: str = Field(default="", description="可选，整理后移动到的115目标目录完整路径，如 '/媒体库/电影'")
    move: bool = Field(default=False, description="是否在整理完成后立即移动；为 true 时必须提供 target_path")


class ClSearchOrganizeOfflineTool(MoviePilotTool):
    """整理115离线目录中的指定文件夹工具"""

    name: str = "cl_search_organize_offline"
    description: str = (
        "整理115离线下载目录中已经下载完成的文件夹（包括用户手动离线下载的文件）。"
        "工具会识别媒体信息、按MoviePilot标准重命名、创建Season目录、清理小体积广告和非媒体杂项，"
        "并记录待整理任务。\n\n"
        "使用方式：\n"
        "1. 不传参数时返回离线目录候选列表；\n"
        "2. 传 folder_name 或 cid 整理指定文件夹；\n"
        "3. 传 target_path 或 move=true 可在整理后直接移动到入库目录；\n"
        "4. 整理完成后返回 folder_cid 和 final_path，需要入库时也可以再调用 cl_search_move_file。"
    )
    args_schema: Type[BaseModel] = ClSearchOrganizeOfflineInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        folder_name = kwargs.get("folder_name") or kwargs.get("cid") or ""
        return f"正在整理离线文件夹: {folder_name}..."

    async def run(self, folder_name: str = "", cid: str = "", target_path: str = "", move: bool = False, **kwargs) -> str:
        try:
            plugin = _get_clsearch_plugin()
            if not plugin:
                return "观影磁力搜插件未运行"
            result = plugin.organize_offline(
                folder_name=folder_name,
                cid=cid,
                target_path=target_path,
                move=move,
            )
            if not result.get("success"):
                message = result.get("message", "整理失败")
                candidates = result.get("data") or []
                if candidates:
                    lines = [message, "离线目录中的文件夹:"]
                    for index, item in enumerate(candidates[:30], 1):
                        lines.append(f"{index}. {item.get('name')} | CID: {item.get('cid')}")
                    return "\n".join(lines)
                return message

            lines = [
                f"✅ {result.get('message') or '整理完成'}",
                f"文件夹: {result.get('new_folder_name')}",
                f"CID: {result.get('folder_cid')}",
                f"路径: {result.get('final_path')}",
            ]
            if result.get("media_type"):
                lines.append(
                    f"媒体: {result.get('title')} ({result.get('year')}) | 类型: {result.get('media_type')} | TMDB: {result.get('tmdb_id')}"
                )
            need_files = result.get("needs_agent_files") or []
            if need_files:
                lines.append(f"\n以下 {len(need_files)} 个文件未自动处理，需要确认:")
                for item in need_files[:20]:
                    lines.append(
                        f"- {item.get('name')} | CID: {item.get('cid')} | {item.get('size_text')} | {item.get('reason')}"
                    )
                if len(need_files) > 20:
                    lines.append(f"- 还有 {len(need_files) - 20} 个未列出")
            move_result = result.get("move")
            if move_result:
                lines.append(f"\n移动结果: {'成功' if move_result.get('success') else '失败'} - {move_result.get('message', '')}")
            else:
                lines.append(
                    "\n如需入库，请根据MP目录映射确定目标路径后调用 cl_search_move_file，"
                    f"source_cid={result.get('folder_cid')}。"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"整理离线文件夹失败: {str(e)}"
