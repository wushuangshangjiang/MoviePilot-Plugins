from pathlib import Path
from typing import Any, List, Dict, Tuple
import logging

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType

logger = logging.getLogger("ISOPlugin")

class ISO(_PluginBase):
    # 插件基础信息
    plugin_name = "ISO原盘分类"
    plugin_desc = "在媒体识别阶段将ISO文件分类为原盘电影"
    plugin_icon = "directory.png"
    plugin_version = "2.0"
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
        ], {
            "enabled": False
        }

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    @eventmanager.register(ChainEventType.MediaRecognizeConvert)
    def handle_media_recognize(self, event: Event):
        """
        在媒体识别转换阶段，如果是ISO文件，直接修改类别为D-原盘电影
        """
        if not self.get_state():
            return
        if not event or not hasattr(event, 'event_data'):
            return

        data = event.event_data
        if not hasattr(data, 'render_str') or not data.render_str:
            return

        file_path = Path(data.render_str)
        if file_path.suffix.lower() == ".iso":
            if hasattr(data, 'category'):
                data.category = "D-原盘电影"
            if hasattr(data, 'movie_type'):
                data.movie_type = "电影"
            logger.debug(f"ISO文件识别时修改类别为 D-原盘电影: {file_path.name}")

    def stop_service(self):
        pass
