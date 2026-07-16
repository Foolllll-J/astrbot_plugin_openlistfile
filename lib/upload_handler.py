import asyncio
import time
from pathlib import Path
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Video, Plain


async def upload_file(
    plugin, event: AstrMessageEvent, file_component: File, user_config: Dict
):
    user_id = event.get_sender_id()
    upload_state_key = plugin._get_upload_state_key(event)
    upload_state = plugin._get_user_upload_state(upload_state_key)
    target_path = upload_state["target_path"]

    file_name = None
    raw_file_id = None
    raw_file_size = None
    raw_file_url = None
    raw_busid = 0
    component_name = getattr(file_component, "name", None)
    component_url = getattr(file_component, "url", None)
    component_file = getattr(file_component, "file_", None)
    raw_event_data = event.message_obj.raw_message
    message_list = (
        raw_event_data.get("message") if isinstance(raw_event_data, dict) else None
    )
    if isinstance(message_list, list):
        for segment_dict in message_list:
            if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                data_dict = segment_dict.get("data", {})
                file_name = data_dict.get("file")
                raw_file_id = data_dict.get("file_id")
                raw_file_size = data_dict.get("file_size")
                raw_file_url = data_dict.get("url")
                raw_busid = data_dict.get("busid", 0)
                if file_name:
                    break

    file_name = file_name or component_name
    if not file_name:
        yield event.plain_result("出现异常，请稍后尝试上传")
        logger.warning(
            f"用户 {user_id} 上传文件失败：无法从原始消息中解析出有效的文件名。"
        )
        return
    if not plugin._is_extension_allowed(file_name, user_config):
        yield event.plain_result(
            f"❌ 文件类型不允许上传: {file_name}\n"
            f"💡 当前允许: {plugin._format_extension_filter(user_config)}"
        )
        return

    raw_file_size_int = None
    debug_transfer_logging = plugin._get_bool_config(
        user_config, "debug_transfer_logging", False
    )
    if raw_file_size not in (None, ""):
        try:
            raw_file_size_int = int(raw_file_size)
        except (TypeError, ValueError):
            if debug_transfer_logging:
                logger.debug(
                    f"用户 {user_id} 上传文件大小解析失败: name={file_name}, raw_size={raw_file_size}"
                )

    try:
        logger.debug(
            f"用户 {user_id} 准备处理上传文件: name={file_name}, target={target_path}, "
            f"raw_size={raw_file_size}, file_id={raw_file_id}, raw_has_url={bool(raw_file_url)}, "
            f"component_name={component_name}, component_has_url={bool(component_url)}, "
            f"component_file={component_file}"
        )
        max_upload_size_mb = plugin._get_size_limit_mb(
            user_config, "max_upload_size", 100
        )
        max_upload_size = max_upload_size_mb * 1024 * 1024
        if (
            max_upload_size_mb > 0
            and raw_file_size_int is not None
            and raw_file_size_int > max_upload_size
        ):
            size_mb = raw_file_size_int / (1024 * 1024)
            yield event.plain_result(
                f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB"
            )
            return

        upload_url = raw_file_url or component_url
        if upload_url and (raw_file_size_int is not None or max_upload_size_mb == 0):
            yield event.plain_result(
                f"📤 开始上传: {file_name}\n"
                f"💾 大小: {plugin._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}\n"
                f"📂 目标: {target_path}"
            )
            logger.debug(
                f"用户 {user_id} 使用 URL 流式中转上传: name={file_name}, "
                f"size={raw_file_size_int}, target={target_path}, openlist_url={user_config.get('openlist_url')}"
            )

            async def refresh_upload_url():
                group_id = getattr(event.message_obj, "group_id", None)
                if not group_id or not raw_file_id:
                    return None
                url_res = await event.bot.api.call_action(
                    "get_group_file_url",
                    group_id=int(group_id),
                    file_id=raw_file_id,
                    busid=raw_busid or 0,
                )
                return url_res.get("url") if isinstance(url_res, dict) else None

            async with plugin._create_openlist_client(user_config) as client:
                success = await plugin._upload_url_stream_with_retry(
                    client,
                    upload_url,
                    target_path,
                    file_name,
                    raw_file_size_int,
                    user_config,
                    refresh_url=refresh_upload_url,
                )
                if success:
                    upload_state = plugin._get_user_upload_state(upload_state_key)
                    upload_state.setdefault("uploaded_files", []).append(file_name)
                    yield event.plain_result(
                        f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}"
                    )
                    plugin.cache_manager.clear_cache(user_id)
                else:
                    yield event.plain_result(
                        "❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息"
                    )
            return

        if upload_url and raw_file_size_int is None and max_upload_size_mb > 0:
            if debug_transfer_logging:
                logger.debug(
                    f"用户 {user_id} 上传文件缺少有效大小，无法预先执行大小限制，"
                    f"回退到本地临时文件上传: name={file_name}"
                )

        yield event.plain_result(
            f"📥 正在获取文件: {file_name}\n"
            f"💾 大小: {plugin._format_file_size(raw_file_size_int) if raw_file_size_int is not None else '未知'}"
        )
        get_file_started_at = time.monotonic()
        file_path = await file_component.get_file()
        get_file_elapsed = time.monotonic() - get_file_started_at

        file_path_obj = Path(file_path) if file_path else None
        if not file_path_obj or not file_path_obj.exists():
            logger.error(
                f"用户 {user_id} 获取上传文件失败: name={file_name}, returned_path={file_path}, "
                f"elapsed={get_file_elapsed:.2f}s"
            )
            yield event.plain_result("❌ 无法获取文件，请重新发送")
            return

        try:
            file_size = file_path_obj.stat().st_size
            logger.debug(
                f"用户 {user_id} 获取上传文件完成: name={file_name}, local_path={file_path}, "
                f"actual_size={file_size}, elapsed={get_file_elapsed:.2f}s"
            )
            if max_upload_size_mb > 0 and file_size > max_upload_size:
                size_mb = file_size / (1024 * 1024)
                yield event.plain_result(
                    f"❌ 文件过大: {size_mb:.1f}MB > {max_upload_size_mb}MB"
                )
                return

            yield event.plain_result(
                f"📤 开始上传: {file_name}\n"
                f"💾 大小: {plugin._format_file_size(file_size)}\n"
                f"📂 目标: {target_path}"
            )
            logger.debug(
                f"用户 {user_id} 开始调用 OpenList 上传: name={file_name}, local_path={file_path}, "
                f"target={target_path}, openlist_url={user_config.get('openlist_url')}"
            )
            async with plugin._create_openlist_client(user_config) as client:
                success = await plugin._upload_file_with_retry(
                    client, file_path, target_path, file_name, user_config
                )
                if success:
                    upload_state = plugin._get_user_upload_state(upload_state_key)
                    upload_state.setdefault("uploaded_files", []).append(file_name)
                    yield event.plain_result(
                        f"✅ 上传成功!\n📄 文件: {file_name}\n📂 路径: {target_path}"
                    )
                    plugin.cache_manager.clear_cache(user_id)
                else:
                    yield event.plain_result(
                        "❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息"
                    )
        finally:
            if file_path_obj and file_path_obj.exists():
                file_path_obj.unlink()
    except Exception as e:
        logger.error(f"用户 {user_id} 上传文件失败: {e}", exc_info=True)
        yield event.plain_result(
            f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息"
        )
        plugin._set_user_upload_waiting(upload_state_key, False)


