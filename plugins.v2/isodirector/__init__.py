from pathlib import Path
from typing import Any, List, Dict, Tuple
import shutil
import logging

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, NotificationType

logger = logging.getLogger("ISOMatcherPlugin")

class ISOMatcherPlugin(_PluginBase):
    # 插件基础信息
    plugin_name = "ISO原盘匹配"
    plugin_desc = "将ISO文件匹配到原盘电影目录"
    plugin_icon = "ISO.png"
    plugin_version = "2.0"
    plugin_author = "wushuangshangjiang"
    author_url = "https://github.com/wushuangshangjiang"
    plugin_config_prefix = "isodirector_"
    plugin_order = 1
    auth_level = 1

    _enabled = False
    _notify = False
    _move_file = False
    _iso_dir = ""
    _movie_root_dir = ""

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._move_file = config.get("move_file", False)
            self._iso_dir = config.get("iso_dir", "")
            self._movie_root_dir = config.get("movie_root_dir", "")

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        插件配置页面
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
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'move_file', 'label': '移动文件(否则复制)'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextField', 'props': {'model': 'iso_dir', 'label': 'ISO 文件目录'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextField', 'props': {'model': 'movie_root_dir', 'label': '原盘电影根目录'}}
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "move_file": False,
            "iso_dir": "",
            "movie_root_dir": ""
        }

    @eventmanager.register(ChainEventType.TransferRename)
    def handle_event(self, event: Event):
        """
        匹配 ISO 文件到原盘电影目录
        """
        if not self.get_state():
            logger.debug("ISO匹配插件未启用")
            return
        if not event or not hasattr(event, 'event_data'):
            logger.warning("ISO匹配异常：事件对象为空或缺少 event_data")
            return

        try:
            iso_path = Path(self._iso_dir)
            movie_root_path = Path(self._movie_root_dir)

            if not iso_path.exists() or not movie_root_path.exists():
                logger.error("ISO目录或原盘电影目录不存在")
                return

            matched_files = []

            for iso_file in iso_path.rglob("*.iso"):
                iso_name = iso_file.stem
                matched_dirs = list(movie_root_path.glob(f"*{iso_name}*"))
                if matched_dirs:
                    target_dir = matched_dirs[0]
                    if self._move_file:
                        shutil.move(str(iso_file), target_dir / iso_file.name)
                    else:
                        shutil.copy2(str(iso_file), target_dir / iso_file.name)
                    matched_files.append(f"{iso_file.name} -> {target_dir}")
                else:
                    logger.warning(f"未找到匹配原盘目录: {iso_file.name}")

            # 更新事件数据
            if matched_files:
                event.event_data.updated = True
                event.event_data.updated_str = "\n".join(matched_files)
                event.event_data.source = "ISOMatcherPlugin"

                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Organize,
                        title="ISO匹配完成",
                        text=f"已匹配文件:\n{event.event_data.updated_str}"
                    )
            else:
                event.event_data.updated = False
                logger.info("ISO匹配完成，但没有匹配到任何文件")

        except Exception as e:
            logger.error(f"ISO匹配异常: {str(e)}", exc_info=True)
            if hasattr(event, 'event_data') and event.event_data:
                event.event_data.updated = False
                event.event_data.updated_str = ""

    def stop_service(self):
        """
        停止服务
        """
        pass
