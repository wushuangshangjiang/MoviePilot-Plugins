from typing import Any, List, Dict, Tuple
import logging
from pathlib import Path

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType

logger = logging.getLogger("ISO")

class ISO(_PluginBase):
    # 插件基础信息
    plugin_name = "ISO原盘匹配"
    plugin_desc = "识别到ISO文件时，将类别标记为原盘电影"
    plugin_icon = "directory.png"
    plugin_version = "2.8"
    plugin_author = "wushuangshangjiang"
    author_url = "https://github.com/wushuangshangjiang"
    plugin_config_prefix = "iso_"
    plugin_order = 1
    auth_level = 1

    _enabled = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)

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
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {"enabled": False}

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    @eventmanager.register(ChainEventType.NameRecognize)
    def handle_event(self, event: Event):
        """
        名称识别阶段：ISO 文件统一归类为原盘电影
        """
        if not self.get_state():
            return
        if not event or not hasattr(event, "event_data"):
            return

        try:
            data = event.event_data
            # 确保存在 file_path 或 render_str 字段
            path_str = getattr(data, "file_path", None) or getattr(data, "render_str", None)
            if not path_str:
                return

            file_path = Path(path_str)
            if file_path.suffix.lower() == ".iso":
                # 修改分类信息
                if hasattr(data, 'category'):
                    data.category = "D-原盘电影"
                if hasattr(data, 'movie_type'):
                    data.movie_type = "电影"
                logger.debug(f"ISO 文件识别为原盘电影: {file_path.name}")

        except Exception as e:
            logger.error(f"ISO识别异常: {str(e)}", exc_info=True)
            # 保证数据字段安全，不破坏主程序
            if hasattr(event, 'event_data') and event.event_data:
                if hasattr(event.event_data, 'category'):
                    event.event_data.category = getattr(event.event_data, 'category', '')
                if hasattr(event.event_data, 'movie_type'):
                    event.event_data.movie_type = getattr(event.event_data, 'movie_type', '')
    def stop_service(self):
        pass
