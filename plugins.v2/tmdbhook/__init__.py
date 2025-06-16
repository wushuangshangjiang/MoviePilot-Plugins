
from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from typing import Any, List, Dict, Tuple
from app.log import logger

import requests


class TmdbHook(_PluginBase):
    plugin_name = "tmdbhook"
    plugin_desc = "使用自建 TMDB 反代服务，锁定电影/剧集名称"
    plugin_icon = "tmdb.png"
    plugin_version = "1.0"
    plugin_author = "your_name"
    author_url = ""
    plugin_config_prefix = "tmdbproxy_"
    plugin_order = 10
    auth_level = 1

    # 配置参数，初始化默认值
    _proxy_url: str = "http://127.0.0.1:9000"
    _enabled: bool = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._proxy_url = config.get("proxy_url", "http://127.0.0.1:9000")

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置界面表单
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'proxy_url',
                                            'label': 'TMDB 代理服务基础 URL',
                                            'placeholder': 'http://127.0.0.1:9000'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "proxy_url": "http://127.0.0.1:9000"
        }

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    def fetch_metadata(self, media_type: str, tmdb_id: str, language: str = "zh-CN") -> dict:
        """
        主动调用的辅助函数：通过反代请求 TMDB，返回锁定名称等元数据
        """
        if not self._enabled or not self._proxy_url or not tmdb_id:
            return {}

        url = f"{self._proxy_url}/3/{media_type}/{tmdb_id}"
        params = {"language": language}

        try:
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            # 提取锁定名称和其他元数据
            title = data.get("locked_title") or data.get("locked_name") or data.get("title") or data.get("name") or ""
            original_title = data.get("locked_original_title") or data.get("locked_original_name") or data.get("original_title") or data.get("original_name") or ""
            year = ""
            if "release_date" in data and data["release_date"]:
                year = data["release_date"][:4]
            elif "first_air_date" in data and data["first_air_date"]:
                year = data["first_air_date"][:4]

            return {
                "title": title,
                "original_title": original_title,
                "year": year,
                "overview": data.get("overview", ""),
                "poster_path": data.get("poster_path", ""),
                "tmdb_id": tmdb_id,
                "language": language,
            }
        except Exception as e:
            logger.error(f"TMDB代理请求失败: {str(e)}")
            return {}

    # 你可以选择注册某个事件，或者写插件内调用fetch_metadata的逻辑
    # 这里只示范事件注册的写法，假设监听某事件后调用fetch_metadata（视你业务需求）
    @eventmanager.register(EventType)
    def on_event(self, event):
        # 根据事件数据判断是否调用fetch_metadata等逻辑
        pass

    def stop_service(self):
        # 停止插件时执行的操作
        pass
