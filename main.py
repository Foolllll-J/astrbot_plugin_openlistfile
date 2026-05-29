import asyncio
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Context, Star

from .lib import backup_handler, command_handler, download_handler, upload_handler
from .lib.cache import CacheManager
from .lib.config import GlobalConfigManager
from .lib.plugin_runtime_mixin import PluginRuntimeMixin


class OpenlistPlugin(PluginRuntimeMixin, Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.user_config_managers = {}
        self.config = config
        self.global_config_manager = GlobalConfigManager("astrbot_plugin_openlistfile")
        self.global_config = self.global_config_manager.load_config()
        self.cache_manager = CacheManager("astrbot_plugin_openlistfile")
        self.user_navigation_state = {}
        self.user_upload_state = {}
        self.autobackup_semaphore = asyncio.Semaphore(2)

    async def _download_file(
        self,
        event: AstrMessageEvent,
        file_item: Dict,
        user_config: Dict,
        full_path_override: str = None,
    ):
        """下载文件并作为附件发送。"""
        async for result in download_handler.download_file(
            self, event, file_item, user_config, full_path_override
        ):
            yield result

    async def _get_and_send_download_link(
        self,
        event: AstrMessageEvent,
        item: Dict,
        user_config: Dict,
        full_path: str = None,
    ):
        """获取文件下载链接并发送。"""
        async for result in download_handler.get_and_send_download_link(
            self, event, item, user_config, full_path
        ):
            yield result

    async def _run_group_file_autobackup(
        self,
        event: AstrMessageEvent,
        file_component: File,
        file_name: str,
        file_size: Optional[int],
        file_url: str,
        target_path: str,
        user_config: Dict,
        group_id: str,
        file_id: str = None,
        busid: int = 0,
    ) -> None:
        """在后台执行群文件自动备份。"""
        await backup_handler.run_group_file_autobackup(
            self,
            event,
            file_component,
            file_name,
            file_size,
            file_url,
            target_path,
            user_config,
            group_id,
            file_id,
            busid,
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def handle_group_file_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件。"""
        await backup_handler.handle_group_file_upload(self, event)

    async def _upload_file(self, event: AstrMessageEvent, file_component: File, user_config: Dict):
        """上传普通文件到 OpenList。"""
        async for result in upload_handler.upload_file(self, event, file_component, user_config):
            yield result

    async def _get_group_files_recursive(
        self,
        bot,
        group_id: int,
        folder_id: str = "/",
        current_path: str = "",
    ) -> List[Dict]:
        """递归收集群文件列表。"""
        return await backup_handler.get_group_files_recursive(
            self, bot, group_id, folder_id, current_path
        )

    async def _backup_group_files(
        self,
        event: AstrMessageEvent,
        group_id: int,
        target_path: str,
        user_config: Dict,
    ):
        """执行手动群文件备份。"""
        async for result in backup_handler.backup_group_files(
            self, event, group_id, target_path, user_config
        ):
            yield result

    async def _retry_last_backup(self, event: AstrMessageEvent, user_config: Dict):
        """重试最近一次失败的手动备份。"""
        async for result in backup_handler.retry_last_backup(self, event, user_config):
            yield result

    async def _upload_group_file_with_retry(
        self,
        bot,
        client,
        group_id: int,
        item: Dict,
        target_dir: str,
        retry_attempts: int,
        retry_delay: int,
        initial_url: str = None,
    ) -> tuple:
        """带重试地上传群文件。"""
        return await backup_handler.upload_group_file_with_retry(
            self,
            bot,
            client,
            group_id,
            item,
            target_dir,
            retry_attempts,
            retry_delay,
            initial_url,
        )

    async def _do_backup_logic(
        self,
        bot,
        event: AstrMessageEvent,
        group_id: int,
        target_path: str,
        user_config: Dict,
        is_auto: bool = False,
        items_override: Optional[List[Dict]] = None,
        retry_key: str = None,
        is_retry: bool = False,
    ):
        """执行群文件备份的主流程。"""
        async for result in backup_handler.do_backup_logic(
            self,
            bot,
            event,
            group_id,
            target_path,
            user_config,
            is_auto,
            items_override,
            retry_key,
            is_retry,
        ):
            yield result

    async def _upload_image(self, event: AstrMessageEvent, image_component: Image, user_config: Dict):
        """上传图片到 OpenList。"""
        async for result in upload_handler.upload_image(self, event, image_component, user_config):
            yield result

    async def _upload_video(self, event: AstrMessageEvent, video_component: Video, user_config: Dict):
        """上传视频到 OpenList。"""
        async for result in upload_handler.upload_video(self, event, video_component, user_config):
            yield result

    @filter.command_group("ol", alias=["网盘"])
    def openlist_group(self):
        """OpenList 文件管理命令组。"""
        pass

    @openlist_group.command("config", alias=["配置"])
    async def config_command(
        self,
        event: AstrMessageEvent,
        action: str = "show",
        key: str = "",
        value: str = "",
    ):
        """配置 OpenList 连接和插件参数。"""
        async for result in command_handler.handle_config_command(self, event, action, key, value):
            yield result

    @openlist_group.command("ls", alias=["列表", "直链"])
    async def list_files(self, event: AstrMessageEvent, path: str = ""):
        """列出目录内容，或为文件返回下载链接。"""
        async for result in command_handler.handle_list_files(self, event, path):
            yield result

    @openlist_group.command("next", alias=["下一页"])
    async def next_page(self, event: AstrMessageEvent):
        """查看下一页。"""
        async for result in command_handler.handle_next_page(self, event):
            yield result

    @openlist_group.command("prev", alias=["上一页"])
    async def prev_page(self, event: AstrMessageEvent):
        """查看上一页。"""
        async for result in command_handler.handle_prev_page(self, event):
            yield result

    @openlist_group.command("search", alias=["搜索"])
    async def search_files(self, event: AstrMessageEvent, keyword: str, path: str = "/"):
        """搜索文件。"""
        async for result in command_handler.handle_search_files(self, event, keyword, path):
            yield result

    @openlist_group.command("info", alias=["信息"])
    async def file_info(self, event: AstrMessageEvent, path: str):
        """查看文件或目录详情。"""
        async for result in download_handler.handle_file_info(self, event, path):
            yield result

    @openlist_group.command("dl", alias=["下载", "download"])
    async def get_download_link(self, event: AstrMessageEvent, path: str):
        """直接下载指定文件。"""
        async for result in download_handler.handle_download_command(self, event, path):
            yield result

    @openlist_group.command("quit", alias=["上一层", "返回"])
    async def quit_navigation(self, event: AstrMessageEvent):
        """返回上级目录。"""
        async for result in command_handler.handle_quit_navigation(self, event):
            yield result

    @openlist_group.command("ul", alias=["上传", "upload"])
    async def upload_command(self, event: AstrMessageEvent, target: str = ""):
        """开启上传模式或上传引用文件。"""
        async for result in upload_handler.handle_upload_command(self, event, target):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_file_message(self, event: AstrMessageEvent):
        """处理上传模式下的文件、图片和视频消息。"""
        async for result in upload_handler.handle_file_message(self, event):
            yield result

    @openlist_group.command("backup", alias=["备份"])
    async def backup_command(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """备份群文件到 OpenList。"""
        async for result in backup_handler.handle_backup_command(self, event, arg1, arg2):
            yield result

    @openlist_group.command("autobackup", alias=["自动备份"])
    async def autobackup_command(
        self,
        event: AstrMessageEvent,
        action: str = "show",
        arg1: str = None,
        arg2: str = None,
    ):
        """管理群文件自动备份。"""
        async for result in backup_handler.handle_autobackup_command(
            self, event, action, arg1, arg2
        ):
            yield result

    @openlist_group.command("restore", alias=["恢复"])
    async def restore_command(self, event: AstrMessageEvent, path: str, target: str = None):
        """把 OpenList 中的文件恢复到会话中。"""
        async for result in download_handler.handle_restore_command(self, event, path, target):
            yield result

    @openlist_group.command("preview", alias=["预览"])
    async def preview_command(self, event: AstrMessageEvent, path: str):
        """预览文本文件或压缩包目录。"""
        async for result in download_handler.handle_preview_command(self, event, path):
            yield result

    @openlist_group.command("rm", alias=["删除"])
    async def remove_command(self, event: AstrMessageEvent, path: str):
        """删除文件或目录。"""
        async for result in command_handler.handle_remove_command(self, event, path):
            yield result

    @openlist_group.command("mkdir", alias=["新建"])
    async def mkdir_command(self, event: AstrMessageEvent, name: str):
        """创建目录。"""
        async for result in command_handler.handle_mkdir_command(self, event, name):
            yield result

    @openlist_group.command("help", alias=["帮助"])
    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息。"""
        async for result in command_handler.handle_help_command(self, event):
            yield result

    async def terminate(self):
        """插件卸载时的清理钩子。"""
        self.user_upload_state.clear()
        self.user_navigation_state.clear()
        self.user_config_managers.clear()
        logger.info("OpenList 助手已卸载")
