from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
import requests


class TmdbHook(_PluginBase):
    plugin_name = "TmdbHook"
    plugin_desc = "使用自建 TMDB 反代服务，锁定电影/剧集名称"
    plugin_icon = "tmdb.png"
    plugin_version = "1.0"
    plugin_author = "wushuangshangjiang"
    author_url = "https://github.com/wushuangshangjiang"
    plugin_config_prefix = "tmdbhook_"
    plugin_order = 10
    auth_level = 1

    _enabled = False
    _proxy_url = ""

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._proxy_url = config.get("proxy_url", "").rstrip('/')

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'proxy_url',
                                            'label': 'TMDB 代理服务 URL',
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

    def stop_service(self):
        pass

    def get_metadata(self, media_type: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        MoviePilot 元数据请求钩子，劫持 TMDB 请求
        media_type: 'movie' 或 'tv'
        metadata: 包含 tmdb_id 和语言等参数
        """
        if not self._enabled or not self._proxy_url:
            return {}

        tmdb_id = metadata.get("tmdb_id")
        language = metadata.get("language", "zh-CN")

        if not tmdb_id or not media_type:
            return {}

        try:
            url = f"{self._proxy_url}/3/{media_type}/{tmdb_id}"
            resp = requests.get(url, params={"language": language}, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            title = (
                data.get("locked_title") or data.get("locked_name") or
                data.get("title") or data.get("name") or ""
            )
            original_title = (
                data.get("locked_original_title") or data.get("locked_original_name") or
                data.get("original_title") or data.get("original_name") or ""
            )

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
            logger.error(f"TmdbHook 插件请求失败: {e}")
            return {}