async def upload_image(
    plugin, event: AstrMessageEvent, image_component: Image, user_config: Dict
):
    """上传图片到 Openlist"""
    user_id = event.get_sender_id()
    upload_state_key = plugin._get_upload_state_key(event)
    upload_state = plugin._get_user_upload_state(upload_state_key)
    target_path = upload_state["target_path"]
    try:
        image_path = await image_component.convert_to_file_path()
        image_path_obj = Path(image_path) if image_path else None
        if not image_path_obj or not image_path_obj.exists():
            yield event.plain_result("❌ 无法获取图片文件，请重新发送")
            return

        try:
            if image_path_obj.suffix.lower() in (
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".bmp",
            ):
                ext = image_path_obj.suffix
            else:
                ext = ".jpg"
            filename = f"image_{plugin._unique_suffix()}{ext}"
            if not plugin._is_extension_allowed(filename, user_config):
                yield event.plain_result(
                    f"❌ 图片类型不允许上传: {filename}\n"
                    f"💡 当前允许: {plugin._format_extension_filter(user_config)}"
                )
                return
            file_size = image_path_obj.stat().st_size
            max_upload_size_mb = plugin._get_size_limit_mb(
                user_config, "max_upload_size", 100
            )
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if max_upload_size_mb > 0 and file_size > max_upload_size:
                size_mb = file_size / (1024 * 1024)
                yield event.plain_result(
                    f"❌ 图片过大: {size_mb:.1f}MB > {max_upload_size_mb}MB"
                )
                return
            yield event.plain_result(
                f"📤 开始上传图片: {filename}\n"
                f"💾 大小: {plugin._format_file_size(file_size)}\n"
                f"📂 目标: {target_path}"
            )
            async with plugin._create_openlist_client(user_config) as client:
                success = await plugin._upload_file_with_retry(
                    client, image_path, target_path, filename, user_config
                )
                if success:
                    upload_state = plugin._get_user_upload_state(upload_state_key)
                    upload_state.setdefault("uploaded_files", []).append(filename)
                    yield event.plain_result(
                        f"✅ 图片上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}"
                    )
                    plugin.cache_manager.clear_cache(user_id)
                else:
                    yield event.plain_result(
                        "❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息"
                    )
        finally:
            if image_path_obj and image_path_obj.exists():
                image_path_obj.unlink()
    except Exception as e:
        logger.error(f"用户 {user_id} 上传图片失败: {e}", exc_info=True)
        yield event.plain_result(
            f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息"
        )
        plugin._set_user_upload_waiting(upload_state_key, False)


