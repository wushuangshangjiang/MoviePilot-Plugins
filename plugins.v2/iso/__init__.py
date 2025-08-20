from pathlib import Path
from typing import Any, List, Dict, Tuple
import logging

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, NotificationType

logger = logging.getLogger("ISOPathPlugin")

class ISO(_PluginBase):
    # 插件基础信息
    plugin_name = "ISO原盘匹配"
    plugin_desc = "将ISO文件匹配到原盘电影目录"
    plugin_icon = "directory.png"
    plugin_version = "2.2"
    plugin_author = "wushuangshangjiang"
    author_url = "https://github.com/wushuangshangjiang"
    plugin_config_prefix = "iso_"
    plugin_order = 1
    auth_level = 1

    _enabled = False
    _notify = False
    _iso_target_dir = ""

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._iso_target_dir = config.get("iso_target_dir", "")

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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'iso_target_dir', 'label': 'ISO目标目录'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "iso_target_dir": ""
        }

    @eventmanager.register(ChainEventType.TransferRename)
    def handle_event(self, event: Event):
        """
        重命名完成后，如果是ISO文件，替换目标路径
        """
        if not self.get_state():
            logger.debug("ISO路径替换插件未启用")
            return
        if not event or not hasattr(event, 'event_data'):
            logger.warning("ISO路径替换异常：事件对象为空或缺少 event_data")
            return

        try:
            data = event.event_data
            if not hasattr(data, 'render_str') or not data.render_str:
                logger.warning("ISO路径替换异常：render_str为空")
                return

            # 检查文件后缀
            file_path = Path(data.render_str)
            if file_path.suffix.lower() == ".iso":
                target_path = Path(self._iso_target_dir) / file_path.name
                event.event_data.updated_str = str(target_path)
                event.event_data.updated = True
                event.event_data.source = "ISOPathPlugin"
                logger.debug(f"ISO文件目标路径已替换: {target_path}")

                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Organize,
                        title="ISO路径替换完成",
                        text=f"{file_path.name} 的目标路径已修改为 {target_path}"
                    )
            else:
                # 非ISO文件不处理
                event.event_data.updated = False

        except Exception as e:
            logger.error(f"ISO路径替换异常: {str(e)}", exc_info=True)
            if hasattr(event, 'event_data') and event.event_data:
                event.event_data.updated = False
                event.event_data.updated_str = getattr(event.event_data, 'render_str', '')

    def stop_service(self):
        pass
