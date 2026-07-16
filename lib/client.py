import asyncio
import posixpath
from pathlib import Path
import socket
import time
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import quote, urlparse
from astrbot.api import logger


DEFAULT_TRANSFER_CONFIG = {
    "upload_chunk_size": 4 * 1024 * 1024,
    "upload_progress_step": 64 * 1024 * 1024,
    "upstream_connect_timeout": 60,
    "upstream_read_timeout": 180,
    "openlist_connect_timeout": 30,
    "openlist_upload_response_timeout": 3000,
    "debug_transfer_logging": False,
}


class ProgressFilePayload(aiohttp.Payload):
    """带进度日志的文件上传载荷，保留明确的 Content-Length。"""

    def __init__(
        self,
        file_path: str,
        filename: str,
        chunk_size: int,
        progress_step: int,
        debug_logging: bool = False,
    ):
        super().__init__(None, content_type="application/octet-stream")
        self.file_path = Path(file_path)
        self._filename = filename
        self.file_size = self.file_path.stat().st_size
        self.chunk_size = chunk_size
        self.progress_step = progress_step
        self.debug_logging = debug_logging
        self._transport_logged = False

    @property
    def size(self):
        return self.file_size

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return f"<streaming file payload filename={self._filename!r} size={self.file_size}>"

    async def write(self, writer):
        uploaded = 0
        last_logged = 0
        transport = getattr(writer, "transport", None)
        started_at = time.monotonic()
        if self.debug_logging and transport and not self._transport_logged:
            local_addr = transport.get_extra_info("sockname")
            peer_addr = transport.get_extra_info("peername")
            ssl_object = transport.get_extra_info("ssl_object")
            logger.debug(
                f"OpenList 上传连接: local={local_addr}, peer={peer_addr}, ssl={bool(ssl_object)}"
            )
            self._transport_logged = True
        with open(self.file_path, "rb") as f:
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                uploaded += len(chunk)
                await writer.write(chunk)
                if self.debug_logging and (
                    uploaded == len(chunk)
                    or uploaded - last_logged >= self.progress_step
                    or uploaded == self.file_size
                ):
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    speed = uploaded / 1024 / 1024 / elapsed
                    buffer_size = (
                        transport.get_write_buffer_size() if transport else None
                    )
                    logger.debug(
                        f"上传进度: {self._filename} {uploaded}/{self.file_size} bytes 已写入连接 "
                        f"speed={speed:.2f}MB/s write_buffer={buffer_size}"
                    )
                    last_logged = uploaded
        elapsed = max(time.monotonic() - started_at, 0.001)
        if self.debug_logging:
            logger.debug(
                f"上传请求体写入完成: {self._filename} {uploaded}/{self.file_size} bytes "
                f"elapsed={elapsed:.2f}s avg_speed={uploaded / 1024 / 1024 / elapsed:.2f}MB/s"
            )


class ProgressStreamPayload(aiohttp.Payload):
    """从上游流读取并写入 OpenList；数据经过 AstrBot，不由 OpenList 直拉 URL。"""

    def __init__(
        self,
        stream,
        filename: str,
        file_size: Optional[int] = None,
        progress_step: int = 64 * 1024 * 1024,
        debug_logging: bool = False,
    ):
        super().__init__(None, content_type="application/octet-stream")
        self.stream = stream
        self._filename = filename
        self.file_size = file_size
        self.progress_step = progress_step
        self.debug_logging = debug_logging
        self._transport_logged = False

    @property
    def size(self):
        return self.file_size

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        total = self.file_size if self.file_size is not None else "unknown"
        return f"<streaming url payload filename={self._filename!r} size={total}>"

    async def write(self, writer):
        uploaded = 0
        last_logged = 0
        transport = getattr(writer, "transport", None)
        started_at = time.monotonic()
        if self.debug_logging and transport and not self._transport_logged:
            local_addr = transport.get_extra_info("sockname")
            peer_addr = transport.get_extra_info("peername")
            ssl_object = transport.get_extra_info("ssl_object")
            logger.debug(
                f"OpenList 上传连接: local={local_addr}, peer={peer_addr}, ssl={bool(ssl_object)}"
            )
            self._transport_logged = True

        async for chunk in self.stream:
            if not chunk:
                continue
            uploaded += len(chunk)
            await writer.write(chunk)
            if self.debug_logging and (
                uploaded == len(chunk)
                or uploaded - last_logged >= self.progress_step
                or (self.file_size is not None and uploaded == self.file_size)
            ):
                elapsed = max(time.monotonic() - started_at, 0.001)
                speed = uploaded / 1024 / 1024 / elapsed
                buffer_size = transport.get_write_buffer_size() if transport else None
                total = self.file_size if self.file_size is not None else "unknown"
                logger.debug(
                    f"上传进度: {self._filename} {uploaded}/{total} bytes 已写入连接 "
                    f"speed={speed:.2f}MB/s write_buffer={buffer_size}"
                )
                last_logged = uploaded

        elapsed = max(time.monotonic() - started_at, 0.001)
        total = self.file_size if self.file_size is not None else "unknown"
        if self.debug_logging:
            logger.debug(
                f"上传请求体写入完成: {self._filename} {uploaded}/{total} bytes "
                f"elapsed={elapsed:.2f}s avg_speed={uploaded / 1024 / 1024 / elapsed:.2f}MB/s"
            )


