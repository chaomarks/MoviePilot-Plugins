"""观影磁力搜 —— 115 离线目录扫描与整理模块。

整理流程与自动/手动入口共用同一套实现：
- list_offline_entries：列出配置的离线目录内容
- organize_offline：识别媒体、标准命名、创建 Season、清理广告、记录待整理
- move_folder：把文件夹移动到入库目录

OfflineOrganizer 通过 __getattr__ 复用插件实例上的配置和工具方法，
避免把 115 Cookie、通知、历史记录等状态复制一份。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.schemas import NotificationType
from app.schemas.types import MediaType as MMediaType

try:
    from p115client import P115Client
except ImportError:
    P115Client = None


VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m2ts",
    ".rmvb", ".rm", ".iso", ".vob", ".mpg", ".mpeg", ".m4v", ".3gp", ".f4v",
)


class OfflineOrganizer:
    """115 离线目录整理器。

    所有配置与通用工具都从插件实例读取，整理逻辑集中在本模块。
    """

    def __init__(self, plugin: Any):
        self._plugin = plugin

    def __getattr__(self, name: str) -> Any:
        # 复用插件实例上的配置、通知、115 客户端等能力
        return getattr(self._plugin, name)

    # ==================== 对外入口 ====================

    def list_offline_entries(self, keyword: str = "") -> Dict[str, Any]:
        """列出配置的 115 离线下载目录内容。

        注意：115 fs_files 返回中，文件的 `cid` 是其父目录ID，真正的文件ID
        需要通过 StorageChain.list_files 的 FileItem.fileid 获取。
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if not self._p115_cookie:
            return {"success": False, "message": "未配置115网盘Cookie"}
        if not self._save_dir_id:
            return {"success": False, "message": "未配置115离线下载目录ID"}

        keyword = str(keyword or "").strip().lower()
        entries: List[Dict[str, Any]] = []
        # 用 StorageChain 拿真实 fileid（文件的 cid 字段是父目录ID，不能用于移动）
        file_ids: Dict[str, str] = {}
        try:
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            parent_name = self._get_cid_name(self._save_dir_id) or ""
            parent_item = FileItem(
                storage="u115",
                fileid=self._save_dir_id,
                type="dir",
                name=parent_name,
                path=self._join_u115_path(self._resolved_path) if self._resolved_path else "",
            )
            for fi in StorageChain().list_files(parent_item) or []:
                if str(getattr(fi, "type", "") or "") != "file":
                    continue
                fi_name = str(getattr(fi, "name", "") or "")
                fi_id = str(getattr(fi, "fileid", "") or "")
                if fi_name and fi_id:
                    file_ids[fi_name] = fi_id
        except Exception as e:
            logger.warning(f"StorageChain 获取文件ID失败，将退回 cid 字段: {e}")

        try:
            client = P115Client(self._normalize_cookie(self._p115_cookie))
            offset = 0
            while True:
                result = client.fs_files({
                    "cid": self._save_dir_id,
                    "limit": 1000,
                    "offset": offset,
                })
                items = result.get("data") if isinstance(result, dict) else None
                if not items:
                    break
                for item in items:
                    name = str(item.get("n") or item.get("name") or "").strip()
                    if not name:
                        continue
                    if keyword and keyword not in name.lower():
                        continue
                    fc = str(item.get("fc", "") or "")
                    has_size = bool(item.get("s") or item.get("size") or item.get("file_size"))
                    is_dir = fc == "0" or (not fc and not has_size)
                    size = self._parse_size_bytes(item.get("s") or item.get("size") or item.get("file_size"))
                    modified = self._format_ts(item.get("t") or item.get("mtime") or item.get("utime"))
                    # 文件用真实 fileid；目录用 cid
                    entry_id = str(item.get("cid") or "") if is_dir else file_ids.get(name, "")
                    entries.append({
                        "name": name,
                        "cid": entry_id or str(item.get("cid") or ""),
                        "type": "dir" if is_dir else "file",
                        "size": size,
                        "size_text": self._format_size_text(size) if size else "",
                        "modified": modified,
                    })
                if len(items) < 1000:
                    break
                offset += 1000
            return {"success": True, "data": entries, "total": len(entries), "path": self._resolved_path}
        except Exception as e:
            logger.error(f"列出115离线目录失败: {e}")
            return {"success": False, "message": f"列出115离线目录失败: {e}"}

    def organize_offline(
        self,
        folder_name: str = "",
        cid: str = "",
        target_path: str = "",
        move: bool = False,
    ) -> Dict[str, Any]:
        """整理离线目录中的指定文件夹或散落文件。

        folder_name 与 cid 至少提供一个；都不提供时返回目录候选列表。
        cid 指向文件时按“散落文件整理”：根据文件名识别媒体，归入同一个
        标准命名的剧集/电影目录后再整体移动。
        target_path 有值或 move=True 时，整理完成后立即移动到目标目录。
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if not self._p115_cookie:
            return {"success": False, "message": "未配置115网盘Cookie"}
        if not self._save_dir_id:
            return {"success": False, "message": "未配置115离线下载目录ID"}

        folder_name = str(folder_name or "").strip()
        cid = str(cid or "").strip()

        if not self._resolved_path:
            try:
                resolved = self._resolve_cid_path(self._save_dir_id)
                if resolved:
                    self._plugin._resolved_path = resolved
            except Exception as e:
                logger.warning(f"整理前解析离线目录路径失败: {e}")

        if not self._resolved_path:
            return {"success": False, "message": "未解析离线目录路径，请在插件配置中保存一次目录CID"}

        if not folder_name and not cid:
            listing = self.list_offline_entries()
            return {
                "success": False,
                "message": "请指定要整理的文件夹名称或CID",
                "data": listing.get("data") or [],
                "path": listing.get("path") or self._resolved_path,
            }

        if cid:
            folder_name = folder_name or self._get_cid_name(cid)
            if not folder_name:
                return {"success": False, "message": f"无法通过CID获取文件夹名称: {cid}"}
            folder_cid = cid
        else:
            try:
                client = P115Client(self._normalize_cookie(self._p115_cookie))
                folder_cid = self._find_download_folder_cid(client, folder_name)
            except Exception as e:
                return {"success": False, "message": f"查找离线文件夹失败: {e}"}
            if not folder_cid:
                listing = self.list_offline_entries()
                candidates = [
                    item for item in (listing.get("data") or [])
                    if item.get("type") == "dir"
                ]
                return {
                    "success": False,
                    "message": f"离线目录中未找到文件夹: {folder_name}",
                    "data": candidates,
                    "path": self._resolved_path,
                }

        # 判断 cid 指向的是文件夹还是散落的文件
        entry_type = self._probe_entry_type(folder_cid, folder_name)
        if entry_type == "file":
            return self._organize_loose_files(
                file_cid=folder_cid,
                file_name=folder_name,
                target_path=target_path,
            )

        result = self._trigger_transfer(folder_name, {"folder_cid": folder_cid, "name": folder_name}, notify=False)
        if not isinstance(result, dict):
            return {"success": False, "message": "整理失败：插件未返回结果"}
        if result.get("success") is False:
            result.setdefault("message", "整理失败")
            return result

        payload: Dict[str, Any] = {
            "success": True,
            "message": (
                f"整理完成: {folder_name}"
                + (
                    f" -> {result.get('new_folder_name')}"
                    if result.get("new_folder_name") and result.get("new_folder_name") != folder_name
                    else ""
                )
            ),
            "folder_cid": result.get("folder_cid") or folder_cid,
            "new_folder_name": result.get("new_folder_name") or folder_name,
            "final_path": result.get("final_path") or "",
            "media_type": result.get("media_type"),
            "title": result.get("title"),
            "year": result.get("year"),
            "tmdb_id": result.get("tmdb_id"),
            "needs_agent_files": result.get("needs_agent_files") or [],
        }

        if target_path or move:
            if not target_path:
                payload["move"] = {"success": False, "message": "未提供目标目录路径 target_path"}
            else:
                payload["move"] = self.move_folder(payload["folder_cid"], target_path, task_name=folder_name)
                if payload["move"].get("success"):
                    payload["message"] = f"整理并移动完成: {payload['new_folder_name']} -> {target_path}"
        return payload

    def _probe_entry_type(self, cid: str, name: str) -> str:
        """判断离线目录中的条目是文件夹还是文件。"""
        try:
            client = P115Client(self._normalize_cookie(self._p115_cookie))
            result = client.fs_files({
                "cid": self._save_dir_id,
                "limit": 1000,
                "offset": 0,
            })
            items = result.get("data") if isinstance(result, dict) else None
            for item in items or []:
                if str(item.get("cid") or "") == str(cid):
                    fc = str(item.get("fc", "") or "")
                    if fc == "0":
                        return "dir"
                    return "file"
        except Exception as e:
            logger.warning(f"探测离线条目类型失败: {e}")
        return "dir"

    def _organize_loose_files(self, file_cid: str, file_name: str, target_path: str = "") -> Dict[str, Any]:
        """整理散落在离线目录中的视频文件（没有文件夹包裹）。

        流程（照搬 MP 整理方案）：
        1. 按文件名识别媒体，得到标题/年份/类型/TMDB ID
        2. 在离线目录创建标准命名的目录（电影：标题 (年份)；剧集：标题 (年份)，内部再建 Season）
        3. 把同一部剧/电影的散落文件都重命名为 MP 标准名并移动进去
        4. 可选直接移动到目标入库目录
        """
        from app.chain.media import MediaChain

        try:
            client = P115Client(self._normalize_cookie(self._p115_cookie))
            # 1. 用 StorageChain 拿到所有散落文件及其真实 fileid
            # （115 fs_files 返回中文件的 cid 是父目录ID，不能用于移动）
            from app.chain.storage import StorageChain
            from app.schemas import FileItem

            storage_chain = StorageChain()
            parent_name = self._get_cid_name(self._save_dir_id) or ""
            parent_path = self._join_u115_path(self._resolved_path) if self._resolved_path else ""
            parent_item = FileItem(
                storage="u115",
                fileid=self._save_dir_id,
                type="dir",
                name=parent_name,
                path=parent_path,
            )
            files: List[Dict[str, Any]] = []
            for fi in storage_chain.list_files(parent_item) or []:
                if str(getattr(fi, "type", "") or "") != "file":
                    continue
                fi_name = str(getattr(fi, "name", "") or "")
                fi_id = str(getattr(fi, "fileid", "") or "")
                if fi_name and fi_id:
                    files.append({
                        "name": fi_name,
                        "cid": fi_id,
                        "path": str(getattr(fi, "path", "") or fi_name),
                    })
            if not files:
                return {"success": False, "message": "离线目录中没有散落文件"}

            # 2. 按第一个文件识别媒体
            media_chain = MediaChain()
            probe_name = file_name or files[0].get("name") or ""
            context = media_chain.recognize_by_path(Path(f"/{probe_name}"), obtain_images=False)
            if not (context and context.media_info):
                # 逐个文件尝试识别
                for item in files:
                    ctx = media_chain.recognize_by_path(Path(f"/{item.get('name')}"), obtain_images=False)
                    if ctx and ctx.media_info:
                        context = ctx
                        break
            if not (context and context.media_info):
                return {
                    "success": False,
                    "message": f"媒体识别失败: {probe_name}，请确认文件名或先重命名",
                }

            mediainfo = context.media_info
            meta = context.meta_info
            media_type_val = getattr(mediainfo, 'type', None)
            if isinstance(media_type_val, MMediaType):
                media_type = media_type_val.to_agent()
            else:
                if hasattr(media_type_val, 'value'):
                    media_type_val = media_type_val.value
                media_type = "tv" if str(media_type_val).lower() in ("tv", "show", "电视剧") else "movie"
            title = getattr(mediainfo, 'title', None) or (meta and getattr(meta, 'title', None))
            year = getattr(mediainfo, 'year', None) or (meta and getattr(meta, 'year', None))
            tmdb_id = getattr(mediainfo, 'tmdb_id', None)

            if tmdb_id:
                year_str = f" ({year})" if year else ""
                new_folder_name = f"{title}{year_str} [tmdbid={tmdb_id}]"
            elif year and title:
                new_folder_name = f"{title} ({year})"
            elif title:
                new_folder_name = str(title)
            else:
                new_folder_name = file_name

            logger.info(f"散落文件整理: 识别为 {media_type} {title} ({year}) tmdb={tmdb_id}, 目标目录名: {new_folder_name}")

            # 3. 在离线目录创建标准目录
            create_result = self._api_create_folder({
                "cid": self._save_dir_id,
                "name": new_folder_name,
                "path": self._resolved_path,
            })
            folder_cid = (create_result.get("data") or {}).get("cid") or ""
            if not folder_cid:
                # 目录可能已存在，从列表里找
                try:
                    result = client.fs_files({"cid": self._save_dir_id, "limit": 1000, "offset": 0})
                    for item in (result.get("data") or []):
                        if str(item.get("n") or "") == new_folder_name:
                            folder_cid = str(item.get("cid") or "")
                            break
                except Exception as e:
                    logger.warning(f"查找已存在目录失败: {e}")
            if not folder_cid:
                return {"success": False, "message": f"创建/查找目录失败: {new_folder_name}"}

            # 4. 剧集建 Season 子目录
            season_dir_cid = ""
            if media_type == "tv":
                create_season = self._api_create_folder({
                    "cid": folder_cid,
                    "name": "Season 1",
                    "path": self._join_u115_path(self._resolved_path, new_folder_name),
                })
                season_dir_cid = (create_season.get("data") or {}).get("cid") or ""

            # 5. 用 TransferChain 生成每个文件的标准名（照搬 MP 命名方案）
            from app.chain.transfer import TransferChain
            transfer_chain = TransferChain()

            moved = 0
            failed = 0
            file_status = []
            dest_cid = season_dir_cid or folder_cid
            dest_path = self._join_u115_path(
                self._resolved_path,
                new_folder_name + ("/Season 1" if media_type == "tv" else ""),
            )
            target_cid_for_files = season_dir_cid or folder_cid

            for item in files:
                name = item.get("name") or ""
                item_cid = item.get("cid") or ""
                item_path = item.get("path") or self._join_u115_path(self._resolved_path, name)
                if not item_cid:
                    continue
                try:
                    ctx = media_chain.recognize_by_path(Path(f"/{name}"), obtain_images=False)
                    if not (ctx and ctx.media_info):
                        # 只整理与识别结果同属一部媒体的文件，识别不出的保留原样
                        file_status.append({"name": name, "status": "跳过（识别失败）"})
                        continue
                    ctx_media_type = getattr(ctx.media_info, 'type', None)
                    if isinstance(ctx_media_type, MMediaType):
                        ctx_type = ctx_media_type.to_agent()
                    else:
                        ctx_type = str(getattr(ctx_media_type, 'value', ctx_media_type) or "").lower()
                        ctx_type = "tv" if ctx_type in ("tv", "show", "电视剧") else "movie"
                    if ctx_type != media_type:
                        file_status.append({"name": name, "status": "跳过（其他媒体）"})
                        continue
                    if tmdb_id and getattr(ctx.media_info, 'tmdb_id', None) not in (None, tmdb_id):
                        file_status.append({"name": name, "status": "跳过（其他媒体）"})
                        continue

                    recommend_path = transfer_chain.recommend_name(meta=ctx.meta_info, mediainfo=ctx.media_info)
                    new_name = Path(recommend_path).name if recommend_path else ""
                    if not new_name:
                        file_status.append({"name": name, "status": "跳过（未生成新名称）"})
                        continue

                    move_result = self._api_move_file({
                        "cid": item_cid,
                        "target_cid": target_cid_for_files,
                        "path": item_path,
                        "name": name,
                        "target_path": dest_path,
                        "type": "file",
                    })
                    if not move_result.get("success"):
                        failed += 1
                        file_status.append({"name": name, "status": f"移动失败: {move_result.get('message')}"})
                        continue
                    if new_name != name:
                        rename_result = self._api_rename({
                            "cid": item_cid,
                            "name": new_name,
                            "path": self._join_u115_path(dest_path, name),
                            "old_name": name,
                            "type": "file",
                        })
                        if rename_result.get("success"):
                            file_status.append({"name": name, "status": f"已整理: {new_name}"})
                        else:
                            file_status.append({"name": name, "status": f"已移动但重命名失败: {rename_result.get('message')}"})
                    else:
                        file_status.append({"name": name, "status": "已移动（名称不变）"})
                    moved += 1
                except Exception as e:
                    failed += 1
                    file_status.append({"name": name, "status": f"异常: {e}"})
                    logger.warning(f"散落文件整理异常: {name}, {e}")

            final_path = dest_path
            message = (
                f"散落文件整理完成: {moved} 个 -> {new_folder_name}"
                + ("/Season 1" if media_type == "tv" and season_dir_cid else "")
            )
            if failed:
                message += f"，失败 {failed} 个"

            payload: Dict[str, Any] = {
                "success": True,
                "message": message,
                "folder_cid": folder_cid,
                "new_folder_name": new_folder_name,
                "final_path": final_path,
                "media_type": media_type,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "moved": moved,
                "failed": failed,
                "files": file_status,
                "needs_agent_files": [],
            }

            # 6. 记录待整理任务，便于智能体后续确认/清理
            self._record_pending_task(new_folder_name, final_path, {
                "rename_result": message,
                "media_type": media_type,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "folder_cid": folder_cid,
                "loose_files": True,
            })

            # 7. 可选直接移动入库
            if target_path:
                payload["move"] = self.move_folder(folder_cid, target_path, task_name=new_folder_name)
                if payload["move"].get("success"):
                    payload["message"] = f"整理并移动完成: {new_folder_name} -> {target_path}"
            return payload
        except Exception as e:
            logger.error(f"散落文件整理异常: {e}")
            return {"success": False, "message": f"散落文件整理异常: {e}"}

    def move_folder(self, cid: str, target_path: str, task_name: str = "") -> Dict[str, Any]:
        """把 115 文件夹移动到目标目录。"""
        cid = str(cid or "").strip()
        target_path = str(target_path or "").strip()
        if not cid or not target_path:
            return {"success": False, "message": "缺少源文件夹CID或目标路径"}
        target_cid = self._find_cid_by_path(target_path)
        if not target_cid:
            return {"success": False, "message": f"未找到目标目录路径: {target_path}"}
        result = self._api_move_file({
            "cid": cid,
            "target_cid": target_cid,
            "target_path": target_path,
            "type": "folder",
        })
        if result.get("success") and task_name:
            self.clear_pending_transfer_task(task_name)
        return result

    # ==================== 内部工具 ====================

    @staticmethod
    def _format_ts(value: Any) -> str:
        try:
            if value:
                return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            pass
        return ""

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
            if result:
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

    def _api_mp_native_recursive_rename(self, data: dict = None) -> dict:
        """Use MoviePilot file manager's native recursive rename logic."""
        data = data or {}
        cid = str(data.get("cid") or data.get("fileid") or "").strip()
        file_path = self._join_u115_path(str(data.get("path") or data.get("file_path") or "").strip())
        new_name = str(data.get("new_name") or data.get("name") or "").strip()
        old_name = self._u115_path_name(file_path) or str(data.get("old_name") or data.get("task_name") or cid).strip()
        storage_name = str(data.get("storage") or "u115").lower()
        cancel_key = str(data.get("info_hash") or "").strip().lower()

        if not new_name:
            return {"success": False, "message": "缺少新名称 new_name", "fallback": False}
        if not cid:
            return {"success": False, "message": "缺少115目录CID，无法重命名", "fallback": False}

        try:
            from app.chain.media import MediaChain
            from app.chain.storage import StorageChain
            from app.chain.transfer import TransferChain
            from app.core.config import settings
            from app.schemas import FileItem

            storage_chain = StorageChain()
            transfer_chain = TransferChain()
            is_tv = str(data.get("media_type") or "").strip().lower() in ("tv", "show", "电视剧", "series")
            folder_item = FileItem(
                storage=storage_name,
                fileid=cid,
                type="dir",
                name=old_name,
                path=file_path,
            )

            logger.info(f"MP原生重命名目录: {old_name} -> {new_name}, cid={cid}")
            if old_name == new_name:
                # 手动整理时目录可能已经符合标准命名，跳过重命名继续处理内部文件
                folder_result = True
            else:
                folder_result = storage_chain.rename_file(folder_item, new_name)
            logger.info(f"MP原生目录rename_file响应: {folder_result}")
            if not folder_result:
                return {"success": False, "message": f"目录重命名失败: rename_file 返回 {folder_result!r}", "fallback": True, "files": []}

            new_folder_path = self._join_u115_path(str(Path(file_path).parent).replace("\\", "/"), new_name)
            renamed_folder_item = FileItem(
                storage=storage_name,
                fileid=cid,
                type="dir",
                name=new_name,
                path=new_folder_path,
            )

            sub_files = storage_chain.list_files(renamed_folder_item) or []
            video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.rmvb', '.rm', '.iso', '.vob', '.mpg', '.mpeg', '.m4v', '.3gp', '.f4v')
            subtitle_exts = ('.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.smi', '.sami')
            audio_exts = tuple(settings.RMT_AUDIOEXT or [])
            media_exts = tuple((settings.RMT_MEDIAEXT or []) + (settings.RMT_SUBEXT or []) + (settings.RMT_AUDIOEXT or []))
            count = 0
            created_dirs = 0
            file_status_list = []
            needs_agent_files = []
            deleted_cids = []
            season_dir_cache = {}
            logger.info(f"MP原生递归重命名开始: 顶层文件 {len(sub_files)} 个, path={new_folder_path}")

            for sub_file in sub_files:
                if not self._enabled or (self._polling_stop and self._polling_stop.is_set()):
                    logger.info("插件已停止，中断MP原生递归重命名")
                    break
                if cancel_key and self._cancel_flags.get(cancel_key):
                    logger.info(f"任务已取消，中断MP原生递归重命名: info_hash={cancel_key}")
                    break

                sub_name = str(getattr(sub_file, "name", "") or "")
                if getattr(sub_file, "type", "") == "dir":
                    file_status_list.append({"old_name": sub_name, "new_name": sub_name, "status": "跳过（目录）"})
                    continue
                extension = str(getattr(sub_file, "extension", "") or "").lower().lstrip(".")
                if not extension:
                    extension = os.path.splitext(sub_name)[1].lower().lstrip(".")
                ext = f".{extension}" if extension else ""
                if ext in subtitle_exts:
                    file_status_list.append({"old_name": sub_name, "new_name": sub_name, "status": "保留（字幕）"})
                    continue
                if not extension or ext not in media_exts:
                    if str(getattr(sub_file, "fileid", "") or ""):
                        deleted_cids.append(str(getattr(sub_file, "fileid", "") or ""))
                        file_status_list.append({"old_name": sub_name, "new_name": "", "status": "删除（非媒体文件）"})
                    else:
                        file_status_list.append({"old_name": sub_name, "new_name": sub_name, "status": "跳过（非媒体，无CID）"})
                    continue

                sub_path = self._fileitem_path(sub_file, new_folder_path)
                try:
                    context = MediaChain().recognize_by_path(Path(sub_path), obtain_images=False)
                    if not context or not context.media_info:
                        size_bytes = self._get_file_size_bytes(sub_file)
                        is_small_ad, _ = self._is_small_ad_video(sub_file)
                        if ext in video_exts and is_small_ad and str(getattr(sub_file, "fileid", "") or ""):
                            deleted_cids.append(str(getattr(sub_file, "fileid", "") or ""))
                            file_status_list.append({"old_name": sub_name, "new_name": "", "status": f"删除（MP原生未识别，小体积疑似广告，{self._format_size_text(size_bytes)}）"})
                            logger.info(f"MP原生未识别，标记小体积疑似广告删除: {sub_path}, size={self._format_size_text(size_bytes)}")
                        else:
                            needs_agent_files.append({
                                "name": sub_name,
                                "cid": str(getattr(sub_file, "fileid", "") or ""),
                                "size": size_bytes,
                                "size_text": self._format_size_text(size_bytes),
                                "reason": "MP原生识别未命中",
                            })
                            file_status_list.append({"old_name": sub_name, "new_name": "", "status": f"保留（MP原生识别未命中，待智能体识别，{self._format_size_text(size_bytes)}）"})
                            logger.warning(f"MP原生识别未命中，保留待确认: {sub_path}")
                        continue

                    recommend_path = transfer_chain.recommend_name(meta=context.meta_info, mediainfo=context.media_info)
                    if not recommend_path:
                        size_bytes = self._get_file_size_bytes(sub_file)
                        needs_agent_files.append({
                            "name": sub_name,
                            "cid": str(getattr(sub_file, "fileid", "") or ""),
                            "size": size_bytes,
                            "size_text": self._format_size_text(size_bytes),
                            "reason": "MP原生未生成新名称",
                        })
                        file_status_list.append({"old_name": sub_name, "new_name": "", "status": f"保留（MP原生未生成新名称，待智能体识别，{self._format_size_text(size_bytes)}）"})
                        logger.warning(f"MP原生未生成新名称，保留待确认: {sub_path}")
                        continue

                    target_name = Path(recommend_path).name
                    if not target_name or target_name == sub_name:
                        file_status_list.append({"old_name": sub_name, "new_name": target_name or sub_name, "status": "无需重命名"})
                        continue

                    setattr(sub_file, "path", sub_path)
                    rename_result = storage_chain.rename_file(sub_file, target_name)
                    logger.info(f"MP原生文件rename_file响应: {sub_name} -> {target_name}, result={rename_result}")
                    if rename_result:
                        count += 1
                        file_status_list.append({"old_name": sub_name, "new_name": target_name, "status": "已重命名（MP原生）"})
                        sub_name = target_name
                        setattr(sub_file, "name", target_name)
                    else:
                        file_status_list.append({"old_name": sub_name, "new_name": target_name, "status": f"改名失败: rename_file 返回 {rename_result!r}"})
                except Exception as e:
                    file_status_list.append({"old_name": sub_name, "new_name": "", "status": f"改名失败: {e}"})
                    logger.warning(f"MP原生文件重命名异常: {sub_name}, {e}")

            if is_tv:
                refreshed_files = storage_chain.list_files(renamed_folder_item) or []
                top_folders = [item for item in refreshed_files if getattr(item, "type", "") == "dir"]
                for item in refreshed_files:
                    if getattr(item, "type", "") == "dir":
                        continue
                    item_name = str(getattr(item, "name", "") or "")
                    item_ext = os.path.splitext(item_name)[1].lower()
                    if item_ext not in video_exts:
                        continue
                    match = re.search(r'[Ss](\d+)[Ee](\d+)', item_name)
                    if not match:
                        continue
                    season = int(match.group(1))
                    season_dir_name = f"Season {season}"
                    season_folder_cid = season_dir_cache.get(season)
                    if not season_folder_cid:
                        for folder in top_folders:
                            if (getattr(folder, "name", "") or "") == season_dir_name:
                                season_folder_cid = str(getattr(folder, "fileid", "") or "")
                                season_dir_cache[season] = season_folder_cid
                                break
                    if not season_folder_cid:
                        create_result = self._api_create_folder({"cid": cid, "name": season_dir_name, "path": new_folder_path})
                        if create_result.get("success"):
                            season_folder_cid = create_result.get("data", {}).get("cid") or ""
                            created_dirs += 1
                            season_dir_cache[season] = season_folder_cid
                            if not season_folder_cid:
                                try:
                                    refreshed_top = storage_chain.list_files(renamed_folder_item) or []
                                    for folder in refreshed_top:
                                        if getattr(folder, "type", "") == "dir" and (getattr(folder, "name", "") or "") == season_dir_name:
                                            season_folder_cid = str(getattr(folder, "fileid", "") or "")
                                            season_dir_cache[season] = season_folder_cid
                                            break
                                except Exception as e:
                                    logger.warning(f"MP原生后处理查找Season目录异常: {e}")
                    if not season_folder_cid:
                        file_status_list.append({"old_name": item_name, "new_name": item_name, "status": f"移动失败: 创建{season_dir_name}失败"})
                        continue
                    item_cid = str(getattr(item, "fileid", "") or "")
                    if not item_cid:
                        continue
                    move_result = self._api_move_file({
                        "cid": item_cid,
                        "target_cid": season_folder_cid,
                        "path": self._fileitem_path(item, new_folder_path),
                        "name": item_name,
                        "target_path": self._join_u115_path(new_folder_path, season_dir_name),
                        "type": "file",
                    })
                    if move_result.get("success"):
                        file_status_list.append({"old_name": item_name, "new_name": f"{season_dir_name}/{item_name}", "status": "已移动到Season目录"})
                    else:
                        file_status_list.append({"old_name": item_name, "new_name": f"{season_dir_name}/{item_name}", "status": f"移动失败: {move_result.get('message')}"})

            if deleted_cids:
                logger.info(f"MP原生后处理准备删除 {len(deleted_cids)} 个非媒体文件")
                delete_result = self._api_delete_files({"cids": deleted_cids})
                if delete_result.get("success"):
                    logger.info(f"MP原生后处理非媒体文件删除成功: {len(deleted_cids)} 个")
                else:
                    logger.warning(f"MP原生后处理非媒体文件删除失败: {delete_result.get('message')}")

            failure_statuses = [
                f for f in file_status_list
                if any(flag in str(f.get("status") or "") for flag in ("失败", "改名失败"))
            ]
            if failure_statuses:
                message = f"MP原生递归重命名部分失败: {len(failure_statuses)} 个文件失败，成功重命名 {count} 个"
                return {
                    "success": False,
                    "message": message,
                    "fallback": False,
                    "files": file_status_list,
                    "renamed": count,
                    "created_dirs": created_dirs,
                    "needs_agent_files": needs_agent_files,
                }

            message = f"MP原生递归重命名完成: 目录已改名, 文件 {count} 个, 创建Season目录 {created_dirs} 个"
            if needs_agent_files:
                message += f", {len(needs_agent_files)} 个文件待智能体确认"
            return {
                "success": True,
                "message": message,
                "fallback": False,
                "files": file_status_list,
                "renamed": count,
                "created_dirs": created_dirs,
                "needs_agent_files": needs_agent_files,
            }
        except Exception as e:
            logger.warning(f"MP原生递归重命名不可用，准备回落旧逻辑: {e}")
            return {"success": False, "message": f"MP原生递归重命名不可用: {e}", "fallback": True, "files": []}

    def _find_cid_by_path(self, target_path: str) -> Optional[str]:
        """根据115网盘路径逐层查找目录CID

        Args:
            target_path: 115网盘完整路径，如 "/媒体库/电影" 或 "媒体库/电影"

        Returns:
            目标目录的CID，失败返回None
        """
        if not target_path or not self._p115_cookie:
            return None
        # 标准化路径：去掉首尾斜杠，按 / 分割
        path = target_path.strip().strip("/")
        if not path:
            return "0"  # 根目录
        parts = [p for p in path.split("/") if p]
        if not parts:
            return "0"

        try:
            p115_cookie = self._normalize_cookie(self._p115_cookie)
            client = P115Client(p115_cookie)
            current_cid = "0"  # 从根目录开始
            for i, part in enumerate(parts):
                found = False
                offset = 0
                while True:
                    result = client.fs_files({"cid": current_cid, "limit": 1000, "offset": offset})
                    items = result.get("data") if isinstance(result, dict) else None
                    if not items:
                        break
                    for item in items:
                        # 只匹配文件夹
                        if str(item.get("fc", "")) == "0":
                            item_name = str(item.get("n") or item.get("name") or "").strip()
                            item_cid = item.get("cid")
                            if item_cid and item_name == part:
                                current_cid = str(item_cid)
                                found = True
                                logger.info(f"路径查找: {'/'.join(parts[:i+1])} -> CID={current_cid}")
                                break
                    if found:
                        break
                    if len(items) < 1000:
                        break
                    offset += 1000
                if not found:
                    logger.error(f"路径查找失败: 未找到目录 '{part}' (路径: {'/'.join(parts[:i+1])})")
                    return None
            logger.info(f"路径查找成功: {target_path} -> CID={current_cid}")
            return current_cid
        except Exception as e:
            logger.error(f"路径查找异常: {target_path}, {e}")
            return None

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

        if not data.get("_skip_mp_native"):
            native_result = self._api_mp_native_recursive_rename(data)
            if native_result.get("success") or not native_result.get("fallback"):
                return native_result
            logger.warning(f"MP原生递归重命名回落旧逻辑: {native_result.get('message')}")

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

        # 手动整理时目录可能已经是标准名称，直接进入内部文件处理
        if old_name == new_name:
            folder_renamed = True

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
            elif storage_result:
                folder_renamed = True
            else:
                folder_error = f"rename_file 返回 {storage_result!r}"
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
        needs_agent_files = []

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
                    "needs_agent_files": [],
                }

            # 递归收集所有层级的视频文件
            video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.rmvb', '.rm', '.iso', '.vob', '.mpg', '.mpeg', '.m4v', '.3gp', '.f4v')
            subtitle_exts = ('.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.smi', '.sami')
            all_media_files = []
            all_other_files = []
            top_sub_folders = []
            visited_cids = set()

            def _collect_files(fileitem: FileItem, depth: int = 0, parent_path: str = ""):
                """递归收集目录内所有文件（视频文件 + 非视频文件）。"""
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
                            item._parent_path = parent_path
                            if file_ext in video_exts:
                                all_media_files.append(item)
                            else:
                                all_other_files.append(item)
                except Exception as e:
                    logger.error(f"递归收集文件异常(cid={item_cid}): {e}")

            _collect_files(top_fileitem)

            logger.info(f"递归收集完成: 视频文件 {len(all_media_files)} 个, 非视频文件 {len(all_other_files)} 个, 顶层子目录 {len(top_sub_folders)} 个")

            def _size_text(size_bytes: int) -> str:
                return self._format_size_text(size_bytes)

            # 缓存 Season 目录 CID，避免重复创建
            season_dir_cache = {}  # {season_number: cid}
            # 收集需要删除的文件CID（小体积疑似广告、非媒体文件）
            deleted_cids = []
            # 收集无法安全自动判断的视频，交给智能体/人工确认
            needs_agent_files = []
            # 记录已处理的集数，用于检测重复集数
            processed_episodes = set()  # {(season, episode)}

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
                    is_small_ad, size_bytes = self._is_small_ad_video(file_item)
                    if is_small_ad:
                        status = f"删除（小体积疑似广告，{_size_text(size_bytes)}）"
                        file_status_list.append({"old_name": original_name, "new_name": "", "status": status})
                        logger.info(f"标记删除（小体积疑似广告）: {original_name}, size={_size_text(size_bytes)}")
                        deleted_cids.append(file_cid)
                    else:
                        status = f"保留（无剧集信息，待智能体识别，{_size_text(size_bytes)}）"
                        file_status_list.append({"old_name": original_name, "new_name": "", "status": status})
                        needs_agent_files.append({
                            "name": original_name,
                            "cid": file_cid,
                            "size": size_bytes,
                            "size_text": _size_text(size_bytes),
                            "reason": "无剧集信息",
                        })
                        logger.warning(f"保留待智能体识别（无剧集信息）: {original_name}, size={_size_text(size_bytes)}")
                    continue

                # 检测重复集数（BT源可能包含重复文件）
                ep_key = (season, episode)
                if ep_key in processed_episodes:
                    size_bytes = self._get_file_size_bytes(file_item)
                    status = f"保留（重复集数，待智能体识别，{_size_text(size_bytes)}）"
                    file_status_list.append({"old_name": original_name, "new_name": "", "status": status})
                    needs_agent_files.append({
                        "name": original_name,
                        "cid": file_cid,
                        "size": size_bytes,
                        "size_text": _size_text(size_bytes),
                        "reason": f"重复集数 S{season:02d}E{episode:02d}",
                    })
                    logger.warning(f"保留待智能体识别（重复集数）: {original_name} (S{season:02d}E{episode:02d}), size={_size_text(size_bytes)}")
                    continue
                processed_episodes.add(ep_key)

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

            # 删除非媒体文件（保留字幕），收集到 deleted_cids
            for file_item in all_other_files:
                file_cid_del = str(getattr(file_item, "fileid", "") or "")
                file_name_del = getattr(file_item, "name", "") or ""
                file_ext_del = os.path.splitext(file_name_del)[1].lower()
                if file_ext_del in subtitle_exts:
                    file_status_list.append({"old_name": file_name_del, "new_name": file_name_del, "status": "保留（字幕）"})
                    continue
                if file_cid_del:
                    deleted_cids.append(file_cid_del)
                    file_status_list.append({"old_name": file_name_del, "new_name": "", "status": "删除（非媒体文件）"})
                    logger.info(f"标记删除（非媒体文件）: {file_name_del}")

            # 批量删除广告/无用文件
            if deleted_cids:
                logger.info(f"准备删除 {len(deleted_cids)} 个广告/无用文件")
                delete_result = self._api_delete_files({"cids": deleted_cids})
                if delete_result.get("success"):
                    logger.info(f"广告/无用文件删除成功: {len(deleted_cids)} 个")
                else:
                    logger.warning(f"广告/无用文件删除失败: {delete_result.get('message')}")

            if needs_agent_files:
                logger.warning(f"递归重命名保留 {len(needs_agent_files)} 个文件待智能体识别/确认")

        except Exception as e:
            logger.error(f"递归重命名内部文件异常: {e}")
            return {
                "success": True,
                "message": f"目录重命名成功，但内部文件处理异常: {str(e)}",
                "files": file_status_list,
                "renamed": count,
                "created_dirs": created_dirs,
                "needs_agent_files": needs_agent_files,
            }

        failure_statuses = [
            f for f in file_status_list
            if any(flag in str(f.get("status") or "") for flag in ("失败", "移动失败", "改名失败"))
        ]
        if failure_statuses:
            message = f"目录已改名，但 {len(failure_statuses)} 个文件处理失败，成功重命名 {count} 个"
            logger.warning(f"递归重命名部分失败: {message}")
            return {
                "success": False,
                "message": message,
                "files": file_status_list,
                "renamed": count,
                "created_dirs": created_dirs,
                "needs_agent_files": needs_agent_files,
            }

        logger.info(f"递归重命名完成: 目录已改名, 文件 {count} 个, 创建Season目录 {created_dirs} 个")
        return {
            "success": True,
            "message": f"递归重命名完成: 目录已改名, 文件 {count} 个, 创建Season目录 {created_dirs} 个",
            "files": file_status_list,
            "renamed": count,
            "created_dirs": created_dirs,
            "needs_agent_files": needs_agent_files,
        }

    def _trigger_transfer(self, task_name: str, task: dict, info_hash: str = "", notify: bool = True) -> dict:
        """自动整理：离线完成后调用内置rename API重命名文件并清理小体积广告，然后通知智能体移动

        完整流程：
        1. 在115网盘的离线目录中查找下载完成的文件夹
        2. 识别媒体信息，确定规范的文件夹名（含tmdbID）
        3. 调用内置rename API原地重命名文件夹和内部文件（含创建Season子目录），仅删除小体积疑似广告文件
        4. 记录待整理任务，通知智能体处理整体移动
        """
        try:
            # 获取来源会话信息，用于定向通知
            _session = {}
            if info_hash:
                with self._task_lock:
                    _task_record = self._pending_tasks.get(info_hash, {})
                    _session = _task_record.get("session") or {}
            if not self._resolved_path:
                logger.info(f"未解析离线目录路径，跳过自动整理: {task_name}")
                self._record_pending_task(task_name, None, {"error": "未解析离线目录路径"})
                self._update_offline_task_status(info_hash, "待处理：未解析离线目录路径")
                return {"success": False, "message": "未解析离线目录路径，请在插件配置中保存一次目录CID"}

            logger.info(f"开始自动整理: {task_name}")

            # 检查取消标志（不pop，让取消标志保持到整理完成或中断后）
            if self._cancel_flags.get(info_hash):
                logger.info(f"任务已取消，中断整理: {task_name}")
                return {"success": False, "message": "任务已取消"}

            if not self._p115_cookie:
                logger.info(f"未配置115 Cookie，跳过自动整理: {task_name}")
                self._record_pending_task(task_name, None, {"error": "未配置115 Cookie"})
                self._update_offline_task_status(info_hash, "待处理：未配置115 Cookie")
                return {"success": False, "message": "未配置115网盘Cookie"}

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
                return {"success": False, "message": f"离线目录中未找到文件夹: {task_name}"}

            logger.info(f"找到下载文件夹: {task_name}, CID: {folder_cid}")

            # Step 2: 识别媒体信息，确定规范的文件夹名
            from app.chain.media import MediaChain

            media_chain = MediaChain()

            virtual_path = Path(f"/{task_name}")
            context = media_chain.recognize_by_path(virtual_path, obtain_images=False)
            if not (context and context.media_info):
                # 手动离线下载的文件夹名可能不规范，改用目录内第一个视频文件名再识别一次
                try:
                    from app.chain.storage import StorageChain
                    from app.schemas import FileItem

                    probe_item = FileItem(
                        storage="u115",
                        fileid=folder_cid,
                        type="dir",
                        name=task_name,
                        path=self._join_u115_path(self._resolved_path, task_name),
                    )
                    for probe_file in StorageChain().list_files(probe_item) or []:
                        if str(getattr(probe_file, "type", "") or "") == "dir":
                            continue
                        probe_name = str(getattr(probe_file, "name", "") or "")
                        if os.path.splitext(probe_name)[1].lower() not in VIDEO_EXTS:
                            continue
                        probe_context = media_chain.recognize_by_path(
                            Path(f"/{task_name}/{probe_name}"), obtain_images=False
                        )
                        if probe_context and probe_context.media_info:
                            context = probe_context
                            logger.info(f"文件夹名识别失败，已通过视频文件识别: {probe_name}")
                            break
                except Exception as probe_error:
                    logger.warning(f"通过视频文件补充识别失败: {probe_error}")

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
                logger.warning(f"媒体识别失败，停止自动重命名并记录待处理: {task_name}")
                source_path = self._join_u115_path(self._resolved_path, task_name)
                message = "媒体识别失败，未执行自动重命名"
                self._record_pending_task(task_name, source_path, {
                    "rename_result": message,
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "tmdb_id": tmdb_id,
                    "folder_cid": folder_cid,
                    "rename_failed": True,
                })
                self._update_offline_task_status(info_hash, "待处理：媒体识别失败", folder_cid=folder_cid, path=source_path, error=message)
                if notify:
                    self.post_message(
                        title="115重命名待处理",
                        text=(
                            f"**{task_name}** 媒体识别失败，未执行自动重命名。\n"
                            f"CID: `{folder_cid}`\n"
                            f"路径: `{source_path}`\n"
                            "请手动确认媒体信息后再整理。"
                        ),
                        mtype=self._notification_type("Warning"),
                        channel=self._convert_channel(_session.get("channel")),
                        userid=_session.get("userid"),
                    )
                return {"success": False, "message": message}

            # Step 3: 用 StorageChain.rename_file + 115 API 完成重命名（文件夹+文件+Season目录）
            # 不走 transfer/manual，避免屏蔽词干扰
            # 检查取消标志
            if self._cancel_flags.get(info_hash):
                logger.info(f"任务已取消，中断整理（重命名前）: {task_name}")
                return {"success": False, "message": "任务已取消"}
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
            needs_agent_files = rename_result.get("needs_agent_files") or []
            if not rename_result.get("success"):
                if notify:
                    self.post_message(
                        title="115重命名失败",
                        text=(
                            f"**{task_name}** 重命名失败，已停止后续自动整理。\n"
                            f"CID: `{folder_cid}`\n"
                            f"错误: {rename_result.get('message', '未知错误')}"
                        ),
                        mtype=self._notification_type("Warning"),
                        channel=self._convert_channel(_session.get("channel")),
                        userid=_session.get("userid"),
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
                return {"success": False, "message": f"重命名失败: {rename_result.get('message', '未知错误')}"}

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
                "needs_agent_files": needs_agent_files,
            })
            self._update_offline_task_status(info_hash, "重命名完成，待智能体移动", folder_cid=folder_cid, path=final_path)

            # Step 5: 主动调用智能体处理移动
            _title_for_notify = re.sub(r"^\[[^\]]*\]\s*", "", task_name)
            _match = re.match(r"^([^\.\[]+)", _title_for_notify)
            _clean_name = _match.group(1).strip() if _match else _title_for_notify.strip()

            agent_file_text = ""
            if needs_agent_files:
                agent_file_lines = []
                for item in needs_agent_files[:20]:
                    agent_file_lines.append(
                        f"- {item.get('name')} | CID: {item.get('cid')} | 大小: {item.get('size_text') or self._format_size_text(self._safe_int(item.get('size'), 0))} | 原因: {item.get('reason')}"
                    )
                if len(needs_agent_files) > 20:
                    agent_file_lines.append(f"- 还有 {len(needs_agent_files) - 20} 个文件未列出，请查看待整理任务记录")
                agent_file_text = (
                    f"\n\n以下 {len(needs_agent_files)} 个视频未自动删除，需要先识别/确认后再整理：\n"
                    + "\n".join(agent_file_lines)
                    + "\n请不要直接删除这些文件；如能判断是正片，请按正确剧集信息重命名/移动，无法判断时回复用户确认。"
                )

            # 构造发给智能体的消息
            agent_message = (
                f"115离线下载已完成并重命名，请将文件夹移动到入库目录：\n"
                f"标题: {title}\n"
                f"年份: {year}\n"
                f"TMDB ID: {tmdb_id}\n"
                f"媒体类型: {media_type}\n"
                f"文件夹名: {new_folder_name}\n"
                f"文件路径: {final_path}\n"
                f"CID: {folder_cid}\n"
                f"存储: u115\n"
                f"\n请执行以下操作：\n"
                f"1. 根据媒体类型({media_type})和MP目录映射配置，确定目标入库目录的115网盘完整路径\n"
                f"   - 电影(movie)通常移动到电影库目录，如 /媒体库/电影\n"
                f"   - 电视剧(tv)通常移动到电视剧库目录，如 /媒体库/电视剧\n"
                f"   - 请查询MP的目录映射配置确认实际路径\n"
                f"2. 调用 cl_search_move_file 工具执行移动，参数：\n"
                f"   - source_cid: {folder_cid}（源文件夹CID）\n"
                f"   - target_path: 目标目录的115网盘完整路径\n"
                f"   - task_name: {task_name}\n"
                f"{agent_file_text}"
                f"\n注意：文件移动由插件直接执行，无需智能体拥有文件操作权限。"
                f"插件会自动根据目标路径查找CID并完成移动，移动成功后自动清除待整理任务记录。"
                f"\n如果因目录映射配置查询失败或目标路径不存在导致无法移动，请将具体失败原因直接回复给当前消息渠道通知用户。"
            )

            # 主动发送消息给智能体（手动整理时由工具直接返回结果，不再反向通知）
            if notify:
                self._notify_agent(
                    agent_message,
                    channel=self._convert_channel(_session.get("channel")),
                    source="clsearch",
                    userid=_session.get("userid", "clsearch"),
                    username=_session.get("username", "clsearch"),
                )

            # 同时发MP通知记录（手动整理时静默）
            if notify:
                notify_content = f"**离线下载完成: {task_name}**\n"
                if new_folder_name != task_name:
                    notify_content += f"重命名: `{task_name}` -> `{new_folder_name}`\n"
                notify_content += f"文件路径: `{final_path}`\n"
                notify_content += f"CID: `{folder_cid}`\n"
                if media_type:
                    notify_content += f"媒体类型: {media_type}\n"
                if needs_agent_files:
                    notify_content += f"有 {len(needs_agent_files)} 个文件未自动删除，已交给智能体确认\n"
                notify_content += "已通知智能体处理移动"

                self.post_message(
                    title=f"{_clean_name} 重命名完成，待移动",
                    text=notify_content,
                    mtype=NotificationType.Plugin,
                    channel=self._convert_channel(_session.get("channel")),
                    userid=_session.get("userid"),
                )
            # 整理完成，清除取消标志
            self._cancel_flags.pop(info_hash, None)
            # 返回需要智能体处理的信息
            return {
                "success": True,
                "needs_agent": True,
                "folder_cid": folder_cid,
                "new_folder_name": new_folder_name,
                "final_path": final_path,
                "media_type": media_type,
                "title": title,
                "year": year,
                "tmdb_id": tmdb_id,
                "needs_agent_files": needs_agent_files,
                "agent_message": agent_message,
            }

        except Exception as e:
            logger.error(f"自动整理异常: {e}")
            self._record_pending_task(task_name, None, {"error": str(e)})
            self._update_offline_task_status(info_hash, "自动整理异常", error=str(e))
            return {"success": False, "message": f"自动整理异常: {e}"}
