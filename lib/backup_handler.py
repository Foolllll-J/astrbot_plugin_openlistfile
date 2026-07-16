import asyncio
import time
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File

from .helpers import new_lru_mapping, remember_lru_entry


async def _upload_group_file_via_local_file(
    client,
    file_component: File,
    file_name: str,
    target_path: str,
    max_size_mb: int,
) -> tuple[bool, Optional[Path]]:
    get_file_started_at = time.monotonic()
    file_path = await file_component.get_file()
    logger.debug(
        f"📥 [自动备份] 本地获取完成: {file_name}, path={file_path}, "
        f"elapsed={time.monotonic() - get_file_started_at:.2f}s"
    )

    file_path_obj = Path(file_path) if file_path else None
    if not file_path_obj or not file_path_obj.exists():
        logger.error(f"❌ [自动备份] 无法获取文件路径: {file_name}")
        return False, file_path_obj

    actual_size = file_path_obj.stat().st_size
    if max_size_mb > 0 and actual_size > (max_size_mb * 1024 * 1024):
        logger.debug(
            f"⏭️ [自动备份] 文件 {file_name} 实际下载大小 {actual_size} 超过限制 {max_size_mb}MB，跳过。"
        )
        return False, file_path_obj

    success = await client.upload_file(str(file_path_obj), target_path, file_name)
    return success, file_path_obj


