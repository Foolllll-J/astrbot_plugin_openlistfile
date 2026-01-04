import os
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import quote
from astrbot.api import logger

class OpenlistClient:
    """Openlist API 客户端"""

    def __init__(
        self, base_url: str, public_base_url: str = "", username: str = "", password: str = "", token: str = "",fixed_base_directory: str = ""
    ):
        self.base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else ""
        self.username = username
        self.password = password
        self.token = token
        self.fixed_base_directory = fixed_base_directory
        self.session = None

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
                        logger.error(f"OpenList登录失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 用户名: {self.username}")
                        return False
                else:
                    error_text = await resp.text()
                    logger.error(f"OpenList登录失败 - HTTP状态: {resp.status}, 响应: {error_text}, 用户名: {self.username}")
                    return False
        except Exception as e:
            logger.error(f"OpenList登录失败: {e}, 用户名: {self.username}, 服务器: {self.base_url}", exc_info=True)
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
                        logger.error(f"获取文件列表失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}")
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(f"获取文件列表失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}")
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
                        logger.error(f"获取文件信息失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}")
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(f"获取文件信息失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}")
                    return None
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}, 路径: {path}", exc_info=True)
            return None

    async def search_files(self, keyword: str, path: str = "/", per_page: int = 1000) -> Optional[List[Dict]]:
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
                        logger.error(f"搜索文件失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 关键词: {keyword}, 路径: {path}")
                        return []
                else:
                    error_text = await resp.text()
                    logger.error(f"搜索文件失败 - HTTP状态: {resp.status}, 响应: {error_text}, 关键词: {keyword}, 路径: {path}")
                    return []
        except Exception as e:
            logger.error(f"搜索文件失败: {e}, 关键词: {keyword}, 路径: {path}", exc_info=True)
            return []

    async def get_download_url(self, path: str) -> Optional[str]:
        """获取文件下载链接"""
        file_info = await self.get_file_info(path)

        if file_info and not file_info.get("is_dir", True):
            sign = file_info.get("sign")
            base_url_to_use = self.public_base_url if self.public_base_url else self.base_url

            if self.fixed_base_directory:
                full_path = f"{self.fixed_base_directory.rstrip('/')}/{path.lstrip('/')}"
            else:
                full_path = path

            encoded_url_path = quote(full_path.encode("utf-8"))

            if not sign:
                logger.warning(
                    f"无法为 {path} 获取签名，可能需要开启 '全部签名' 选项。返回无签名链接。"
                )
                return f"{base_url_to_use}/d{encoded_url_path}"

            return f"{base_url_to_use}/d{encoded_url_path}?sign={sign}"

        return None

    async def upload_file(
        self, file_path: str, target_path: str, filename: str = None
    ) -> bool:
        """上传文件到Openlist"""
        try:
            if not os.path.exists(file_path):
                logger.error(f"文件不存在: {file_path}")
                return False

            if filename is None:
                filename = os.path.basename(file_path)

            upload_url = f"{self.base_url}/api/fs/put"

            with open(file_path, "rb") as f:
                file_data = f.read()

            headers = {
                "Content-Type": "application/octet-stream",
                "File-Path": quote(f"{target_path.rstrip('/')}/{filename}", safe="/"),
            }

            if hasattr(self, "token") and self.token:
                headers["Authorization"] = self.token

            async with self.session.put(
                upload_url, data=file_data, headers=headers
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 200:
                        return True
                    else:
                        logger.error(f"上传失败，服务器返回错误 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 完整响应: {result}")
                        return False
                else:
                    error_text = await response.text()
                    logger.error(f"上传失败 - HTTP状态: {response.status}, 响应内容: {error_text}, 目标路径: {target_path}/{filename}")
                    return False

        except Exception as e:
            logger.error(f"上传文件失败: {e}, 文件路径: {file_path}, 目标路径: {target_path}/{filename}", exc_info=True)
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
                        logger.error(f"创建目录失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}")
                        return False
                else:
                    error_text = await resp.text()
                    logger.error(f"创建目录失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}")
                    return False
        except Exception as e:
            logger.error(f"创建目录失败: {e}, 路径: {path}", exc_info=True)
            return False

    async def list_archive_contents(self, path: str, archive_path: str = "/") -> Optional[Dict]:
        """获取压缩包内的文件列表"""
        try:
            headers = {}
            if self.token:
                headers["Authorization"] = self.token

            archive_data = {
                "path": path,
                "archive_path": archive_path
            }

            async with self.session.post(
                f"{self.base_url}/api/fs/archive/list", json=archive_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    else:
                        logger.error(f"获取压缩包列表失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}, 路径: {path}")
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(f"获取压缩包列表失败 - HTTP状态: {resp.status}, 响应: {error_text}, 路径: {path}")
                    return None
        except Exception as e:
            logger.error(f"获取压缩包列表失败: {e}, 路径: {path}", exc_info=True)
            return None