class OpenlistClient:
    """Openlist API 客户端"""

    def __init__(
        self,
        base_url: str,
        public_base_url: str = "",
        username: str = "",
        password: str = "",
        fixed_base_directory: str = "",
        transfer_config: Optional[Dict] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else ""
        self.username = username
        self.password = password
        self.token = ""
        self.fixed_base_directory = fixed_base_directory
        self.transfer_config = self._build_transfer_config(transfer_config)
        self.session = None

    def _build_transfer_config(self, transfer_config: Optional[Dict]) -> Dict:
        config = DEFAULT_TRANSFER_CONFIG.copy()
        if isinstance(transfer_config, dict):
            config.update({k: v for k, v in transfer_config.items() if v is not None})
            if (
                "debug_transfer_logging" not in transfer_config
                and "debug_upload_logging" in transfer_config
            ):
                config["debug_transfer_logging"] = transfer_config[
                    "debug_upload_logging"
                ]

        for key in [
            "upload_chunk_size",
            "upload_progress_step",
            "upstream_connect_timeout",
            "upstream_read_timeout",
            "openlist_connect_timeout",
            "openlist_upload_response_timeout",
        ]:
            try:
                value = int(config.get(key, DEFAULT_TRANSFER_CONFIG[key]))
            except (TypeError, ValueError):
                value = DEFAULT_TRANSFER_CONFIG[key]
            config[key] = max(1, value)

        value = config.get(
            "debug_transfer_logging", DEFAULT_TRANSFER_CONFIG["debug_transfer_logging"]
        )
        if isinstance(value, str):
            config["debug_transfer_logging"] = value.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            )
        else:
            config["debug_transfer_logging"] = bool(value)
        return config

    def _normalize_path(self, path: str) -> str:
        """Normalize an OpenList path without applying user permissions/base path."""
        normalized = (path or "").strip().replace("\\", "/")
        if not normalized:
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = posixpath.normpath(normalized)
        if normalized in ("", "."):
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        return normalized

    def _with_fixed_base_directory(self, path: str) -> str:
        """Return the full raw path required by /d, /p and /api/fs/link routes."""
        path = self._normalize_path(path)
        if not self.fixed_base_directory:
            return path

        base_dir = self._normalize_path(self.fixed_base_directory)
        if base_dir == "/":
            return path
        if path == base_dir or path.startswith(base_dir + "/"):
            return path
        return self._normalize_path(f"{base_dir.rstrip('/')}/{path.lstrip('/')}")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        if not self.token and self.username and self.password:
            await self.login()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def login(self) -> bool:
        """登录获取token"""
        try:
            login_data = {"username": self.username, "password": self.password}

            async with self.session.post(
                f"{self.base_url}/api/auth/login", json=login_data
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        self.token = result.get("data", {}).get("token", "")
                        return True
                    else:
                        logger.error(
                            f"OpenList登录失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 用户名: {self.username}"
                        )
                        return False
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"OpenList登录失败 - HTTP状态: {resp.status}, 响应: {error_text}, 用户名: {self.username}"
                    )
                    return False
        except Exception as e:
            logger.error(
                f"OpenList登录失败: {e}, 用户名: {self.username}, 服务器: {self.base_url}",
                exc_info=True,
            )
            return False

    async def list_files(
        self, path: str = "/", page: int = 1, per_page: int = 30
    ) -> Optional[Dict]:
        """获取文件列表"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            list_data = {
                "path": path,
                "password": "",
                "page": page,
                "per_page": per_page,
                "refresh": False,
            }

            async with self.session.post(
                f"{self.base_url}/api/fs/list", json=list_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    else:
                        logger.error(
                            f"获取文件列表失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}"
                        )
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"获取文件列表失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}"
                    )
                    return None
        except Exception as e:
            logger.error(f"获取文件列表失败: {e}, 路径: {path}", exc_info=True)
            return None

    async def get_file_info(self, path: str) -> Optional[Dict]:
        """获取文件信息"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            get_data = {"path": path, "password": ""}

            async with self.session.post(
                f"{self.base_url}/api/fs/get", json=get_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    else:
                        logger.error(
                            f"获取文件信息失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}"
                        )
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"获取文件信息失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}"
                    )
                    return None
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}, 路径: {path}", exc_info=True)
            return None

    async def search_files(
        self, keyword: str, path: str = "/", per_page: int = 1000
    ) -> Optional[List[Dict]]:
        """在指定路径下搜索文件"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            search_data = {
                "parent": path,
                "keywords": keyword,
                "scope": 0,  # 0: 当前目录及子目录
                "page": 1,
                "per_page": per_page,
            }

            async with self.session.post(
                f"{self.base_url}/api/fs/search", json=search_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        content = result.get("data", {}).get("content")
                        return content if content is not None else []
                    else:
                        logger.error(
                            f"搜索文件失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 关键词: {keyword}, 路径: {path}"
                        )
                        return []
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"搜索文件失败 - HTTP状态: {resp.status}, 响应: {error_text}, 关键词: {keyword}, 路径: {path}"
                    )
                    return []
        except Exception as e:
            logger.error(
                f"搜索文件失败: {e}, 关键词: {keyword}, 路径: {path}", exc_info=True
            )
            return []

    async def get_download_url(
        self, path: str, prefer_public: bool = True
    ) -> Optional[str]:
        """获取文件下载链接"""
        file_info = await self.get_file_info(path)

        if file_info and not file_info.get("is_dir", True):
            raw_url = file_info.get("raw_url")
            if raw_url:
                if (
                    prefer_public
                    and self.public_base_url
                    and raw_url.startswith(self.base_url)
                ):
                    return self.public_base_url + raw_url[len(self.base_url) :]
                return raw_url

            sign = file_info.get("sign")
            base_url_to_use = (
                self.public_base_url
                if prefer_public and self.public_base_url
                else self.base_url
            )

            full_path = self._with_fixed_base_directory(path)
            encoded_url_path = quote(full_path.encode("utf-8"))

            if not sign:
                logger.warning(
                    f"无法为 {path} 获取签名，可能需要开启 '全部签名' 选项。返回无签名链接。"
                )
                return f"{base_url_to_use}/d{encoded_url_path}"

            return f"{base_url_to_use}/d{encoded_url_path}?sign={sign}"

        return None

    async def get_direct_download_link(self, path: str) -> Optional[Dict]:
        """通过认证 API 获取真实下载链接，供机器人后台下载使用。"""
        raw_path = self._with_fixed_base_directory(path)

        def is_admin_only_reason(reason: str) -> bool:
            reason_lower = (reason or "").strip().lower()
            return (
                "not an admin" in reason_lower or "you are not an admin" in reason_lower
            )

        async def fallback_to_raw_url(reason: str) -> Optional[Dict]:
            file_info = await self.get_file_info(path)
            if (
                file_info
                and not file_info.get("is_dir", True)
                and file_info.get("raw_url")
            ):
                if is_admin_only_reason(reason):
                    logger.debug(
                        f"获取真实下载链接失败，已改用 raw_url 兜底: {reason}, "
                        f"路径: {path}, raw_path: {raw_path}"
                    )
                else:
                    logger.warning(
                        f"获取真实下载链接失败，尝试 raw_url 兜底: {reason}, "
                        f"路径: {path}, raw_path: {raw_path}"
                    )
                return {"url": file_info["raw_url"], "header": {}}
            logger.error(
                f"获取真实下载链接失败，raw_url 兜底也失败: {reason}, "
                f"路径: {path}, raw_path: {raw_path}"
            )
            return None

        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            link_data = {"path": raw_path}
            async with self.session.post(
                f"{self.base_url}/api/fs/link", json=link_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        data = result.get("data") or {}
                        if data.get("url"):
                            return data
                        return await fallback_to_raw_url("响应缺少 url")
                    reason = (
                        f"code={result.get('code')}, "
                        f"message={result.get('message', '未知错误')}"
                    )
                    return await fallback_to_raw_url(reason)

                error_text = await resp.text()
                return await fallback_to_raw_url(
                    f"HTTP {resp.status}, 响应: {error_text}"
                )
        except Exception as e:
            logger.error(
                f"获取真实下载链接失败: {e}, 路径: {path}, raw_path: {raw_path}",
                exc_info=True,
            )
            return await fallback_to_raw_url(str(e))

    async def upload_file(
        self, file_path: str, target_path: str, filename: str = None
    ) -> bool:
        """上传文件到Openlist"""
        try:
            local_path = Path(file_path)
            if not local_path.exists():
                logger.error(f"文件不存在: {file_path}")
                return False

            if filename is None:
                filename = local_path.name

            file_size = local_path.stat().st_size
            logger.debug(
                f"开始流式上传文件: {filename}, 大小: {file_size} bytes, 目标: {target_path}"
            )
            payload = ProgressFilePayload(
                file_path,
                filename,
                self.transfer_config["upload_chunk_size"],
                self.transfer_config["upload_progress_step"],
                self.transfer_config["debug_transfer_logging"],
            )
            return await self._put_payload(
                payload, target_path, filename, f"local_file={file_path}"
            )

        except Exception as e:
            logger.error(
                f"上传文件失败: {e}, 文件路径: {file_path}, 目标路径: {target_path}/{filename}",
                exc_info=True,
            )
            return False

    async def upload_url_stream(
        self,
        source_url: str,
        target_path: str,
        filename: str,
        file_size: Optional[int] = None,
    ) -> bool:
        """从 URL 读取文件并流式 PUT 到 OpenList，OpenList 不会收到源 URL。"""
        parsed_source = urlparse(source_url)
        source_host = parsed_source.hostname or "unknown"
        source_port = parsed_source.port or (
            443 if parsed_source.scheme == "https" else 80
        )

        try:
            if (
                self.transfer_config["debug_transfer_logging"]
                and source_host != "unknown"
            ):
                try:
                    addrinfo = await asyncio.get_running_loop().getaddrinfo(
                        source_host, source_port, type=socket.SOCK_STREAM
                    )
                    resolved = sorted(
                        {f"{item[4][0]}:{item[4][1]}" for item in addrinfo}
                    )
                    logger.debug(
                        f"上游文件目标解析: {source_host}:{source_port} -> {', '.join(resolved)}"
                    )
                except Exception as e:
                    logger.debug(
                        f"解析上游文件目标失败: {source_host}:{source_port}, err={e}"
                    )

            timeout = aiohttp.ClientTimeout(
                total=None,
                sock_connect=self.transfer_config["upstream_connect_timeout"],
                sock_read=self.transfer_config["upstream_read_timeout"],
            )
            async with self.session.get(source_url, timeout=timeout) as source_response:
                content_length = source_response.headers.get("Content-Length")
                content_type = source_response.headers.get("Content-Type")
                if self.transfer_config["debug_transfer_logging"]:
                    logger.debug(
                        f"上游文件响应: HTTP {source_response.status}, source={source_host}, "
                        f"content_length={content_length}, content_type={content_type}"
                    )
                if source_response.status != 200:
                    error_text = await source_response.text()
                    logger.error(
                        f"上游文件获取失败: HTTP {source_response.status}, source={source_host}, "
                        f"响应内容: {error_text[:500]}"
                    )
                    return False

                if content_length:
                    try:
                        upstream_size = int(content_length)
                        if file_size is None or (file_size == 0 and upstream_size > 0):
                            file_size = upstream_size
                        elif file_size != upstream_size:
                            logger.warning(
                                f"上游文件大小与事件大小不一致: {filename}, "
                                f"event_size={file_size}, upstream_content_length={upstream_size}"
                            )
                    except ValueError:
                        logger.warning(f"上游 Content-Length 无效: {content_length}")

                payload = ProgressStreamPayload(
                    source_response.content.iter_chunked(
                        self.transfer_config["upload_chunk_size"]
                    ),
                    filename,
                    file_size,
                    self.transfer_config["upload_progress_step"],
                    self.transfer_config["debug_transfer_logging"],
                )
                return await self._put_payload(
                    payload,
                    target_path,
                    filename,
                    f"url_stream_source={source_host}, upstream_status={source_response.status}",
                )

        except Exception as e:
            logger.error(
                f"URL 流式中转上传失败: {e}, 文件: {filename}, source={source_host}, "
                f"目标路径: {target_path}/{filename}",
                exc_info=True,
            )
            return False

    async def _put_payload(
        self,
        payload: aiohttp.Payload,
        target_path: str,
        filename: str,
        source_desc: str = "",
    ) -> bool:
        """将任意 aiohttp payload PUT 到 OpenList。"""
        upload_url = f"{self.base_url}/api/fs/put"
        encoded_file_path = quote(f"{target_path.rstrip('/')}/{filename}", safe="/")
        headers = {"File-Path": encoded_file_path}

        if hasattr(self, "token") and self.token:
            headers["Authorization"] = self.token

        if self.transfer_config["debug_transfer_logging"]:
            logger.debug(
                f"OpenList 上传参数: url={upload_url}, source={source_desc}, "
                f"content_length={payload.size}, file_path_header={encoded_file_path}, "
                f"auth={'yes' if headers.get('Authorization') else 'no'}"
            )

        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self.transfer_config["openlist_connect_timeout"],
            sock_read=self.transfer_config["openlist_upload_response_timeout"],
        )
        if self.transfer_config["debug_transfer_logging"]:
            logger.debug(f"发起 OpenList PUT 上传请求: {upload_url}")
        parsed_url = urlparse(upload_url)
        host = parsed_url.hostname
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        if self.transfer_config["debug_transfer_logging"] and host:
            try:
                addrinfo = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
                resolved = sorted({f"{item[4][0]}:{item[4][1]}" for item in addrinfo})
                logger.debug(
                    f"OpenList 上传目标解析: {host}:{port} -> {', '.join(resolved)}"
                )
            except Exception as e:
                logger.debug(f"解析 OpenList 上传目标失败: {host}:{port}, err={e}")

        try:
            request_started_at = time.monotonic()
            async with self.session.put(
                upload_url, data=payload, headers=headers, timeout=timeout
            ) as response:
                request_elapsed = time.monotonic() - request_started_at
                logger.debug(
                    f"OpenList 上传响应: HTTP {response.status}, 文件: {filename}, "
                    f"elapsed={request_elapsed:.2f}s"
                )
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 200:
                        return True
                    logger.error(
                        f"上传失败，服务器返回错误 - code: {result.get('code')}, "
                        f"message: {result.get('message', '未知错误')}, 完整响应: {result}"
                    )
                    return False

                error_text = await response.text()
                logger.error(
                    f"上传失败 - HTTP状态: {response.status}, 响应内容: {error_text}, "
                    f"目标路径: {target_path}/{filename}"
                )
                return False
        except Exception as e:
            logger.error(
                f"OpenList PUT 请求失败: {e}, 文件: {filename}, source={source_desc}, "
                f"目标路径: {target_path}/{filename}",
                exc_info=True,
            )
            return False

    async def mkdir(self, path: str) -> bool:
        """在Openlist创建目录"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            mkdir_data = {"path": path}

            async with self.session.post(
                f"{self.base_url}/api/fs/mkdir", json=mkdir_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return True
                    else:
                        # 405 可能表示目录已存在，通常也视为成功
                        if result.get("code") == 405:
                            return True
                        logger.error(
                            f"创建目录失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}"
                        )
                        return False
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"创建目录失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}"
                    )
                    return False
        except Exception as e:
            logger.error(f"创建目录失败: {e}, 路径: {path}", exc_info=True)
            return False

    async def ensure_dir(self, path: str) -> bool:
        """逐级创建目录，兼容父目录不存在的目标路径。"""
        normalized = (path or "/").strip().replace("\\", "/")
        if not normalized or normalized == "/":
            return True
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        parts = [part for part in normalized.strip("/").split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            if not await self.mkdir(current):
                return False
        return True

    async def remove(self, dir_path: str, names: List[str]) -> bool:
        """删除文件或目录"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            remove_data = {"dir": dir_path, "names": names}

            async with self.session.post(
                f"{self.base_url}/api/fs/remove", json=remove_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return True
                    else:
                        logger.error(
                            f"删除失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 目录: {dir_path}, 文件: {names}"
                        )
                        return False
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"删除失败 - HTTP状态: {resp.status}, 响应: {error_text}, 目录: {dir_path}, 文件: {names}"
                    )
                    return False
        except Exception as e:
            logger.error(
                f"删除失败: {e}, 目录: {dir_path}, 文件: {names}", exc_info=True
            )
            return False

    async def list_archive_contents(
        self, path: str, archive_path: str = "/"
    ) -> Optional[Dict]:
        """获取压缩包内的文件列表"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            archive_data = {"path": path, "archive_path": archive_path}

            async with self.session.post(
                f"{self.base_url}/api/fs/archive/list",
                json=archive_data,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    else:
                        logger.error(
                            f"获取压缩包列表失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}"
                        )
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"获取压缩包列表失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}"
                    )
                    return None
        except Exception as e:
            logger.error(f"获取压缩包列表失败: {e}, 路径: {path}", exc_info=True)
            return None