async def run_group_file_autobackup(
    plugin,
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
    """后台执行群文件自动备份，避免阻塞同一条消息上的其他处理器。"""
    async with plugin.autobackup_semaphore:
        file_path_obj = None
        try:
            max_size_mb = plugin._get_size_limit_mb(user_config, "backup_max_size", 0)
            if (
                max_size_mb > 0
                and file_size is not None
                and file_size > (max_size_mb * 1024 * 1024)
            ):
                logger.debug(
                    f"⏭️ [自动备份] 文件 {file_name} 事件大小 {file_size} 超过限制 {max_size_mb}MB，跳过。"
                )
                return

            logger.info(f"🚀 [自动备份] 发现新文件: {file_name} -> {target_path}")
            async with plugin._create_openlist_client(user_config) as client:
                if not await client.ensure_dir(target_path):
                    logger.error(f"❌ [自动备份] 创建目标目录失败: {target_path}")
                    return
                if plugin._get_bool_config(user_config, "backup_skip_existing", True):
                    list_result = await client.list_files(target_path, per_page=0)
                    if list_result is not None:
                        for existing in list_result.get("content") or []:
                            if (
                                existing.get("is_dir", False)
                                or existing.get("name") != file_name
                            ):
                                continue
                            try:
                                existing_size = int(existing.get("size", 0))
                                expected_size = (
                                    int(file_size) if file_size is not None else None
                                )
                            except (TypeError, ValueError):
                                expected_size = None
                                existing_size = None
                            if expected_size is None or existing_size == expected_size:
                                logger.debug(
                                    f"⏭️ [自动备份] 跳过已存在文件: {target_path}/{file_name}"
                                )
                                return

                success = False
                use_url_transfer = (
                    event.platform_meta.name == "aiocqhttp"
                    and (file_url or file_id)
                    and file_size is not None
                )
                if use_url_transfer:
                    item = {
                        "file_id": file_id,
                        "file_name": file_name,
                        "file_size": file_size,
                        "busid": busid,
                    }
                    retry_attempts = plugin._get_positive_int_config(
                        user_config, "backup_retry_attempts", 3
                    )
                    retry_delay = plugin._get_positive_int_config(
                        user_config, "backup_retry_delay", 5, minimum=0
                    )
                    if file_id:
                        success, reason = await upload_group_file_with_retry(
                            plugin,
                            event.bot,
                            client,
                            int(group_id),
                            item,
                            target_path,
                            retry_attempts,
                            retry_delay,
                            initial_url=file_url,
                        )
                    else:
                        logger.debug(
                            f"🚀 [自动备份] 使用 URL 流式中转: {file_name}, size={file_size}, target={target_path}"
                        )
                        success = await client.upload_url_stream(
                            file_url, target_path, file_name, file_size
                        )
                        reason = "URL 流式中转上传失败"

                    if not success:
                        logger.warning(
                            f"⚠️ [自动备份] URL 上传失败，回退本地文件上传: {file_name}, reason={reason}"
                        )
                        (
                            success,
                            file_path_obj,
                        ) = await _upload_group_file_via_local_file(
                            client,
                            file_component,
                            file_name,
                            target_path,
                            max_size_mb,
                        )
                else:
                    success, file_path_obj = await _upload_group_file_via_local_file(
                        client,
                        file_component,
                        file_name,
                        target_path,
                        max_size_mb,
                    )

                if success:
                    logger.info(f"✅ [自动备份] 文件 {file_name} 上传成功。")
                    plugin.cache_manager.clear_cache()
                else:
                    logger.error(f"❌ [自动备份] 文件 {file_name} 上传失败。")
        except Exception as e:
            logger.error(f"❌ [自动备份] 处理文件 {file_name} 出错: {e}", exc_info=True)
        finally:
            if file_path_obj and file_path_obj.exists():
                try:
                    file_path_obj.unlink()
                except OSError as e:
                    logger.warning(
                        f"⚠️ [自动备份] 清理临时文件失败: group={group_id}, file={file_name}, err={e}"
                    )


async def handle_group_file_upload(plugin, event: AstrMessageEvent):
    """处理群文件上传事件（自动备份）"""
    raw_event_data = event.message_obj.raw_message
    message_list = (
        raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
    )
    if not isinstance(message_list, list):
        return

    for segment_dict in message_list:
        if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
            data_dict = segment_dict.get("data", {})
            file_name = data_dict.get("file")
            file_id = data_dict.get("file_id")
            file_size = data_dict.get("file_size")
            file_url = data_dict.get("url")

            if not file_name:
                continue

            is_qq_platform = event.platform_meta.name == "aiocqhttp"
            if is_qq_platform and not file_id:
                continue

            if isinstance(file_size, str):
                try:
                    file_size = int(file_size)
                except ValueError:
                    file_size = None

            group_id = str(event.message_obj.group_id)
            if not group_id:
                return

            global_cfg = plugin.get_global_config()
            target_path = plugin._get_autobackup_target_path(global_cfg, group_id)
            if not target_path:
                return

            user_config = global_cfg
            if not plugin._validate_config(user_config):
                logger.warning(
                    f"⚠️ [自动备份] 群 {group_id} 触发了自动备份，但未找到有效的 Openlist 配置。"
                )
                return

            if file_size is not None:
                max_size_mb = plugin._get_size_limit_mb(
                    user_config, "backup_max_size", 0
                )
                if max_size_mb > 0 and file_size > (max_size_mb * 1024 * 1024):
                    logger.debug(
                        f"⏭️ [自动备份] 文件 {file_name} 超过限制 {max_size_mb}MB (事件报告大小 {file_size})，跳过。"
                    )
                    return

            file_component = None
            for msg in event.get_messages():
                if isinstance(msg, File):
                    file_component = msg
                    break
            if not file_component:
                return

            allowed_exts = plugin._get_extension_filter(
                user_config, "backup_allowed_extensions"
            )
            if allowed_exts:
                ext = Path(file_name.lower()).suffix
                if ext not in allowed_exts:
                    logger.debug(
                        f"⏭️ [自动备份] 文件 {file_name} 后缀 {ext} 不在允许范围内，跳过。"
                    )
                    return

            task_user_config = dict(user_config)
            asyncio.create_task(
                run_group_file_autobackup(
                    plugin,
                    event=event,
                    file_component=file_component,
                    file_name=file_name,
                    file_size=file_size,
                    file_url=file_url,
                    target_path=target_path,
                    user_config=task_user_config,
                    group_id=group_id,
                    file_id=file_id,
                    busid=data_dict.get("busid", 0),
                )
            )
            break


async def get_group_files_recursive(
    plugin, bot, group_id: int, folder_id: str = "/", current_path: str = ""
) -> List[Dict]:
    """递归获取群文件列表"""
    all_files = []
    try:
        if folder_id == "/":
            res = await bot.api.call_action("get_group_root_files", group_id=group_id)
        else:
            res = await bot.api.call_action(
                "get_group_files_by_folder", group_id=group_id, folder_id=folder_id
            )

        if not res:
            return []

        files = res.get("files", [])
        folders = res.get("folders", [])

        for f in files:
            f["relative_path"] = f"{current_path}/{f['file_name']}".lstrip("/")
            all_files.append(f)

        for folder in folders:
            sub_folder_id = folder.get("folder_id")
            sub_folder_name = folder.get("folder_name")
            if sub_folder_id:
                sub_files = await get_group_files_recursive(
                    plugin,
                    bot,
                    group_id,
                    sub_folder_id,
                    f"{current_path}/{sub_folder_name}",
                )
                all_files.extend(sub_files)

        return all_files
    except Exception as e:
        logger.error(f"递归获取群 {group_id} 文件失败: {e}", exc_info=True)
        return all_files


async def backup_group_files(
    plugin, event: AstrMessageEvent, group_id: int, target_path: str, user_config: Dict
):
    """执行群文件备份"""
    bot = event.bot
    async for result in do_backup_logic(
        plugin,
        bot,
        event,
        group_id,
        target_path,
        user_config,
        retry_key=plugin._get_backup_retry_key(event),
    ):
        yield result


async def retry_last_backup(plugin, event: AstrMessageEvent, user_config: Dict):
    """重试最近一次手动备份失败项。"""
    if event.platform_meta.name != "aiocqhttp":
        yield event.plain_result(
            "❌ 备份重试仅支持 aiocqhttp 协议端（NapCat/LLOneBot 等）。"
        )
        return
    retry_key = plugin._get_backup_retry_key(event)
    retry_state = plugin._load_backup_retry_state(retry_key)
    if not retry_state or not retry_state.get("items"):
        yield event.plain_result("💡 当前会话没有可重试的备份失败项。")
        return

    group_id = retry_state["group_id"]
    target_path = retry_state["target_path"]
    failed_items = retry_state["items"]
    plugin._delete_backup_retry_state(retry_key)
    yield event.plain_result(
        f"🔁 开始重试上次备份失败的 {len(failed_items)} 个文件\n📂 目标: {target_path}"
    )
    async for result in do_backup_logic(
        plugin,
        event.bot,
        event,
        group_id,
        target_path,
        user_config,
        is_auto=False,
        items_override=failed_items,
        retry_key=retry_key,
        is_retry=True,
    ):
        yield result


async def upload_group_file_with_retry(
    plugin,
    bot,
    client,
    group_id: int,
    item: Dict,
    target_dir: str,
    retry_attempts: int,
    retry_delay: int,
    initial_url: str = None,
) -> tuple:
    """获取群文件 URL 并上传；失败时重新获取 URL 后重试。"""
    file_id = item.get("file_id")
    file_name = item.get("file_name")
    busid = item.get("busid", 0)
    upload_size = item.get("file_size")
    try:
        upload_size = int(upload_size) if upload_size is not None else None
    except (TypeError, ValueError):
        upload_size = None

    attempts = max(1, retry_attempts)
    url_acquire_timeout = 30
    for attempt in range(1, attempts + 1):
        try:
            if attempt == 1 and initial_url:
                download_url = initial_url
            else:
                url_res = await asyncio.wait_for(
                    bot.api.call_action(
                        "get_group_file_url",
                        group_id=group_id,
                        file_id=file_id,
                        busid=busid,
                    ),
                    timeout=url_acquire_timeout,
                )
                download_url = url_res.get("url") if isinstance(url_res, dict) else None
            if not download_url:
                reason = "无法获取群文件下载 URL"
                logger.warning(
                    f"备份文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}"
                )
            else:
                logger.debug(
                    f"🚀 [群备份] 使用 URL 流式中转: {file_name}, "
                    f"size={upload_size}, target={target_dir}, attempt={attempt}/{attempts}"
                )
                is_large = upload_size is not None and upload_size > 50 * 1024 * 1024
                if is_large and not client.transfer_config.get(
                    "debug_transfer_logging"
                ):
                    logger.info(
                        f"📤 [群备份] 大文件上传中: {file_name} ({upload_size / 1024 / 1024:.1f}MB), "
                        f"目标: {target_dir}"
                    )
                if await client.upload_url_stream(
                    download_url, target_dir, file_name, upload_size
                ):
                    return True, ""
                reason = "URL 流式中转上传失败"
                logger.warning(
                    f"备份文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason}"
                )
        except asyncio.TimeoutError:
            reason = "获取群文件URL超时"
            logger.warning(
                f"备份文件 {file_name} 第 {attempt}/{attempts} 次失败: {reason} (超时{url_acquire_timeout}s)"
            )
        except Exception as e:
            reason = str(e)
            logger.error(
                f"备份文件 {file_name} 第 {attempt}/{attempts} 次异常: {e}",
                exc_info=True,
            )

        if attempt < attempts:
            await asyncio.sleep(max(0, retry_delay))

    return False, reason


async def do_backup_logic(
    plugin,
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
    """核心备份逻辑，支持手动和自动备份"""
    if not is_auto and not is_retry:
        yield event.plain_result(f"🔍 正在扫描群 {group_id} 的所有文件，请稍候...")

    if items_override is not None:
        filtered_items = list(items_override)
    else:
        all_items = await get_group_files_recursive(plugin, bot, group_id)
        if not all_items:
            if not is_auto:
                yield event.plain_result("❌ 未找到任何群文件或获取失败。")
            return

        allowed_exts = plugin._get_extension_filter(
            user_config, "backup_allowed_extensions"
        )
        max_size_mb = plugin._get_size_limit_mb(user_config, "backup_max_size", 0)
        max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0

        filtered_items = []
        for item in all_items:
            name = item.get("file_name", "").lower()
            size = item.get("file_size", 0)

            if allowed_exts:
                ext = Path(name).suffix
                if ext not in allowed_exts:
                    continue

            if max_size > 0 and size > max_size:
                continue

            filtered_items.append(item)

    if not filtered_items:
        if not is_auto:
            message = (
                "⚠️ 没有可重试的失败项。"
                if is_retry
                else "⚠️ 扫描完成，但没有符合过滤条件的文件需要备份。"
            )
            yield event.plain_result(message)
        return

    total = len(filtered_items)
    if is_retry:
        logger.info(f"🔁 [群备份] 开始重试 {total} 个失败文件，目标路径: {target_path}")
    elif not is_auto:
        yield event.plain_result(
            f"📦 扫描完成，共发现 {total} 个文件需要备份。\n🚀 开始备份到 Openlist: {target_path}"
        )
    else:
        logger.info(f"🚀 [自动备份] 开始处理 {total} 个新文件，目标路径: {target_path}")

    success_count = 0
    fail_count = 0
    skipped_count = 0
    failed_items = []
    retry_attempts = plugin._get_positive_int_config(
        user_config, "backup_retry_attempts", 3
    )
    retry_delay = plugin._get_positive_int_config(
        user_config, "backup_retry_delay", 5, minimum=0
    )
    skip_existing = plugin._get_bool_config(user_config, "backup_skip_existing", True)
    existing_cache = new_lru_mapping()
    existing_cache_lock = asyncio.Lock()
    existing_cache_max = 20

    async with plugin._create_openlist_client(user_config) as client:
        semaphore = asyncio.Semaphore(3)

        async def get_existing_files(target_dir: str) -> Dict[str, Dict]:
            target_dir = target_dir or "/"
            async with existing_cache_lock:
                if target_dir in existing_cache:
                    remember_lru_entry(
                        existing_cache,
                        target_dir,
                        existing_cache[target_dir],
                        existing_cache_max,
                    )
                    return existing_cache[target_dir]
                list_result = await client.list_files(target_dir, per_page=0)
                files = {}
                if list_result is not None:
                    for existing in list_result.get("content") or []:
                        if not existing.get("is_dir", False):
                            files[existing.get("name", "")] = existing
                remember_lru_entry(
                    existing_cache, target_dir, files, existing_cache_max
                )
                return files

        async def existing_file_matches(
            target_dir: str, file_name: str, file_size
        ) -> bool:
            if not skip_existing:
                return False
            existing_files = await get_existing_files(target_dir)
            existing = existing_files.get(file_name)
            if not existing:
                return False
            try:
                expected_size = int(file_size) if file_size is not None else None
                existing_size = int(existing.get("size", 0))
            except (TypeError, ValueError):
                return True
            return expected_size is None or existing_size == expected_size

        async def remember_existing_file(target_dir: str, file_name: str, file_size):
            if not skip_existing:
                return
            target_dir = target_dir or "/"
            async with existing_cache_lock:
                files = dict(existing_cache.get(target_dir, {}))
                files[file_name] = {
                    "name": file_name,
                    "size": file_size or 0,
                    "is_dir": False,
                }
                remember_lru_entry(
                    existing_cache, target_dir, files, existing_cache_max
                )

        async def upload_task(item, idx):
            nonlocal success_count, fail_count, skipped_count
            async with semaphore:
                file_name = item.get("file_name")
                rel_path = item.get("relative_path")
                file_dir = str(Path(rel_path).parent)
                target_dir = f"{target_path.rstrip('/')}/{file_dir}".rstrip("/")

                try:
                    if not await client.ensure_dir(target_dir or target_path):
                        fail_count += 1
                        failed_items.append(dict(item))
                        return

                    target_dir = target_dir or "/"
                    if await existing_file_matches(
                        target_dir, file_name, item.get("file_size")
                    ):
                        skipped_count += 1
                        logger.debug(
                            f"⏭️ [群备份] 跳过已存在文件: {target_dir}/{file_name}"
                        )
                        return

                    try:
                        fs = int(item.get("file_size", 0))
                        if fs > 50 * 1024 * 1024 and not is_auto:
                            logger.info(
                                f"📤 [群备份] 处理大文件: {file_name} ({fs / 1024 / 1024:.1f}MB)"
                            )
                    except (TypeError, ValueError):
                        pass

                    up_res, reason = await upload_group_file_with_retry(
                        plugin,
                        bot,
                        client,
                        group_id,
                        item,
                        target_dir,
                        retry_attempts,
                        retry_delay,
                    )
                    if up_res:
                        success_count += 1
                        await remember_existing_file(
                            target_dir, file_name, item.get("file_size")
                        )
                    else:
                        fail_count += 1
                        failed_item = dict(item)
                        failed_item["_backup_fail_reason"] = reason
                        failed_items.append(failed_item)
                except Exception as e:
                    logger.error(f"备份文件 {file_name} 失败: {e}")
                    fail_count += 1
                    failed_item = dict(item)
                    failed_item["_backup_fail_reason"] = str(e)
                    failed_items.append(failed_item)

        batch_size = 5
        for i in range(0, total, batch_size):
            batch_tasks = [
                upload_task(item, j)
                for j, item in enumerate(filtered_items[i : i + batch_size], start=i)
            ]
            await asyncio.gather(*batch_tasks)
            logger.info(
                f"📈 备份进度: {min(i + batch_size, total)}/{total} "
                f"(成功: {success_count}, 跳过: {skipped_count}, 失败: {fail_count})"
            )

    if not is_auto:
        if success_count:
            plugin.cache_manager.clear_cache()
        if retry_key:
            if failed_items:
                plugin._save_backup_retry_state(
                    retry_key,
                    {
                        "group_id": group_id,
                        "target_path": target_path,
                        "items": failed_items,
                        "timestamp": time.time(),
                    },
                )
            else:
                plugin._delete_backup_retry_state(retry_key)
        retry_hint = (
            "\n💡 发送 /ol backup retry 可只重试失败项。" if failed_items else ""
        )
        yield event.plain_result(
            f"✅ 备份任务结束!\n"
            f"📊 统计: 总计 {total}, 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}\n"
            f"📂 目标: {target_path}"
            f"{retry_hint}"
        )
    else:
        if success_count:
            plugin.cache_manager.clear_cache()
        logger.info(
            f"✅ [自动备份] 任务结束。群 {group_id}: 成功 {success_count}, 跳过 {skipped_count}, 失败 {fail_count}"
        )


async def handle_backup_command(
    plugin, event: AstrMessageEvent, arg1: str = None, arg2: str = None
):
    """备份群文件到 Openlist"""
    if event.platform_meta.name != "aiocqhttp":
        yield event.plain_result(
            "❌ 备份功能仅支持 aiocqhttp 协议端（NapCat/LLOneBot 等）。"
        )
        return
    user_id = event.get_sender_id()
    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result(
            "❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导"
        )
        return

    arg1 = (arg1 or "").strip()
    arg2 = (arg2 or "").strip()
    if arg1.lower() in ("retry", "重试") or arg2.lower() in ("retry", "重试"):
        async for result in retry_last_backup(plugin, event, user_config):
            yield result
        return

    target_path_arg = None
    target_group_id = 0
    for arg in [arg1, arg2]:
        if not arg:
            continue
        if arg.startswith("/"):
            target_path_arg = arg
        elif arg.startswith("@"):
            try:
                target_group_id = int(arg[1:])
            except ValueError:
                yield event.plain_result(f"❌ 无效的群号格式: {arg}")
                return
        else:
            yield event.plain_result(
                f"⚠️ 无法识别参数 '{arg}'。路径请以 / 开头，群号请以 @ 开头。"
            )
            return

    if not target_group_id:
        if event.message_obj.group_id:
            target_group_id = int(event.message_obj.group_id)
        else:
            yield event.plain_result("❌ 请指定群号（以 @ 开头）或在群聊中使用。")
            return

    target_path = plugin._render_backup_path(
        target_path_arg
        or user_config.get("backup_default_path", "/backup/group_{group_id}"),
        target_group_id,
    )

    async for result in backup_group_files(
        plugin, event, target_group_id, target_path, user_config
    ):
        yield result


async def handle_autobackup_command(
    plugin,
    event: AstrMessageEvent,
    action: str = "show",
    arg1: str = None,
    arg2: str = None,
):
    """配置自动备份"""
    global_cfg = plugin.get_global_config()
    if not plugin._is_event_admin(event):
        logger.warning(
            f"自动备份配置权限不足: user={event.get_sender_id()}, "
            f"group={getattr(event.message_obj, 'group_id', '')}, "
            f"role={plugin._extract_sender_role(event)!r}"
        )
        yield event.plain_result("❌ 权限不足。")
        return

    action = (action or "show").lower()
    if action in ("show", "status", "list", "状态", "列表"):
        effective_groups = global_cfg.get("autobackup_groups", [])
        lines = ["🔄 自动备份配置", ""]
        if effective_groups:
            lines.append("已启用群组:")
            for item in effective_groups:
                if not isinstance(item, str):
                    continue
                if ":" in item:
                    gid, path = item.split(":", 1)
                    path = plugin._render_backup_path(path, gid)
                else:
                    gid = item
                    path = plugin._render_backup_path(
                        global_cfg.get(
                            "backup_default_path", "/backup/group_{group_id}"
                        ),
                        gid,
                    )
                lines.append(f"• 群 {gid} -> {path}")
        else:
            lines.append("当前没有启用自动备份的群组。")
        lines.extend(
            [
                "",
                "用法:",
                "/ol autobackup enable [@群号] [/OpenList路径]",
                "/ol autobackup disable [@群号]",
                "未指定群号时使用当前群；未指定路径时使用备份默认目录。",
            ]
        )
        yield event.plain_result("\n".join(lines))
        return

    target_gid = None
    target_path = None
    for arg in [arg1, arg2]:
        if not arg:
            continue
        if arg.startswith("/"):
            target_path = arg
        elif arg.startswith("@"):
            target_gid = arg[1:]
        else:
            yield event.plain_result(
                f"⚠️ 无法识别参数 '{arg}'。路径请以 / 开头，群号请以 @ 开头。"
            )
            return

    if not target_gid:
        if event.message_obj.group_id:
            target_gid = str(event.message_obj.group_id)
        else:
            yield event.plain_result("❌ 请指定群号（以 @ 开头）或在群聊中使用。")
            return

    local_cfg = plugin.global_config_manager.load_config()
    groups = local_cfg.get("autobackup_groups", [])

    if action == "enable":
        target_path = plugin._render_backup_path(
            target_path
            or global_cfg.get("backup_default_path", "/backup/group_{group_id}"),
            target_gid,
        )

        new_entry = f"{target_gid}:{target_path}"
        new_groups = [
            item
            for item in groups
            if (item.split(":", 1)[0] if ":" in item else item) != target_gid
        ]
        new_groups.append(new_entry)
        local_cfg["autobackup_groups"] = new_groups
        plugin.global_config_manager.save_config(local_cfg)
        yield event.plain_result(
            f"✅ 群 {target_gid} 自动备份已开启 -> {target_path}\n"
            f"📦 正在执行首次全量备份..."
        )
        if event.platform_meta.name == "aiocqhttp":
            backup_config = plugin.get_global_config()
            async for result in do_backup_logic(
                plugin,
                event.bot,
                event,
                int(target_gid),
                target_path,
                backup_config,
                is_auto=False,
                retry_key=plugin._get_backup_retry_key(event),
            ):
                yield result
        else:
            yield event.plain_result(
                "💡 当前平台不支持全量群文件扫描，已仅开启后续自动备份。"
            )

    elif action == "disable":
        new_groups = [
            item
            for item in groups
            if (item.split(":", 1)[0] if ":" in item else item) != target_gid
        ]
        if len(new_groups) < len(groups):
            local_cfg["autobackup_groups"] = new_groups
            plugin.global_config_manager.save_config(local_cfg)
            yield event.plain_result(f"✅ 群 {target_gid} 自动备份已禁用。")
        else:
            yield event.plain_result(f"💡 群 {target_gid} 当前未开启自动备份。")
    else:
        yield event.plain_result("❌ 未知操作。请使用 enable 或 disable。")