async def upload_video(
    plugin, event: AstrMessageEvent, video_component: Video, user_config: Dict
):
    """上传视频到 Openlist"""
    user_id = event.get_sender_id()
    upload_state_key = plugin._get_upload_state_key(event)
    upload_state = plugin._get_user_upload_state(upload_state_key)
    target_path = upload_state["target_path"]
    try:
        video_path = await video_component.convert_to_file_path()
        video_path_obj = Path(video_path) if video_path else None
        if not video_path_obj or not video_path_obj.exists():
            yield event.plain_result("❌ 无法获取视频文件，请重新发送")
            return

        try:
            ext = video_path_obj.suffix or ".mp4"
            filename = f"video_{plugin._unique_suffix()}{ext}"
            if not plugin._is_extension_allowed(filename, user_config):
                yield event.plain_result(
                    f"❌ 视频类型不允许上传: {filename}\n"
                    f"💡 当前允许: {plugin._format_extension_filter(user_config)}"
                )
                return
            file_size = video_path_obj.stat().st_size
            max_upload_size_mb = plugin._get_size_limit_mb(
                user_config, "max_upload_size", 100
            )
            max_upload_size = max_upload_size_mb * 1024 * 1024
            if max_upload_size_mb > 0 and file_size > max_upload_size:
                size_mb = file_size / (1024 * 1024)
                yield event.plain_result(
                    f"❌ 视频过大: {size_mb:.1f}MB > {max_upload_size_mb}MB"
                )
                return
            yield event.plain_result(
                f"📤 开始上传视频: {filename}\n"
                f"💾 大小: {plugin._format_file_size(file_size)}\n"
                f"📂 目标: {target_path}"
            )
            async with plugin._create_openlist_client(user_config) as client:
                success = await plugin._upload_file_with_retry(
                    client, video_path, target_path, filename, user_config
                )
                if success:
                    upload_state = plugin._get_user_upload_state(upload_state_key)
                    upload_state.setdefault("uploaded_files", []).append(filename)
                    yield event.plain_result(
                        f"✅ 视频上传成功!\n📄 文件: {filename}\n📂 路径: {target_path}"
                    )
                    plugin.cache_manager.clear_cache(user_id)
                else:
                    yield event.plain_result(
                        "❌ 上传失败，请检查网络连接和权限\n💡 提示: 管理员可在后台日志中查看详细错误信息"
                    )
        finally:
            if video_path_obj and video_path_obj.exists():
                video_path_obj.unlink()
    except Exception as e:
        logger.error(f"用户 {user_id} 上传视频失败: {e}", exc_info=True)
        yield event.plain_result(
            f"❌ 上传失败: {str(e)}\n💡 提示: 管理员可在后台日志中查看详细错误信息"
        )
        plugin._set_user_upload_waiting(upload_state_key, False)


