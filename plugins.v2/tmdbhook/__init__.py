from app.plugins import _PluginBase
from app.utils.metadata import TMDB
import requests
from typing import Any, List, Dict, Tuple
from app.log import logger


class TmdbHook(_PluginBase):
    plugin_name = "TmdbHook"
    plugin_desc = "使用自建 TMDB 反代服务，锁定电影/剧集名称"
    plugin_icon = "tmdb.png"
    plugin_version = "1.1"
    plugin_author = "wushuangshangjiang"
    author_url = "https://github.com/wushuangshangjiang"
    plugin_config_prefix = "tmdbhook_"
    plugin_order = 10
    auth_level = 1

    _enabled = False
    _proxy_url = ""

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置，并进行 TMDB 方法替换
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._proxy_url = config.get("proxy_url", "").rstrip('/')
        if self._enabled:
            self._patch_tmdb()

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VSwitch',
                        'props': {
                            'model': 'enabled',
                            'label': '启用插件',
                        }
                    },
                    {
                        'component': 'VTextField',
                        'props': {
                            'model': 'proxy_url',
                            'label': 'TMDB 代理地址',
                            'placeholder': 'http://127.0.0.1:9000'
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "proxy_url": ""
        }

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    def _patch_tmdb(self):
        """
        替换 TMDB.get 方法，将所有 TMDB 请求改为使用代理地址
        """
        proxy_url = self._proxy_url

        if not proxy_url:
            logger.warning("[TmdbHook] 未设置代理地址，不进行 patch")
            return

        logger.info(f"[TmdbHook] TMDB 请求将被代理至：{proxy_url}")

        def _proxy_get(self, url: str, **kwargs):
            # 替换 URL
            if url.startswith("https://api.themoviedb.org/3"):
                new_url = url.replace("https://api.themoviedb.org/3", f"{proxy_url}/3")
            else:
                new_url = url
            try:
                response = requests.get(new_url, params=kwargs.get("params"), timeout=10)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(f"[TmdbHook] TMDB 请求失败: {e}")
                return {}

        # 替换 TMDB 的 get 方法
        TMDB.get = _proxy_get.__get__(TMDB, TMDB)
