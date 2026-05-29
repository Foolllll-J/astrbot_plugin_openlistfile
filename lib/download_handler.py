import asyncio
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.io import ensure_dir

from .download_service import (
    build_plugin_temp_dir,
    build_temp_file_path,
    decode_text_preview,
    extract_epub_preview_text,
    parse_link_content_length,
)


async def cleanup_temp_file(plugin, file_path: str, delay: int = 10):
    await asyncio.sleep(delay)
    try:
        if Path(file_path).exists():
            Path(file_path).unlink()
    except OSError as e:
        logger.debug(f"清理临时文件失败: {file_path}, err={e}")


async def download_file(plugin, event, file_item: dict, user_config: dict, full_path_override: str = None):
    user_id = event.get_sender_id()
    file_name = file_item.get("name", "")
    file_size = file_item.get("size", 0)
    if not plugin._is_extension_allowed(file_name, user_config):
        yield event.plain_result(
            f"❌ 文件类型不允许下载: {file_name}\n"
            f"💡 当前允许: {plugin._format_extension_filter(user_config)}"
        )
        return
    max_download_size_mb = plugin._get_size_limit_mb(user_config, "max_download_size", 50)
    max_download_size = max_download_size_mb * 1024 * 1024
    if max_download_size_mb > 0 and file_size > max_download_size:
        size_mb = file_size / (1024 * 1024)
        yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用/ol ls 获取下载链接")
        return
    try:
        if full_path_override:
            file_path = full_path_override
        else:
            file_path = plugin._get_item_full_path(user_id, file_item, user_config)

        async with plugin._create_openlist_client(user_config) as client:
            link = await client.get_direct_download_link(file_path)
            if not link:
                yield event.plain_result("❌ 无法获取真实下载链接，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                return
            download_url = link["url"]
            download_headers = plugin._normalize_download_headers(link.get("header", {}))
            link_size = parse_link_content_length(link)
            if not file_size and link_size > 0:
                file_size = link_size
            if max_download_size_mb > 0 and link_size > max_download_size:
                size_mb = link_size / (1024 * 1024)
                yield event.plain_result(f"❌ 文件过大: {size_mb:.1f}MB > {max_download_size_mb}MB\n💡 请使用 /ol ls 获取下载链接")
                return
            downloads_dir = Path(build_plugin_temp_dir(get_astrbot_temp_path(), "downloads"))
            ensure_dir(downloads_dir)
            safe_filename = plugin._sanitize_filename(file_name)
            temp_file_path = build_temp_file_path(str(downloads_dir), user_id, plugin._unique_suffix(), safe_filename)
            yield event.plain_result(f"📥 开始下载: {file_name}\n💾 大小: {plugin._format_file_size(file_size)}")
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=download_headers) as response:
                    if response.status == 200:
                        with open(temp_file_path, "wb") as f:
                            downloaded = 0
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if (
                                    plugin._get_bool_config(user_config, "debug_transfer_logging", False)
                                    and file_size > 10 * 1024 * 1024
                                    and downloaded % (10 * 1024 * 1024) < 8192
                                ):
                                    progress = (downloaded / file_size) * 100
                                    logger.info(
                                        f"下载进度: {file_name} {progress:.1f}% "
                                        f"({plugin._format_file_size(downloaded)}/{plugin._format_file_size(file_size)})"
                                    )
                        yield event.plain_result("✅ 下载完成，正在发送文件...")
                        file_component = File(name=file_name, file=temp_file_path)
                        yield event.chain_result([file_component])
                        asyncio.create_task(cleanup_temp_file(plugin, temp_file_path))
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"用户 {user_id} 下载文件失败 - HTTP状态: {response.status}, 响应: {error_text}, 文件: {file_name}, URL: {download_url}"
                        )
                        yield event.plain_result(f"❌ 下载失败: HTTP {response.status}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
    except Exception as e:
        logger.error(f"用户 {user_id} 下载文件失败: {e}, 文件: {file_name}, 路径: {file_path}", exc_info=True)
        yield event.plain_result(f"❌ 下载失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def get_and_send_download_link(plugin, event, item: dict, user_config: dict, full_path: str = None):
    user_id = event.get_sender_id()
    yield event.plain_result(f"🔗 正在获取文件链接: {item.get('name', '')}...")

    if full_path:
        file_path = full_path
    else:
        file_path = plugin._get_item_full_path(user_id, item, user_config)

    file_name = item.get("name", "")
    if not plugin._is_extension_allowed(file_name, user_config):
        yield event.plain_result(
            f"❌ 文件类型不允许获取链接: {file_name}\n"
            f"💡 当前允许: {plugin._format_extension_filter(user_config)}"
        )
        return

    try:
        async with plugin._create_openlist_client(user_config) as client:
            download_url = await client.get_download_url(file_path)
            if download_url:
                name = item.get("name", "")
                size = item.get("size", 0)
                result_text = f"📥 下载链接\n\n"
                result_text += f"📄 文件: {name}\n"
                result_text += f"💾 大小: {plugin._format_file_size(size)}\n"
                result_text += f"🔗 链接: {download_url}\n\n"
                result_text += "💡 提示: 请复制链接并在浏览器中打开以下载文件。"
                yield event.plain_result(result_text)
            else:
                logger.warning(f"用户 {user_id} 无法获取下载链接 - 路径: {file_path}, 文件名: {item.get('name', '')}")
                yield event.plain_result(f"❌ 无法获取下载链接，文件可能不存在或为目录: {file_path}")
    except Exception as e:
        logger.error(f"用户 {user_id} 获取下载链接失败: {e}, 路径: {file_path}, 文件名: {item.get('name', '')}", exc_info=True)
        yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_file_info(plugin, event, path: str):
    path = (path or "").strip()
    if not path:
        yield event.plain_result("❌ 请提供文件路径或序号")
        return
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
        return
    file_info = None
    path_candidates = []
    if path.isdigit():
        number = int(path)
        item = plugin._get_item_by_number(user_id, number)
        if not item:
            yield event.plain_result(f"❌ 序号 {number} 无效")
            return
        target_path = plugin._get_item_full_path(user_id, item, user_config)
    else:
        path_candidates = plugin._resolve_path_candidates(user_id, path)
        target_path = path_candidates[0]
    try:
        async with plugin._create_openlist_client(user_config) as client:
            if path_candidates:
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info:
                        target_path = candidate_path
                        break
            else:
                file_info = await client.get_file_info(target_path)
            if file_info:
                name = file_info.get("name", "")
                size = file_info.get("size", 0)
                modified = file_info.get("modified", "")
                is_dir = file_info.get("is_dir", False)
                provider = file_info.get("provider", "")
                info_text = "📋 文件信息\n\n"
                info_text += f"📄 名称: {name}\n"
                info_text += f"📁 类型: {'目录' if is_dir else '文件'}\n"
                info_text += f"📍 路径: {target_path}\n"
                if not is_dir:
                    info_text += f"💾 大小: {plugin._format_file_size(size)}\n"
                if modified:
                    info_text += f"📅 修改时间: {modified.replace('T', ' ').split('.')[0]}\n"
                if provider:
                    info_text += f"🔗 存储: {provider}\n"
                if not is_dir:
                    download_url = await client.get_download_url(target_path)
                    if download_url:
                        info_text += f"\n🔗 下载链接:\n{download_url}"
                yield event.plain_result(info_text)
            else:
                display_path = " / ".join(path_candidates)
                logger.warning(f"用户 {user_id} 文件不存在: {display_path}")
                yield event.plain_result(f"❌ 文件不存在: {display_path}")
    except Exception as e:
        logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
        yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_download_command(plugin, event, path: str):
    path = (path or "").strip()
    if not path:
        yield event.plain_result("❌ 请提供文件路径或序号")
        return
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    item_to_download = None
    full_path_override = None

    if path.isdigit():
        number = int(path)
        item = plugin._get_item_by_number(user_id, number)
        if item:
            if item.get("is_dir", False):
                yield event.plain_result(f"❌ 序号 {number} 是目录，无法下载。")
                return
            item_to_download = item
        else:
            yield event.plain_result(f"❌ 序号 {number} 无效。")
            return
    else:
        path_candidates = plugin._resolve_path_candidates(user_id, path)
        try:
            async with plugin._create_openlist_client(user_config) as client:
                for candidate_path in path_candidates:
                    file_info = await client.get_file_info(candidate_path)
                    if file_info and not file_info.get("is_dir", False):
                        item_to_download = file_info
                        full_path_override = candidate_path
                        break
                if not item_to_download:
                    display_path = " / ".join(path_candidates)
                    yield event.plain_result(f"❌ 无法下载，文件不存在或路径为目录: {display_path}")
                    return
        except Exception as e:
            logger.error(f"用户 {user_id} 获取文件信息失败: {e}, 路径候选: {path_candidates}", exc_info=True)
            yield event.plain_result(f"❌ 操作失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")
            return

    if item_to_download:
        async for result in download_file(plugin, event, item_to_download, user_config, full_path_override=full_path_override):
            yield result


async def handle_restore_command(plugin, event, path: str, target: str = None):
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    target_group_id = None
    if target:
        if target.startswith("@"):
            try:
                target_group_id = int(target[1:])
            except ValueError:
                yield event.plain_result(f"❌ 群号格式错误: {target}")
                return
        else:
            yield event.plain_result(f"⚠️ 无法识别目标参数 '{target}'。群号请以 @ 开头。")
            return

    if not target_group_id and event.message_obj.group_id:
        target_group_id = int(event.message_obj.group_id)

    is_group = target_group_id is not None
    target_desc = f"群 {target_group_id}" if is_group else "私聊会话"

    yield event.plain_result(f"🚀 正在启动恢复任务...\n📂 来源路径: {path}\n🎯 目标: {target_desc}")

    try:
        async with plugin._create_openlist_client(user_config) as client:
            files_to_restore = []
            base_path = path.rstrip("/")

            async def collect(current_path):
                res = await client.list_files(current_path, per_page=0)
                if not res:
                    return
                for item in res.get("content", []):
                    full_item_path = f"{current_path.rstrip('/')}/{item['name']}"
                    if item.get("is_dir"):
                        await collect(full_item_path)
                    else:
                        item["full_path"] = full_item_path
                        rel = full_item_path[len(base_path):].lstrip("/")
                        item["relative_path"] = rel
                        files_to_restore.append(item)

            file_info = await client.get_file_info(path)
            if not file_info:
                yield event.plain_result(f"❌ 路径不存在: {path}")
                return

            if file_info.get("is_dir"):
                await collect(base_path)
            else:
                file_info["full_path"] = path
                file_info["relative_path"] = file_info["name"]
                files_to_restore.append(file_info)

            if not files_to_restore:
                yield event.plain_result("📂 路径下没有可恢复的文件。")
                return

            total = len(files_to_restore)
            yield event.plain_result(f"📦 找到 {total} 个文件，开始下载并发送...")

            use_group_upload = is_group and event.platform_meta.name == "aiocqhttp"
            created_folders = {}
            if use_group_upload:
                try:
                    root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                    if root_files and "folders" in root_files:
                        for f in root_files["folders"]:
                            created_folders[f["folder_name"]] = f["folder_id"]
                except Exception as e:
                    logger.warning(f"获取群根目录文件列表失败: {e}")

            success_count = 0
            fail_count = 0
            max_download_size_mb = plugin._get_size_limit_mb(user_config, "max_download_size", 50)
            max_download_size = max_download_size_mb * 1024 * 1024

            downloads_dir = Path(build_plugin_temp_dir(get_astrbot_temp_path(), "downloads"))
            ensure_dir(downloads_dir)

            for i, item in enumerate(files_to_restore, 1):
                file_name = item["name"]
                full_path = item["full_path"]
                rel_path = item["relative_path"]
                try:
                    if not plugin._is_extension_allowed(file_name, user_config):
                        logger.debug(f"跳过恢复文件 {file_name}: 后缀不在允许范围内。")
                        fail_count += 1
                        continue
                    item_size = item.get("size", 0)
                    if max_download_size_mb > 0 and item_size and item_size > max_download_size:
                        logger.debug(f"跳过恢复文件 {file_name}: 大小 {item_size} 超过限制 {max_download_size_mb}MB。")
                        fail_count += 1
                        continue

                    link = await client.get_direct_download_link(full_path)
                    if not link:
                        logger.warning(f"无法获取真实下载链接: {full_path}")
                        fail_count += 1
                        continue
                    download_url = link["url"]
                    download_headers = plugin._normalize_download_headers(link.get("header", {}))
                    link_size = parse_link_content_length(link)
                    if max_download_size_mb > 0 and link_size > max_download_size:
                        logger.debug(f"跳过恢复文件 {file_name}: 下载链接大小 {link_size} 超过限制 {max_download_size_mb}MB。")
                        fail_count += 1
                        continue

                    safe_filename = plugin._sanitize_filename(file_name)
                    temp_file_path = build_temp_file_path(downloads_dir, "restore", plugin._unique_suffix(), safe_filename)
                    async with aiohttp.ClientSession() as session:
                        async with session.get(download_url, headers=download_headers) as response:
                            if response.status == 200:
                                with open(temp_file_path, "wb") as f:
                                    async for chunk in response.content.iter_chunked(8192):
                                        f.write(chunk)
                            else:
                                logger.error(f"下载失败 {file_name}: HTTP {response.status}")
                                fail_count += 1
                                continue

                    if use_group_upload:
                        folder_id = None
                        if "/" in rel_path:
                            folder_name = rel_path.split("/")[0]
                            if folder_name not in created_folders:
                                try:
                                    await event.bot.api.call_action("create_group_file_folder", group_id=target_group_id, folder_name=folder_name)
                                    root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                                    if root_files and "folders" in root_files:
                                        for f in root_files["folders"]:
                                            if f["folder_name"] == folder_name:
                                                created_folders[folder_name] = f["folder_id"]
                                                break
                                except Exception as e:
                                    try:
                                        root_files = await event.bot.api.call_action("get_group_root_files", group_id=target_group_id)
                                        if root_files and "folders" in root_files:
                                            for f in root_files["folders"]:
                                                if f["folder_name"] == folder_name:
                                                    created_folders[folder_name] = f["folder_id"]
                                                    break
                                    except Exception:
                                        logger.error(f"无法获取群文件夹 {folder_name} 的 ID: {e}")
                            folder_id = created_folders.get(folder_name)

                        try:
                            upload_params = {
                                "group_id": target_group_id,
                                "file": str(Path(temp_file_path).resolve()),
                                "name": file_name,
                            }
                            if folder_id is not None:
                                upload_params["folder"] = folder_id
                                upload_params["folder_id"] = folder_id
                            await event.bot.api.call_action("upload_group_file", **upload_params)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"上传群文件 {file_name} 失败: {e}")
                            fail_count += 1
                    else:
                        try:
                            file_comp = File(name=file_name, file=temp_file_path)
                            await event.send(MessageChain([file_comp]))
                            success_count += 1
                            await asyncio.sleep(1)
                        except Exception as e:
                            logger.error(f"发送文件 {file_name} 失败: {e}")
                            fail_count += 1

                    if Path(temp_file_path).exists():
                        Path(temp_file_path).unlink()

                    if i % 5 == 0 or i == total:
                        logger.info(f"📧 恢复进度: {i}/{total} (成功: {success_count}, 失败: {fail_count})")
                except Exception as e:
                    logger.error(f"处理文件 {file_name} 时发生错误: {e}")
                    fail_count += 1
                    if "temp_file_path" in locals() and Path(temp_file_path).exists():
                        Path(temp_file_path).unlink()

            yield event.plain_result(f"✅ 恢复任务完成!\n📊 统计: 总计 {total}, 成功 {success_count}, 失败 {fail_count}\n🎯 目标: {target_desc}")
    except Exception as e:
        logger.error(f"恢复任务失败: {e}", exc_info=True)
        yield event.plain_result(f"❌ 恢复失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息")


async def handle_preview_command(plugin, event, path: str):
    path = (path or "").strip()
    if not path:
        yield event.plain_result("❌ 请提供文件路径或序号")
        return
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)

    max_preview_size_mb = user_config.get("max_preview_size", 0)
    if max_preview_size_mb == -1:
        yield event.plain_result("❌ 预览功能已禁用。")
        return

    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导")
        return

    item = None
    path_or_num = path
    path_candidates = []
    if path_or_num.isdigit():
        number = int(path_or_num)
        item = plugin._get_item_by_number(user_id, number)
        if item:
            if item.get("is_dir"):
                yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                return
            full_path = plugin._get_item_full_path(user_id, item, user_config)
        else:
            yield event.plain_result(f"❌ 序号 {number} 无效")
            return
    else:
        path_candidates = plugin._resolve_path_candidates(user_id, path_or_num)
        full_path = path_candidates[0]

    try:
        async with plugin._create_openlist_client(user_config) as client:
            if not item:
                for candidate_path in path_candidates:
                    item = await client.get_file_info(candidate_path)
                    if item:
                        full_path = candidate_path
                        break
                if not item:
                    display_path = " / ".join(path_candidates)
                    yield event.plain_result(f"❌ 未找到文件: {display_path}")
                    return
                if item.get("is_dir"):
                    yield event.plain_result("❌ 无法预览目录，请指定一个文件。")
                    return

            file_name = item.get("name", "")
            file_size = item.get("size", 0)
            ext = Path(file_name).suffix.lower()

            archive_extensions = [".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz"]
            if ext in archive_extensions:
                yield event.plain_result(f"🔍 正在读取压缩包内容: {file_name}...")
                archive_data = await client.list_archive_contents(full_path)
                if archive_data and "content" in archive_data:
                    contents = archive_data["content"]
                    if not contents:
                        yield event.plain_result(f"📦 压缩包 {file_name} 为空。")
                        return
                    file_list = []
                    for f in contents:
                        prefix = "📂" if f.get("is_dir") else "📄"
                        size_str = f" ({f['size'] / 1024:.1f} KB)" if not f.get("is_dir") else ""
                        file_list.append(f"{prefix} {f['name']}{size_str}")
                    max_display = 20
                    display_list = file_list[:max_display]
                    result_text = f"📦 压缩包预览: {file_name}\n---\n" + "\n".join(display_list)
                    if len(file_list) > max_display:
                        result_text += f"\n\n...(及其他 {len(file_list) - max_display} 个文件)"
                    yield event.plain_result(result_text)
                    return
                else:
                    yield event.plain_result("❌ 无法读取压缩包内容或该格式暂不支持。")
                    return

            if max_preview_size_mb > 0 and file_size > max_preview_size_mb * 1024 * 1024:
                yield event.plain_result(
                    f"❌ 文件过大 ({file_size / (1024*1024):.2f} MB)，超过了最大预览限制 ({max_preview_size_mb} MB)。"
                )
                return

            yield event.plain_result(f"🔍 正在获取预览: {file_name}...")
            link = await client.get_direct_download_link(full_path)
            if not link:
                yield event.plain_result("❌ 获取真实下载链接失败，请确认配置账号为 OpenList 管理员或具有 /api/fs/link 权限")
                return
            download_url = link["url"]
            download_headers = plugin._normalize_download_headers(link.get("header", {}))

            temp_dir = Path(build_plugin_temp_dir(get_astrbot_temp_path(), "preview"))
            ensure_dir(temp_dir)
            safe_filename = plugin._sanitize_filename(file_name)
            temp_file_path = temp_dir / f"preview_{plugin._unique_suffix()}_{safe_filename}"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_url, headers=download_headers) as resp:
                        if resp.status == 200:
                            with open(temp_file_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 1024):
                                    f.write(chunk)
                        else:
                            yield event.plain_result(f"❌ 下载文件失败: HTTP {resp.status}")
                            return

                text_extensions = [
                    ".txt", ".md", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf",
                    ".cfg", ".toml", ".py", ".js", ".java", ".c", ".cpp", ".h", ".go", ".rs",
                    ".php", ".rb", ".sh", ".bash", ".html", ".htm", ".css", ".jsx", ".tsx",
                    ".ts", ".vue", ".sql", ".csv", ".properties", ".env",
                ]
                if ext == ".epub":
                    text_length = user_config.get("text_preview_length", 1000)
                    preview_text = extract_epub_preview_text(str(temp_file_path), text_length)
                    yield event.plain_result(f"📚 EPUB 预览:\n---\n{preview_text}")
                elif ext in text_extensions:
                    text_length = user_config.get("text_preview_length", 1000)
                    try:
                        with open(temp_file_path, "rb") as f:
                            content_bytes = f.read(text_length * 4)
                            preview = decode_text_preview(content_bytes, text_length)
                            logger.debug(f"文本预览编码检测: {preview['encoding']}, 置信度: {preview['confidence']:.2f}")
                            yield event.plain_result(f"📝 文本预览:\n---\n{preview['text']}")
                    except Exception as e:
                        logger.error(f"文本预览失败: {e}")
                        yield event.plain_result(f"❌ 文本解析失败: {e}")
                else:
                    yield event.plain_result(f"❓ 该格式 ({ext}) 不在支持的文本预览列表中。")
            finally:
                if temp_file_path and Path(temp_file_path).exists():
                    Path(temp_file_path).unlink()
    except Exception as e:
        logger.error(f"预览失败: {e}", exc_info=True)
        yield event.plain_result(f"❌ 预览失败: {str(e)}")