async def handle_upload_command(plugin, event: AstrMessageEvent, target: str = ""):
    """上传文件命令"""
    user_id = event.get_sender_id()
    upload_state_key = plugin._get_upload_state_key(event)
    target = (target or "").strip()
    if target.lower() in ("cancel", "取消"):
        upload_state = plugin._get_user_upload_state(upload_state_key)
        if upload_state["waiting"]:
            uploaded = upload_state.get("uploaded_files", [])
            if uploaded:
                summary = f"📊 上传汇总:\n共上传 {len(uploaded)} 个文件\n"
                for f in uploaded:
                    summary += f"• {f}\n"
                yield event.plain_result(summary)
            plugin._set_user_upload_waiting(upload_state_key, False)
            yield event.plain_result("✅ 已取消上传模式")
        else:
            yield event.plain_result("❌ 当前不在上传模式")
        return

    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result(
            "❌ 请先配置Openlist连接信息\n💡 使用 /ol config setup 开始配置向导"
        )
        return

    if target.isdigit():
        number = int(target)
        item = plugin._get_item_by_number(user_id, number)
        if not item:
            yield event.plain_result(
                f"❌ 序号 {number} 无效，请先执行 /ol ls 或使用路径指定上传目录"
            )
            return
        if not item.get("is_dir", False):
            yield event.plain_result(f"❌ 序号 {number} 不是目录，无法作为上传目标")
            return
        target_path = plugin._get_item_full_path(user_id, item, user_config)
    else:
        target_path = plugin._resolve_target_path(user_id, target)

    quoted_components = plugin._get_quoted_upload_components(event.get_messages())
    if quoted_components:
        try:
            async with plugin._create_openlist_client(user_config) as client:
                result = await client.list_files(target_path, per_page=1)
                if result is None:
                    if await client.ensure_dir(target_path):
                        result = await client.list_files(target_path, per_page=1)
                if result is None:
                    yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}")
                    return
        except Exception as e:
            logger.error(f"用户 {user_id} 检查上传目标目录失败: {e}", exc_info=True)
            yield event.plain_result(
                f"❌ 无法访问上传目标目录: {target_path}\n💡 提示: 管理员可在后台日志中查看详细错误信息"
            )
            return

        plugin._set_user_upload_waiting(upload_state_key, False, target_path)
        for component in quoted_components:
            if isinstance(component, Image):
                async for result in upload_image(plugin, event, component, user_config):
                    yield result
            elif isinstance(component, Video):
                async for result in upload_video(plugin, event, component, user_config):
                    yield result
            else:
                async for result in upload_file(plugin, event, component, user_config):
                    yield result
        return

    try:
        async with plugin._create_openlist_client(user_config) as client:
            result = await client.list_files(target_path, per_page=1)
            if result is None:
                if await client.ensure_dir(target_path):
                    result = await client.list_files(target_path, per_page=1)
            if result is None:
                yield event.plain_result(f"❌ 无法访问上传目标目录: {target_path}")
                return
    except Exception as e:
        logger.error(
            f"用户 {user_id} 检查上传目标目录失败: {e}, 路径: {target_path}",
            exc_info=True,
        )
        yield event.plain_result(
            f"❌ 无法访问上传目标目录: {target_path}\n💡 提示: 管理员可在后台日志中查看详细错误信息"
        )
        return

    upload_timeout_minutes = plugin._get_upload_mode_timeout_minutes(user_config)
    plugin._set_user_upload_waiting(upload_state_key, True, target_path)
    upload_text = (
        f"📤 上传模式已启动\n\u200b\n"
        f"📂 目标目录: {target_path}\n\u200b\n"
        f"💡 请直接发送图片、视频或文件，系统会自动上传到此目录\n"
        f"⏰ 上传模式将在{upload_timeout_minutes}分钟后自动取消\n\u200b\n"
        f"• /ol ul 路径 - 切换上传目标目录\n"
        f"• /ol ul cancel - 取消上传模式"
    )
    yield event.plain_result(upload_text)

    async def auto_cancel_upload():
        await asyncio.sleep(upload_timeout_minutes * 60)
        upload_state = plugin._get_user_upload_state(upload_state_key)
        if upload_state["waiting"] and upload_state.get("target_path") == target_path:
            uploaded = upload_state.get("uploaded_files", [])
            if uploaded:
                summary = f"📊 上传汇总:\n共上传 {len(uploaded)} 个文件\n"
                for f in uploaded:
                    summary += f"• {f}\n"
                await event.send(MessageChain([Plain(text=summary)]))
            plugin._set_user_upload_waiting(upload_state_key, False)
            logger.info(
                f"用户 {user_id} 在会话 {upload_state_key} 的上传模式已自动取消（超时{upload_timeout_minutes}分钟）"
            )

    asyncio.create_task(auto_cancel_upload())


async def handle_file_message(plugin, event: AstrMessageEvent):
    """处理文件消息"""
    if not isinstance(event, AstrMessageEvent):
        return

    messages = event.get_messages()
    file_components = [msg for msg in messages if isinstance(msg, (File, Image, Video))]
    if not file_components:
        return

    user_id = event.get_sender_id()
    upload_state_key = plugin._get_upload_state_key(event)
    upload_state = plugin._get_user_upload_state(upload_state_key)
    if not upload_state["waiting"]:
        return

    user_config = plugin.get_user_config(user_id)
    if not plugin._validate_config(user_config):
        yield event.plain_result("❌ 请先配置Openlist连接信息")
        plugin._set_user_upload_waiting(upload_state_key, False)
        return

    for file_component in file_components:
        if isinstance(file_component, Image):
            async for result in upload_image(
                plugin, event, file_component, user_config
            ):
                yield result
        elif isinstance(file_component, Video):
            async for result in upload_video(
                plugin, event, file_component, user_config
            ):
                yield result
        else:
            async for result in upload_file(plugin, event, file_component, user_config):
                yield result
