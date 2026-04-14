import base64
import datetime
import gc
import hashlib
import importlib
import os
import re
import ast
import sys
import threading
import time
import shutil
import random
import requests
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import pytz
import yaml

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.mediaserver import MediaServerChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo, TransferInfo
from app.schemas.types import EventType
from app.schemas import ServiceInfo
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils

sys.dont_write_bytecode = True


class _ManualServerInstance:
    def __init__(self, host: str, api_key: str):
        self.host = (host or "").strip().rstrip("/") + "/"
        self.api_key = (api_key or "").strip()

    def is_inactive(self):
        return False

    def _replace_url(self, url: str) -> str:
        real_url = (url or "").replace("[HOST]", self.host).replace("[APIKEY]", self.api_key)
        if real_url.startswith("emby/"):
            real_url = self.host + real_url
        return real_url

    def get_data(self, url: str):
        real_url = self._replace_url(url)
        return requests.get(real_url, timeout=20, verify=False)

    def post_data(self, url: str, data=None, headers=None):
        real_url = self._replace_url(url)
        return requests.post(real_url, data=data, headers=headers, timeout=30, verify=False)


class _ManualService:
    def __init__(self, name: str, host: str, api_key: str):
        self.name = name
        self.type = "emby"
        self.instance = _ManualServerInstance(host=host, api_key=api_key)


class WsEmbyCover(_PluginBase):
    # 插件名称
    plugin_name = "无双Emby封面"
    # 插件描述
    plugin_desc = "生成媒体库动态/静态封面，支持 Emby/Jellyfin"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/icons/emby.png"
    # 插件版本
    plugin_version = "1.54"
    # 插件作者
    plugin_author = "wushuangshangjiang"
    # 作者主页
    author_url = "https://github.com/wushuangshangjiang/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "wsembycover_"
    # 加载顺序
    plugin_order = 2
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = threading.Event()

    # 私有属性
    _scheduler = None
    mschain = None
    mediaserver_helper = None
    _enabled = False
    _update_now = False
    _transfer_monitor = True
    _cron = None
    _delay = 60
    _servers = None
    _selected_servers = []
    _servers_config = ''
    _server_style_map = {}
    _manual_servers = []
    _server_profiles = {}
    _active_server_name = ""
    _active_server_edit_target = ""
    _active_server_host = ""
    _active_server_api_key = ""
    _active_server_style = "static_1"
    _all_libraries = []
    _include_libraries = []
    _selected_libraries = []
    _sort_by = 'Random'
    _monitor_sort = ''
    _current_updating_items = set()
    _covers_output = ''
    _covers_input = ''
    _zh_font_url = ''
    _en_font_url = ''
    _zh_font_path = ''
    _en_font_path = ''
    _title_config = ''
    _current_config = {}
    _cover_style = 'static_1'
    _cover_style_base = 'static_1'
    _font_path = ''
    _covers_path = ''
    _tab = 'title-tab'
    _multi_1_blur = True
    _zh_font_size = None
    _en_font_size = None
    _blur_size = 50
    _color_ratio = 0.8
    _use_primary = False
    _seen_keys = set()
    _zh_font_custom = ''
    _en_font_custom = ''
    _zh_font_preset = 'chaohei'
    _en_font_preset = 'EmblemaOne'
    _zh_font_offset = ''
    _title_spacing = ''
    _en_line_spacing = ''
    _title_scale = 1.0
    _resolution = '480p'
    _custom_width = 1920
    _custom_height = 1080
    _bg_color_mode = 'auto'
    _custom_bg_color = ''
    _resolution_config = None
    _style_naming_v2 = True
    _sanitize_log_cache = set()
    _clean_images = False
    _clean_fonts = False
    _save_recent_covers = True
    _covers_history_limit_per_library = 10
    _covers_page_history_limit = 50
    _debug_mode = False

    def __init__(self):
        super().__init__()

    def init_plugin(self, config: dict = None):
        self.mschain = MediaServerChain()
        data_path = self.get_data_path()
        (data_path / 'fonts').mkdir(parents=True, exist_ok=True)
        (data_path / 'input').mkdir(parents=True, exist_ok=True)
        self._covers_path = data_path / 'input'
        self._font_path = data_path / 'fonts'
        if config:
            self._enabled = config.get("enabled")
            self._update_now = config.get("update_now")
            self._transfer_monitor = config.get("transfer_monitor")
            self._cron = config.get("cron")
            self._delay = config.get("delay")
            self._selected_servers = []
            self._servers_config = config.get("servers_config", "")
            self._manual_servers = self.__parse_manual_servers_from_config(config)
            parsed_from_servers_config = self.__parse_servers_config(self._servers_config)
            if parsed_from_servers_config:
                self._server_profiles = {}
                for item in parsed_from_servers_config:
                    name = str(item.get("name", "")).strip()
                    if not name:
                        continue
                    self._server_profiles[name] = self.__profile_from_runtime(
                        name=name,
                        host=item.get("host", ""),
                        api_key=item.get("api_key", ""),
                        style=item.get("style", "static_1"),
                    )
                form_profiles = {}
            else:
                form_profiles = self.__parse_server_profiles_from_form_slots(config)
                self._server_profiles = self.__parse_server_profiles_from_config(config) or form_profiles
            self._include_libraries = []
            selected_libraries = config.get("selected_libraries")
            if isinstance(selected_libraries, list):
                self._selected_libraries = [str(item or "").strip() for item in selected_libraries if str(item or "").strip()]
            else:
                legacy_single = str(config.get("selected_library", "") or "").strip()
                if legacy_single:
                    self._selected_libraries = [legacy_single]
                else:
                    legacy_include = config.get("include_libraries")
                    if isinstance(legacy_include, list):
                        self._selected_libraries = [str(item or "").strip() for item in legacy_include if str(item or "").strip()]
                    else:
                        self._selected_libraries = []
            self._sort_by = config.get("sort_by")
            self._covers_output = config.get("covers_output")
            self._covers_input = config.get("covers_input")
            # self._title_config = self.get_data('title_config')
            self._title_config = config.get("title_config")
            self._zh_font_url = config.get("zh_font_url")
            self._en_font_url = config.get("en_font_url")
            self._zh_font_path = config.get("zh_font_path")
            self._en_font_path = config.get("en_font_path")
            self._cover_style = config.get("cover_style", "static_1")

            # 样式命名升级兼容（仅对旧配置执行一次迁移）
            if not config.get("style_naming_v2"):
                if self._cover_style == 'single_1':
                    self._cover_style = 'static_1'
                elif self._cover_style == 'single_2':
                    self._cover_style = 'static_2'
                elif self._cover_style == 'multi_1':
                    self._cover_style = 'static_2'
            default_base, default_variant = self.__resolve_cover_style_ui(self._cover_style)
            self._cover_style_base = config.get("cover_style_base", default_base)
            self._multi_1_blur = config.get("multi_1_blur", True)
            self._zh_font_size = config.get("zh_font_size", 170)
            self._en_font_size = config.get("en_font_size", 75)
            try:
                self._blur_size = int(config.get("blur_size", 50))
            except (ValueError, TypeError):
                self._blur_size = 50
            try:
                self._color_ratio = float(config.get("color_ratio", 0.8))
            except (ValueError, TypeError):
                self._color_ratio = 0.8
            self._use_primary = config.get("use_primary")
            self._zh_font_custom = config.get("zh_font_custom", "")
            self._en_font_custom = config.get("en_font_custom", "")
            self._zh_font_preset = config.get("zh_font_preset", "chaohei")
            self._en_font_preset = config.get("en_font_preset", "EmblemaOne")
            self._zh_font_offset = config.get("zh_font_offset")
            self._title_spacing = config.get("title_spacing")
            self._en_line_spacing = config.get("en_line_spacing")
            try:
                self._title_scale = float(config.get("title_scale", 1.0))
            except (ValueError, TypeError):
                self._title_scale = 1.0
            self._resolution = config.get("resolution", "480p")
            self._custom_width = config.get("custom_width", 1920)
            self._custom_height = config.get("custom_height", 1080)
            self._clean_images = config.get("clean_images", False)
            self._clean_fonts = config.get("clean_fonts", False)
            self._save_recent_covers = config.get("save_recent_covers", True)
            self._debug_mode = bool(config.get("debug_mode", config.get("debug_show_apikey", False)))
            self._covers_history_limit_per_library = self.__clamp_value(
                config.get("covers_history_limit_per_library", 10),
                1,
                100,
                10,
                "covers_history_limit_per_library[init_plugin]",
                int,
            )
            self._covers_page_history_limit = self.__clamp_value(
                config.get("covers_page_history_limit", 50),
                1,
                500,
                50,
                "covers_page_history_limit[init_plugin]",
                int,
            )
            self._active_server_name = str(config.get("active_server_name", "") or "").strip()
            self._active_server_edit_target = str(config.get("active_server_edit_target", "") or "").strip()
            self._active_server_host = str(config.get("active_server_host", "") or "").strip()
            self._active_server_api_key = str(config.get("active_server_api_key", "") or "").strip()
            self._active_server_style = str(config.get("active_server_style", "static_1") or "static_1").strip() or "static_1"
            loaded_profiles_from_form = bool(form_profiles)
        else:
            loaded_profiles_from_form = False

            if self._resolution not in ["1080p", "720p", "480p"]:
                self._resolution = "480p"

        self._bg_color_mode = (config or {}).get("bg_color_mode", "auto")
        self._custom_bg_color = (config or {}).get("custom_bg_color", "")

        # 初始化分辨率配置（确保安全初始化）
        try:
            self._resolution_config = self.__new_resolution_config(self._resolution)
        except Exception as e:
            logger.warning(f"分辨率配置初始化失败，使用默认配置: {e}")
            self._resolution_config = self.__new_resolution_config("480p")

        self._servers = {}
        self._server_style_map = {}
        self._all_libraries = []
        profile_dirty = False
        if not self._server_profiles:
            self._server_profiles = self.__build_server_profiles_from_legacy(config or {})
            profile_dirty = bool(self._server_profiles)
        if (not loaded_profiles_from_form) and self.__upsert_active_server_profile(config or {}):
            profile_dirty = True
        if self.__sync_profile_styles_with_selected_style():
            profile_dirty = True
        self._manual_servers = self.__profiles_to_manual_servers()
        self.__sync_active_server_editor()
        parsed_servers = self._manual_servers or self.__parse_servers_config(self._servers_config)
        for server_item in parsed_servers:
            server_name = server_item.get("name")
            host = server_item.get("host")
            api_key = server_item.get("api_key")
            style = server_item.get("style", "static_1")
            service = _ManualService(name=server_name, host=host, api_key=api_key)
            self._servers[server_name] = service
            self._server_style_map[server_name] = style if style in {"static_1", "static_2"} else "static_1"
            self._all_libraries.extend(self.__get_all_libraries(server_name, service))
        available_library_values = {
            str(item.get("value", "")).strip()
            for item in self._all_libraries
            if isinstance(item, dict) and str(item.get("value", "")).strip()
        }
        normalized_selected_libraries = []
        for item in (self._selected_libraries or []):
            raw_item = str(item or "").strip()
            if raw_item and raw_item in available_library_values:
                normalized_selected_libraries.append(raw_item)
        if normalized_selected_libraries != (self._selected_libraries or []):
            self._selected_libraries = normalized_selected_libraries
            profile_dirty = True
        if profile_dirty:
            self.__update_config()

        if not self._servers:
            logger.info("未配置可用媒体服务器")
        
        # 停止现有任务
        self.stop_service()

        cleanup_triggered = False
        if self._clean_images:
            self.__clean_generated_images()
            self._clean_images = False
            cleanup_triggered = True
        if self._clean_fonts:
            self.__clean_downloaded_fonts()
            self._clean_fonts = False
            cleanup_triggered = True
        if cleanup_triggered:
            self.__update_config()

        if self._update_now:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(func=self.__update_all_libraries, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                    )
            logger.info(f"媒体库封面更新服务启动，立即运行一次")
            # 关闭一次性开关
            self._update_now = False
            # 保存配置
            self.__update_config()
            # 启动服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __clamp_value(self, value, minimum, maximum, default_value, name, cast_type):
        try:
            parsed = cast_type(value)
        except (ValueError, TypeError):
            logger.warning(f"{name} 配置值非法 ({value})，已回退默认值 {default_value}")
            return default_value

        if parsed < minimum or parsed > maximum:
            clamped = max(minimum, min(maximum, parsed))
            logger.warning(f"{name} 配置值超出范围 ({parsed})，已限制为 {clamped}")
            return clamped

        return parsed

    def __load_style_creator(self, module_name: str, func_name: str):
        module = importlib.import_module(f"app.plugins.wsembycover.style.{module_name}")
        return getattr(module, func_name)

    def __new_resolution_config(self, resolution):
        class LocalResolutionConfig:
            PRESETS = {
                "1080p": (1920, 1080),
                "720p": (1280, 720),
                "480p": (854, 480),
                "360p": (640, 360),
                "4k": (3840, 2160),
                "1440p": (2560, 1440),
                "custom": None,
            }

            def __init__(self, value):
                if isinstance(value, str):
                    preset = self.PRESETS.get(value)
                    self._resolution = preset or (1920, 1080)
                    self._preset_name = value if preset else "1080p"
                elif isinstance(value, (tuple, list)) and len(value) == 2:
                    self._resolution = (int(value[0]), int(value[1]))
                    self._preset_name = "custom"
                else:
                    self._resolution = (1920, 1080)
                    self._preset_name = "1080p"

            @property
            def width(self):
                return self._resolution[0]

            @property
            def height(self):
                return self._resolution[1]

            def get_font_size(self, base_size: int, scale_factor: float = 1.0) -> int:
                height_scale = self.height / 1080.0
                return int(base_size * height_scale * scale_factor)

            def __str__(self):
                return f"{self.width}x{self.height}"

        return LocalResolutionConfig(resolution)

    def __validate_font_file(self, font_path: Path):
        return self._validate_font_file(font_path)

    def __parse_servers_config(self, config_text: str) -> List[Dict[str, str]]:
        if not config_text or not str(config_text).strip():
            return []
        try:
            raw = yaml.safe_load(config_text) or []
        except Exception as e:
            logger.error(f"服务器配置解析失败: {e}")
            return []

        items: List[Dict[str, str]] = []

        def append_item(name: str, host: str, api_key: str, style: str = "static_1"):
            safe_name = str(name or "").strip()
            safe_host = str(host or "").strip()
            safe_api_key = str(api_key or "").strip()
            safe_style = str(style or "static_1").strip() or "static_1"
            if not safe_name or not safe_host or not safe_api_key:
                return
            if not safe_host.startswith(("http://", "https://")):
                safe_host = f"http://{safe_host}"
            items.append({
                "name": safe_name,
                "host": safe_host.rstrip("/") + "/",
                "api_key": safe_api_key,
                "style": "static_2" if safe_style == "static_2" else "static_1",
            })

        def parse_mapping(name: str, value: Any):
            if isinstance(value, dict):
                append_item(
                    name=name,
                    host=str(value.get("host", "") or value.get("地址", "")).strip(),
                    api_key=str(value.get("api_key", "") or value.get("apikey", "") or value.get("ApiKey", "")).strip(),
                    style=str(value.get("style", "static_1")).strip() or "static_1",
                )
                return
            if isinstance(value, (list, tuple)):
                host = str(value[0]).strip() if len(value) > 0 else ""
                api_key = str(value[1]).strip() if len(value) > 1 else ""
                style = str(value[2]).strip() if len(value) > 2 else "static_1"
                append_item(name=name, host=host, api_key=api_key, style=style)
                return
            if isinstance(value, str):
                append_item(name=name, host=value, api_key="", style="static_1")

        if isinstance(raw, dict):
            # 新格式：
            # 服务器1:
            #   - http://127.0.0.1:8096
            #   - apikey
            if any(key in raw for key in ("name", "host", "api_key")):
                raw = [raw]
            else:
                for server_name, cfg in raw.items():
                    parse_mapping(str(server_name), cfg)
                return items

        if not isinstance(raw, list):
            logger.error("服务器配置格式错误，应为列表或字典")
            return []

        for one in raw:
            if not isinstance(one, dict):
                continue
            # 兼容列表中的新格式单项：{服务器名: [host, apikey]}
            if "name" not in one and "host" not in one and "api_key" not in one and len(one) == 1:
                only_name, only_value = next(iter(one.items()))
                parse_mapping(str(only_name), only_value)
                continue

            name = str(one.get("name", "")).strip()
            host = str(one.get("host", "")).strip()
            api_key = str(one.get("api_key", "")).strip()
            style = str(one.get("style", "static_1")).strip() or "static_1"
            if not name or not host or not api_key:
                continue
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
            items.append({
                "name": name,
                "host": host.rstrip("/") + "/",
                "api_key": api_key,
                "style": "static_2" if style == "static_2" else "static_1",
            })
        return items

    def __parse_manual_servers_from_config(self, config: dict) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        cfg = config or {}
        for idx in range(1, 6):
            name = str(cfg.get(f"server_{idx}_name", "")).strip()
            host = str(cfg.get(f"server_{idx}_host", "")).strip()
            api_key = str(cfg.get(f"server_{idx}_api_key", "")).strip()
            style = str(cfg.get(f"server_{idx}_style", "static_1")).strip() or "static_1"
            if not name or not host or not api_key:
                continue
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
            items.append({
                "name": name,
                "host": host.rstrip("/") + "/",
                "api_key": api_key,
                "style": "static_2" if style == "static_2" else "static_1",
            })
        return items

    def __manual_server_slot_value(self, idx: int, key: str, default: str = "") -> str:
        if not self._manual_servers:
            return default
        pos = idx - 1
        if pos < 0 or pos >= len(self._manual_servers):
            return default
        return str(self._manual_servers[pos].get(key, default))

    @staticmethod
    def __default_title_config_template() -> str:
        return '''# 配置封面标题（支持按服务器分组）
# 推荐格式（按服务器分组）：
#
# 服务器1:
#   媒体库名称:
#     - 主标题
#     - 副标题
#   另一个媒体库:
#     - 主标题
#     - 副标题
#
# 兼容旧格式（不分服务器）：
# 媒体库名称:
#   - 主标题
#   - 副标题
#   - "#FF5722"  # 背景颜色（可选，必须加引号）
#
'''

    @staticmethod
    def __default_servers_config_template() -> str:
        return '''# 配置多服务器
# 格式如下：
#
# 服务器1:
#   - http://127.0.0.1:8096
#   - xxxxx
# 服务器2:
#   - http://192.168.1.10:8096
#   - yyyyy
#
'''

    def __profile_from_runtime(self, name: str, host: str, api_key: str, style: str) -> Dict[str, Any]:
        safe_style = "static_2" if style == "static_2" else "static_1"
        normalized_host = (host or "").strip()
        if normalized_host and not normalized_host.startswith(("http://", "https://")):
            normalized_host = f"http://{normalized_host}"
        if normalized_host:
            normalized_host = normalized_host.rstrip("/") + "/"
        return {
            "name": (name or "").strip(),
            "host": normalized_host,
            "api_key": (api_key or "").strip(),
            "style": safe_style,
            "title_config": self._title_config or self.__default_title_config_template(),
            "sort_by": self._sort_by or "Random",
            "covers_input": self._covers_input or "",
            "covers_output": self._covers_output or "",
            "save_recent_covers": bool(self._save_recent_covers),
            "covers_history_limit_per_library": self._covers_history_limit_per_library,
            "covers_page_history_limit": self._covers_page_history_limit,
            "use_primary": bool(self._use_primary),
            "multi_1_blur": bool(self._multi_1_blur),
            "resolution": self._resolution or "480p",
            "custom_width": self._custom_width,
            "custom_height": self._custom_height,
            "bg_color_mode": self._bg_color_mode or "auto",
            "custom_bg_color": self._custom_bg_color or "",
            "zh_font_preset": self._zh_font_preset or "chaohei",
            "en_font_preset": self._en_font_preset or "EmblemaOne",
            "zh_font_custom": self._zh_font_custom or "",
            "en_font_custom": self._en_font_custom or "",
            "zh_font_size": self._zh_font_size,
            "en_font_size": self._en_font_size,
            "blur_size": self._blur_size,
            "color_ratio": self._color_ratio,
            "title_scale": self._title_scale,
            "zh_font_offset": self._zh_font_offset,
            "title_spacing": self._title_spacing,
            "en_line_spacing": self._en_line_spacing,
        }

    def __normalize_server_profile(self, name: str, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        safe_name = (name or raw.get("name") or "").strip()
        host = str(raw.get("host", "")).strip()
        api_key = str(raw.get("api_key", "")).strip()
        if not safe_name or not host or not api_key:
            return None
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        host = host.rstrip("/") + "/"
        style = "static_2" if str(raw.get("style", "static_1")).strip() == "static_2" else "static_1"
        profile = {
            "name": safe_name,
            "host": host,
            "api_key": api_key,
            "style": style,
        }
        for key in [
            "title_config", "sort_by", "covers_input", "covers_output", "use_primary", "multi_1_blur",
            "save_recent_covers", "covers_history_limit_per_library", "covers_page_history_limit",
            "resolution", "custom_width", "custom_height", "bg_color_mode", "custom_bg_color",
            "zh_font_preset", "en_font_preset", "zh_font_custom", "en_font_custom",
            "zh_font_size", "en_font_size", "blur_size", "color_ratio", "title_scale",
            "zh_font_offset", "title_spacing", "en_line_spacing"
        ]:
            if key in raw:
                profile[key] = raw.get(key)
        return profile

    def __parse_server_profiles_from_config(self, config: dict) -> Dict[str, Dict[str, Any]]:
        cfg = config or {}
        raw_profiles = cfg.get("server_profiles")
        profiles: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_profiles, dict):
            for key, value in raw_profiles.items():
                normalized = self.__normalize_server_profile(str(key), value if isinstance(value, dict) else {})
                if normalized:
                    profiles[normalized["name"]] = normalized
        return profiles

    def __parse_server_profiles_from_form_slots(self, config: dict) -> Dict[str, Dict[str, Any]]:
        cfg = config or {}
        profiles: Dict[str, Dict[str, Any]] = {}
        for idx in range(1, 11):
            name = str(cfg.get(f"profile_{idx}_name", "") or "").strip()
            host = str(cfg.get(f"profile_{idx}_host", "") or "").strip()
            api_key = str(cfg.get(f"profile_{idx}_api_key", "") or "").strip()
            style = str(cfg.get(f"profile_{idx}_style", "static_1") or "static_1").strip() or "static_1"
            if not name or not host or not api_key:
                continue
            profile = self.__profile_from_runtime(name=name, host=host, api_key=api_key, style=style)
            profile["title_config"] = cfg.get(f"profile_{idx}_title_config", profile.get("title_config"))
            profile["sort_by"] = cfg.get(f"profile_{idx}_sort_by", profile.get("sort_by"))
            profile["covers_input"] = cfg.get(f"profile_{idx}_covers_input", profile.get("covers_input"))
            profile["covers_output"] = cfg.get(f"profile_{idx}_covers_output", profile.get("covers_output"))
            profile["use_primary"] = cfg.get(f"profile_{idx}_use_primary", profile.get("use_primary"))
            profile["multi_1_blur"] = cfg.get(f"profile_{idx}_multi_1_blur", profile.get("multi_1_blur"))
            profile["resolution"] = cfg.get(f"profile_{idx}_resolution", profile.get("resolution"))
            profile["bg_color_mode"] = cfg.get(f"profile_{idx}_bg_color_mode", profile.get("bg_color_mode"))
            profile["custom_bg_color"] = cfg.get(f"profile_{idx}_custom_bg_color", profile.get("custom_bg_color"))
            profile["zh_font_size"] = cfg.get(f"profile_{idx}_zh_font_size", profile.get("zh_font_size"))
            profile["en_font_size"] = cfg.get(f"profile_{idx}_en_font_size", profile.get("en_font_size"))
            profile["title_scale"] = cfg.get(f"profile_{idx}_title_scale", profile.get("title_scale"))
            profile["zh_font_offset"] = cfg.get(f"profile_{idx}_zh_font_offset", profile.get("zh_font_offset"))
            profile["title_spacing"] = cfg.get(f"profile_{idx}_title_spacing", profile.get("title_spacing"))
            profile["en_line_spacing"] = cfg.get(f"profile_{idx}_en_line_spacing", profile.get("en_line_spacing"))
            normalized = self.__normalize_server_profile(name, profile)
            if normalized:
                profiles[normalized["name"]] = normalized
        return profiles

    def __build_server_profiles_from_legacy(self, config: dict) -> Dict[str, Dict[str, Any]]:
        cfg = config or {}
        legacy_servers = self.__parse_manual_servers_from_config(cfg)
        if not legacy_servers:
            legacy_servers = self.__parse_servers_config(str(cfg.get("servers_config", "") or ""))
        profiles: Dict[str, Dict[str, Any]] = {}
        for item in legacy_servers:
            name = item.get("name")
            if not name:
                continue
            profile = self.__profile_from_runtime(
                name=name,
                host=item.get("host", ""),
                api_key=item.get("api_key", ""),
                style=item.get("style", "static_1"),
            )
            profiles[name] = profile
        return profiles

    def __profiles_to_manual_servers(self) -> List[Dict[str, str]]:
        servers: List[Dict[str, str]] = []
        for name in sorted(self._server_profiles.keys()):
            profile = self._server_profiles.get(name) or {}
            normalized = self.__normalize_server_profile(name, profile)
            if not normalized:
                continue
            servers.append({
                "name": normalized["name"],
                "host": normalized["host"],
                "api_key": normalized["api_key"],
                "style": normalized.get("style", "static_1"),
            })
        return servers

    def __apply_server_profile_values(self, profile: Dict[str, Any]):
        if not profile:
            return
        self._cover_style = "static_2" if str(profile.get("style", "static_1")) == "static_2" else "static_1"
        self._cover_style_base = self._cover_style
        self._title_config = profile.get("title_config") or self.__default_title_config_template()
        self._sort_by = profile.get("sort_by") or "Random"
        self._covers_input = profile.get("covers_input") or ""
        self._covers_output = profile.get("covers_output") or ""
        self._save_recent_covers = bool(profile.get("save_recent_covers", self._save_recent_covers))
        self._covers_history_limit_per_library = self.__clamp_value(
            profile.get("covers_history_limit_per_library", self._covers_history_limit_per_library),
            1, 100, 10, "covers_history_limit_per_library[profile]", int
        )
        self._covers_page_history_limit = self.__clamp_value(
            profile.get("covers_page_history_limit", self._covers_page_history_limit),
            1, 500, 50, "covers_page_history_limit[profile]", int
        )
        self._use_primary = bool(profile.get("use_primary", self._use_primary))
        self._multi_1_blur = bool(profile.get("multi_1_blur", self._multi_1_blur))
        self._resolution = str(profile.get("resolution", self._resolution or "480p"))
        self._custom_width = profile.get("custom_width", self._custom_width)
        self._custom_height = profile.get("custom_height", self._custom_height)
        self._bg_color_mode = profile.get("bg_color_mode", self._bg_color_mode or "auto")
        self._custom_bg_color = profile.get("custom_bg_color", self._custom_bg_color or "")
        self._zh_font_preset = profile.get("zh_font_preset", self._zh_font_preset or "chaohei")
        self._en_font_preset = profile.get("en_font_preset", self._en_font_preset or "EmblemaOne")
        self._zh_font_custom = profile.get("zh_font_custom", self._zh_font_custom or "")
        self._en_font_custom = profile.get("en_font_custom", self._en_font_custom or "")
        self._zh_font_size = profile.get("zh_font_size", self._zh_font_size)
        self._en_font_size = profile.get("en_font_size", self._en_font_size)
        self._blur_size = profile.get("blur_size", self._blur_size)
        self._color_ratio = profile.get("color_ratio", self._color_ratio)
        self._title_scale = profile.get("title_scale", self._title_scale)
        self._zh_font_offset = profile.get("zh_font_offset", self._zh_font_offset)
        self._title_spacing = profile.get("title_spacing", self._title_spacing)
        self._en_line_spacing = profile.get("en_line_spacing", self._en_line_spacing)
        if self._resolution not in ["1080p", "720p", "480p"]:
            self._resolution = "480p"
        try:
            self._resolution_config = self.__new_resolution_config(self._resolution)
        except Exception:
            self._resolution_config = self.__new_resolution_config("480p")

    @staticmethod
    def __fetch_emby_server_name(host: str, api_key: str) -> Optional[str]:
        safe_host = (host or "").strip().rstrip("/")
        safe_key = (api_key or "").strip()
        if not safe_host or not safe_key:
            return None
        candidates = [
            f"{safe_host}/emby/System/Info/Public?api_key={safe_key}",
            f"{safe_host}/System/Info/Public?api_key={safe_key}",
            f"{safe_host}/emby/System/Info?api_key={safe_key}",
            f"{safe_host}/System/Info?api_key={safe_key}",
        ]
        for url in candidates:
            try:
                resp = requests.get(url, timeout=10, verify=False)
                if not resp or resp.status_code >= 400:
                    continue
                data = resp.json() if resp.content else {}
                if not isinstance(data, dict):
                    continue
                name = str(data.get("ServerName") or data.get("Name") or "").strip()
                if name:
                    return name
            except Exception:
                continue
        return None

    def __make_unique_server_name(self, base_name: str) -> str:
        raw = (base_name or "").strip() or "Emby"
        if raw not in self._server_profiles:
            return raw
        idx = 2
        while f"{raw}_{idx}" in self._server_profiles:
            idx += 1
        return f"{raw}_{idx}"

    def __upsert_active_server_profile(self, config: dict) -> bool:
        cfg = config or {}
        selected_name = str(cfg.get("active_server_name", self._active_server_name or "")).strip()
        edit_target = str(cfg.get("active_server_edit_target", self._active_server_name or "")).strip()
        host = str(cfg.get("active_server_host", "")).strip()
        api_key = str(cfg.get("active_server_api_key", "")).strip()
        style = str(cfg.get("active_server_style", self._active_server_style or "static_1")).strip() or "static_1"
        if not selected_name:
            return False
        if selected_name != "__new__" and selected_name not in self._server_profiles:
            return False
        dirty = False
        if edit_target and edit_target in self._server_profiles and host and api_key:
            old_edit_profile = self._server_profiles.get(edit_target)
            edit_style = style if selected_name in {edit_target, "__new__"} else str(old_edit_profile.get("style", "static_1"))
            new_edit_profile = self.__profile_from_runtime(
                name=edit_target,
                host=host,
                api_key=api_key,
                style=edit_style,
            )
            if old_edit_profile != new_edit_profile:
                self._server_profiles[edit_target] = new_edit_profile
                dirty = True
        if selected_name != "__new__":
            if self._active_server_name != selected_name:
                dirty = True
            self._active_server_name = selected_name
            self.__sync_active_server_editor()
            return dirty
        if not host or not api_key:
            self._active_server_name = selected_name
            self._active_server_style = "static_2" if style == "static_2" else "static_1"
            return dirty
        target_name = selected_name
        if selected_name == "__new__":
            fetched_name = self.__fetch_emby_server_name(host=host, api_key=api_key) or "Emby"
            target_name = self.__make_unique_server_name(fetched_name)
        old_profile = self._server_profiles.get(target_name)
        profile = self.__profile_from_runtime(
            name=target_name,
            host=host,
            api_key=api_key,
            style=style,
        )
        self._server_profiles[target_name] = profile
        self._active_server_name = target_name
        self._active_server_host = profile.get("host", "")
        self._active_server_api_key = profile.get("api_key", "")
        self._active_server_style = profile.get("style", "static_1")
        return dirty or (old_profile != profile)

    def __sync_active_server_editor(self):
        if self._active_server_name == "__new__":
            self._active_server_host = ""
            self._active_server_api_key = ""
            self._active_server_style = "static_1"
            return
        if self._server_profiles and (not self._active_server_name or self._active_server_name not in self._server_profiles):
            self._active_server_name = sorted(self._server_profiles.keys())[0]
        if not self._active_server_name or self._active_server_name not in self._server_profiles:
            self._active_server_name = "__new__"
            self._active_server_host = ""
            self._active_server_api_key = ""
            self._active_server_style = "static_1"
            return
        profile = self._server_profiles.get(self._active_server_name) or {}
        self._active_server_host = str(profile.get("host", "")).strip()
        self._active_server_api_key = str(profile.get("api_key", "")).strip()
        self._active_server_style = "static_2" if str(profile.get("style", "static_1")) == "static_2" else "static_1"
        self._active_server_edit_target = self._active_server_name
        self.__apply_server_profile_values(profile)

    def __apply_server_profile(self, server_name: str):
        profile = self._server_profiles.get(server_name) or {}
        if not profile:
            self._cover_style = self._server_style_map.get(server_name, "static_1")
            return
        self.__apply_server_profile_values(profile)

    def __sync_profile_styles_with_selected_style(self) -> bool:
        target_style = "static_2" if str(self._cover_style_base or "static_1") == "static_2" else "static_1"
        dirty = False
        if isinstance(self._server_profiles, dict):
            for name, profile in list(self._server_profiles.items()):
                normalized = dict(profile or {})
                if str(normalized.get("style", "")).strip() != target_style:
                    normalized["style"] = target_style
                    self._server_profiles[name] = normalized
                    dirty = True
        self._active_server_style = target_style
        self._cover_style = target_style
        self._cover_style_base = target_style
        return dirty

    def __compose_cover_style(self, base_style: str, variant: str) -> str:
        mapping = {
            "static_1": "static_1",
            "static_2": "static_2",
        }
        return mapping.get(base_style, "static_1")

    def __resolve_cover_style_ui(self, cover_style: str) -> Tuple[str, str]:
        mapping = {
            "static_1": "static_1",
            "static_2": "static_2",
        }
        return mapping.get(cover_style, "static_1"), "static"

    def __is_single_image_style(self) -> bool:
        return False

    def __get_required_items(self) -> int:
        if self._cover_style == "static_1":
            return 9
        if self._cover_style == "static_2":
            return 6
        return 5

    def __get_fetch_target_count(self) -> int:
        required_items = self.__get_required_items()
        if self._cover_style == "static_2":
            return required_items + 1
        return required_items

    def __update_config(self):
        """
        ??????
        """
        self._cover_style = self.__compose_cover_style(self._cover_style_base, "static")
        self.__sync_profile_styles_with_selected_style()
        self.update_config({
            "enabled": self._enabled,
            "update_now": self._update_now,
            "transfer_monitor": self._transfer_monitor,
            "cron": self._cron,
            "delay": self._delay,
            "selected_servers": [],
            "servers_config": self._servers_config,
            "server_profiles": self._server_profiles,
            "active_server_name": self._active_server_name,
            "active_server_edit_target": self._active_server_name,
            "active_server_host": self._active_server_host,
            "active_server_api_key": self._active_server_api_key,
            "active_server_style": self._active_server_style,
            "server_1_name": self.__manual_server_slot_value(1, "name", ""),
            "server_1_host": self.__manual_server_slot_value(1, "host", ""),
            "server_1_api_key": self.__manual_server_slot_value(1, "api_key", ""),
            "server_1_style": self.__manual_server_slot_value(1, "style", "static_1"),
            "server_2_name": self.__manual_server_slot_value(2, "name", ""),
            "server_2_host": self.__manual_server_slot_value(2, "host", ""),
            "server_2_api_key": self.__manual_server_slot_value(2, "api_key", ""),
            "server_2_style": self.__manual_server_slot_value(2, "style", "static_1"),
            "server_3_name": self.__manual_server_slot_value(3, "name", ""),
            "server_3_host": self.__manual_server_slot_value(3, "host", ""),
            "server_3_api_key": self.__manual_server_slot_value(3, "api_key", ""),
            "server_3_style": self.__manual_server_slot_value(3, "style", "static_1"),
            "server_4_name": self.__manual_server_slot_value(4, "name", ""),
            "server_4_host": self.__manual_server_slot_value(4, "host", ""),
            "server_4_api_key": self.__manual_server_slot_value(4, "api_key", ""),
            "server_4_style": self.__manual_server_slot_value(4, "style", "static_1"),
            "server_5_name": self.__manual_server_slot_value(5, "name", ""),
            "server_5_host": self.__manual_server_slot_value(5, "host", ""),
            "server_5_api_key": self.__manual_server_slot_value(5, "api_key", ""),
            "server_5_style": self.__manual_server_slot_value(5, "style", "static_1"),
            "include_libraries": self._include_libraries,
            "selected_libraries": self._selected_libraries,
            "selected_library": self._selected_libraries[0] if self._selected_libraries else "",
            "all_libraries": self._all_libraries,
            "sort_by": self._sort_by,
            "covers_output": self._covers_output,
            "covers_input": self._covers_input,
            "title_config": self._title_config or self.__default_title_config_template(),
            "zh_font_url": str(self._zh_font_url),
            "en_font_url": str(self._en_font_url),
            "zh_font_path": str(self._zh_font_path),
            "en_font_path": str(self._en_font_path),
            "cover_style": self._cover_style,
            "cover_style_base": self._cover_style_base,
            "multi_1_blur": self._multi_1_blur,
            "zh_font_size": self._zh_font_size,
            "en_font_size": self._en_font_size,
            "blur_size": self._blur_size,
            "color_ratio": self._color_ratio,
            "use_primary": self._use_primary,
            "zh_font_custom": self._zh_font_custom,
            "en_font_custom": self._en_font_custom,
            "zh_font_preset": self._zh_font_preset,
            "en_font_preset": self._en_font_preset,
            "zh_font_offset": self._zh_font_offset,
            "title_spacing": self._title_spacing,
            "en_line_spacing": self._en_line_spacing,
            "title_scale": self._title_scale,
            "resolution": self._resolution,
            "custom_width": self._custom_width,
            "custom_height": self._custom_height,
            "bg_color_mode": self._bg_color_mode,
            "custom_bg_color": self._custom_bg_color,
            "clean_images": self._clean_images,
            "clean_fonts": self._clean_fonts,
            "save_recent_covers": self._save_recent_covers,
            "debug_mode": bool(self._debug_mode),
            "debug_show_apikey": bool(self._debug_mode),
            "covers_history_limit_per_library": self._covers_history_limit_per_library,
            "covers_page_history_limit": self._covers_page_history_limit,
            "style_naming_v2": True,
        })

    def get_state(self) -> bool:
        return self._enabled

    def __font_search_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        if self._font_path:
            dirs.append(Path(self._font_path))
        repo_font_dir = Path(__file__).resolve().parents[2] / "fonts"
        dirs.append(repo_font_dir)
        unique_dirs: List[Path] = []
        seen = set()
        for directory in dirs:
            key = str(directory)
            if key in seen:
                continue
            seen.add(key)
            if directory.exists() and directory.is_dir():
                unique_dirs.append(directory)
        return unique_dirs

    def __find_font_file(self, aliases: List[str], exts: List[str]) -> Optional[str]:
        normalized_aliases = [item.lower() for item in aliases if item]
        normalized_aliases_compact = [re.sub(r'[\s_\-]+', '', item) for item in normalized_aliases]
        normalized_exts = [item.lower() for item in exts]
        for directory in self.__font_search_dirs():
            candidates = sorted(directory.iterdir(), key=lambda p: p.name.lower())
            for font_file in candidates:
                if not font_file.is_file():
                    continue
                suffix = font_file.suffix.lower()
                if suffix not in normalized_exts:
                    continue
                stem = font_file.stem.lower()
                name = font_file.name.lower()
                stem_compact = re.sub(r'[\s_\-]+', '', stem)
                name_compact = re.sub(r'[\s_\-]+', '', name)
                if any(
                    alias in stem or alias in name or compact in stem_compact or compact in name_compact
                    for alias, compact in zip(normalized_aliases, normalized_aliases_compact)
                ):
                    return str(font_file)
        return None

    def __get_font_presets(self) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, Optional[str]], Dict[str, Optional[str]]]:
        zh_specs = [
            {"title": "潮黑", "value": "chaohei", "aliases": ["chaohei", "wendao", "潮黑", "chao_hei"]},
            {"title": "粗雅宋", "value": "yasong", "aliases": ["yasong", "粗雅宋", "multi_1_zh", "ya_song"]},
        ]
        en_specs = [
            {"title": "EmblemaOne", "value": "EmblemaOne", "aliases": ["emblemaone", "emblema_one"]},
            {"title": "Melete", "value": "Melete", "aliases": ["melete", "multi_1_en"]},
            {"title": "Phosphate", "value": "Phosphate", "aliases": ["phosphate", "phosphat"]},
            {"title": "JosefinSans", "value": "JosefinSans", "aliases": ["josefinsans", "josefin_sans"]},
            {"title": "LilitaOne", "value": "LilitaOne", "aliases": ["lilitaone", "lilita_one"]},
            {"title": "Monoton", "value": "Monoton", "aliases": ["monoton"]},
            {"title": "Plaster", "value": "Plaster", "aliases": ["plaster"]},
        ]
        all_specs = []
        seen_values = set()
        for spec in zh_specs + en_specs:
            if spec["value"] in seen_values:
                continue
            seen_values.add(spec["value"])
            value_alias = spec["value"].lower()
            compact_value_alias = re.sub(r'[\s_\-]+', '', value_alias)
            if value_alias not in spec["aliases"]:
                spec["aliases"].append(value_alias)
            if compact_value_alias and compact_value_alias not in spec["aliases"]:
                spec["aliases"].append(compact_value_alias)
            title_alias = spec["title"].lower()
            compact_title_alias = re.sub(r'[\s_\-]+', '', title_alias)
            if title_alias not in spec["aliases"]:
                spec["aliases"].append(title_alias)
            if compact_title_alias and compact_title_alias not in spec["aliases"]:
                spec["aliases"].append(compact_title_alias)
            all_specs.append(spec)
        zh_paths: Dict[str, Optional[str]] = {}
        en_paths: Dict[str, Optional[str]] = {}
        zh_items: List[Dict[str, str]] = []
        en_items: List[Dict[str, str]] = []
        zh_exts = [".ttf", ".otf", ".woff2", ".woff"]
        en_exts = [".ttf", ".otf", ".woff2", ".woff"]

        for spec in all_specs:
            found = self.__find_font_file(spec["aliases"], zh_exts)
            zh_paths[spec["value"]] = found
            zh_items.append({"title": spec["title"], "value": spec["value"]})
        for spec in all_specs:
            found = self.__find_font_file(spec["aliases"], en_exts)
            en_paths[spec["value"]] = found
            en_items.append({"title": spec["title"], "value": spec["value"]})
        return zh_items, en_items, zh_paths, en_paths

    def __clean_generated_images(self):
        removed = 0
        cache_dirs: List[Path] = []
        if self._covers_path:
            cache_dirs.append(Path(self._covers_path))
        data_path = self.get_data_path()
        legacy_covers_dir = data_path / "covers"
        cache_dirs.append(legacy_covers_dir)

        handled = set()
        for cache_dir in cache_dirs:
            if not cache_dir.exists() or not cache_dir.is_dir():
                continue
            cache_key = str(cache_dir.resolve())
            if cache_key in handled:
                continue
            handled.add(cache_key)
            for entry in cache_dir.iterdir():
                if not entry.exists():
                    continue
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                        removed += 1
                    elif entry.is_file():
                        entry.unlink(missing_ok=True)
                        removed += 1
                except Exception as e:
                    logger.warning(f"清理图片失败 {entry}: {e}")
        logger.info(f"清理图片完成（含旧版 covers 兼容目录），共清理 {removed} 项")

    def __clean_downloaded_fonts(self):
        if not self._font_path or not Path(self._font_path).exists():
            logger.info("清理字体：未找到字体目录，跳过")
            return
        removed = 0
        for entry in Path(self._font_path).iterdir():
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file():
                    entry.unlink(missing_ok=True)
                    removed += 1
                elif entry.is_dir():
                    shutil.rmtree(entry)
                    removed += 1
            except Exception as e:
                logger.warning(f"清理字体失败 {entry}: {e}")
        self._zh_font_path = ""
        self._en_font_path = ""
        logger.info(f"清理字体完成，共清理 {removed} 项")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/update_covers",
                "event": EventType.PluginAction,
                "desc": "更新媒体库封面",
                "category": "",
                "data": {"action": "update_covers"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/clean_cache",
                "endpoint": self.api_clean_cache,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理全部缓存（图片+字体）",
            },
            {
                "path": "clean_cache",
                "endpoint": self.api_clean_cache,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理全部缓存（图片+字体，兼容无前导斜杠）",
            },
            {
                "path": "/generate_now",
                "endpoint": self.api_generate_now,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "立即生成媒体库封面",
            },
            {
                "path": "generate_now",
                "endpoint": self.api_generate_now,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "立即生成媒体库封面(兼容无前导斜杠)",
            },
        ]

    def api_clean_cache(self):
        try:
            logger.info("【WsEmbyCover】收到立即清理全部缓存请求（图片+字体）")
            self.__clean_generated_images()
            self.__clean_downloaded_fonts()
            self._clean_images = False
            self._clean_fonts = False
            self.__update_config()
            return {"code": 0, "msg": "缓存清理完成（图片+字体）"}
        except Exception as e:
            logger.error(f"【WsEmbyCover】立即清理全部缓存失败: {e}", exc_info=True)
            return {"code": 1, "msg": f"缓存清理失败: {e}"}

    def api_generate_now(self, style: str = ""):
        old_style = self._cover_style
        try:
            if not self._enabled:
                logger.warning("【WsEmbyCover】立即生成失败：插件未启用，请先在设置页启用插件并保存")
                return {"code": 1, "msg": "插件未启用，请先在设置页启用插件并保存"}
            if not self._servers:
                logger.warning("【WsEmbyCover】立即生成失败：未配置媒体服务器，请先在设置页填写并保存")
                return {"code": 1, "msg": "未配置媒体服务器，请先在设置页填写并保存"}

            target_style = (style or "").strip()
            allowed_styles = {
                "static_1", "static_2",
            }
            if target_style:
                if target_style not in allowed_styles:
                    return {"code": 1, "msg": f"不支持的风格: {target_style}"}
                self._cover_style = target_style
            logger.info(f"【WsEmbyCover】收到立即生成请求，风格: {self._cover_style}")
            tips = self.__update_all_libraries()
            return {"code": 0, "msg": tips or "封面生成任务已完成"}
        except Exception as e:
            logger.error(f"【WsEmbyCover】立即生成失败: {e}", exc_info=True)
            return {"code": 1, "msg": f"封面生成失败: {e}"}
        finally:
            self._cover_style = old_style

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        services = []
        if self._enabled and self._cron:
            services.append({
                "id": "WsEmbyCover",
                "name": "媒体库封面更新服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__update_all_libraries,
                "kwargs": {}
            })
        
        # 总是显示停止按钮，以便中断长时间运行的任务
        services.append({
            "id": "StopWsEmbyCover",
            "name": "停止当前更新任务",
            "trigger": None,
            "func": self.stop_task,
            "kwargs": {}
        })
        return services

    def stop_task(self):
        """
        手动停止当前正在执行的任务
        """
        if not self._event.is_set():
            logger.info("正在发送停止任务信号...")
            self._event.set()
            return True, "已发送停止停止信号，请等待当前操作清理完成"
        return True, "任务已处于停止状态或正在停止中"

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        zh_font_items, en_font_items, _, _ = self.__get_font_presets()
        server_tab = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VAceEditor',
                                'props': {
                                    'modelvalue': 'servers_config',
                                    'lang': 'yaml',
                                    'theme': 'monokai',
                                    'style': 'height: 18rem',
                                    'label': '多服务器配置',
                                    'placeholder': self.__default_servers_config_template()
                                 }
                             }
                         ]
                     },
                ]
            },
        ]

        # 标题配置
        title_tab = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VAceEditor',
                                'props': {
                                    'modelvalue': 'title_config',
                                    'lang': 'yaml',
                                    'theme': 'monokai',
                                    'style': 'height: 30rem',
                                    'label': '中英标题配置',
                                    'placeholder': '''服务器1:
  动画电影:
    - 动画电影
    - ANI MOVIE
  华语电影:
    - 华语电影
    - CHN MOVIE'''
                                 }
                             }
                         ]
                     },
                ]
            },
        ]

        # 其他设置标签
        others_tab = [
            
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '自定义图片目录：请将图片存于与媒体库同名的子目录下，例如：/mnt/custom_images/华语电影/1.jpg，填写 /mnt/custom_images 即可。多图模式下，文件名须为 1.jpg, 2.jpg, ...9.jpg，不满足的会被重命名，不够的会随机复制填满9张'
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'covers_input',
                                    'label': '自定义图片目录（可选）',
                                    'prependInnerIcon': 'mdi-file-image',
                                    'hint': '使用你指定的图片生成封面，图片放在与媒体库同名的文件夹下',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },

                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'covers_output',
                                    'label': '历史封面保存目录（可选）',
                                    'prependInnerIcon': 'mdi-file-image',
                                    'hint': '生成的封面默认保存在本插件数据目录下',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                                        {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'save_recent_covers',
                                    'label': '保存最近生成的封面',
                                    'hint': '默认开启，保存历史封面',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'covers_history_limit_per_library',
                                    'label': '媒体库历史封面数量',
                                    'prependInnerIcon': 'mdi-history',
                                    'hint': '单个媒体库封面保留上限，默认 10',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'covers_page_history_limit',
                                    'label': '历史封面显示数量',
                                    'prependInnerIcon': 'mdi-image-multiple-outline',
                                    'hint': '历史封面「显示数量」，默认 50',
                                    'persistentHint': True
                                },
                            }
                        ]
                    }
                ]
            },
            
        ]
        # 更多参数标签
        single_tab = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '字体设置为可选项。若字体无法下载，可以手动下载并填写本地路径。主标题和副标题可以使用不同的字体。'
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VSelect',
                                'props': {
                                    'chips': False,
                                    'multiple': False,
                                    'model': 'zh_font_preset',
                                    'label': '主标题字体预设',
                                    'prependInnerIcon': 'mdi-ideogram-cjk',
                                    'items': zh_font_items
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VSelect',
                                'props': {
                                    'chips': False,
                                    'multiple': False,
                                    'model': 'en_font_preset',
                                    'label': '副标题字体预设',
                                    'prependInnerIcon': 'mdi-format-font',
                                    'items': en_font_items
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'zh_font_custom',
                                    'label': '自定义主标题字体',
                                    'prependInnerIcon': 'mdi-ideogram-cjk',
                                    'placeholder': '留空使用预设字体',
                                    'hint': '字体链接 / 路径',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'en_font_custom',
                                    'label': '自定义副标题字体',
                                    'prependInnerIcon': 'mdi-format-font',
                                    'placeholder': '留空使用预设字体',
                                    'hint': '字体链接 / 路径',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'zh_font_size',
                                    'label': '主标题字体大小',
                                    'prependInnerIcon': 'mdi-format-size',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '根据自己喜好设置，默认 180',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'en_font_size',
                                    'label': '副标题字体大小',
                                    'prependInnerIcon': 'mdi-format-size',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '根据自己喜好设置，默认 75',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'blur_size',
                                    'label': '背景模糊尺寸',
                                    'prependInnerIcon': 'mdi-blur',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '数字越大越模糊，默认 50',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'color_ratio',
                                    'label': '背景颜色混合占比',
                                    'prependInnerIcon': 'mdi-format-color-fill',
                                    'placeholder': '留空使用预设占比',
                                    'hint': '颜色所占的比例，0-1，默认 0.8',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'title_scale',
                                    'label': '标题整体缩放',
                                    'prependInnerIcon': 'mdi-arrow-expand-all',
                                    'placeholder': '留空使用预设比例',
                                    'hint': '以 1080p 为基准，1.0 为默认',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'zh_font_offset',
                                    'label': '主标题偏移量',
                                    'prependInnerIcon': 'mdi-arrow-up-down',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '上移为负值，下移为正值',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'title_spacing',
                                    'label': '主副标题间距',
                                    'prependInnerIcon': 'mdi-arrow-up-down',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '大于 0，默认 40',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'en_line_spacing',
                                    'label': '副标题行间距',
                                    'prependInnerIcon': 'mdi-format-line-height',
                                    'placeholder': '留空使用预设尺寸',
                                    'hint': '大于 0，默认 40',
                                    'persistentHint': True
                                }
                            }
                        ]
                    },
                ]
            },
        ]

        more_tab = single_tab + others_tab

        styles = [
            {
                "value": "static_1",
                "src": self.__style_preview_src(1)
            },
            {
                "value": "static_2",
                "src": self.__style_preview_src(2)
            },
        ]
        library_items = self._all_libraries or []
        profile_defaults: Dict[str, Any] = {}
        server_profile_panels: List[Dict[str, Any]] = []
        profile_entries = list(sorted(self._server_profiles.items(), key=lambda item: item[0]))
        profile_entries.append(("__new__", self.__profile_from_runtime(name="", host="", api_key="", style="static_1")))
        for idx, (profile_name, profile) in enumerate(profile_entries, start=1):
            key = f"profile_{idx}"
            profile_defaults[f"{key}_name"] = "" if profile_name == "__new__" else profile_name
            profile_defaults[f"{key}_host"] = "" if profile_name == "__new__" else str(profile.get("host", "") or "")
            profile_defaults[f"{key}_api_key"] = "" if profile_name == "__new__" else str(profile.get("api_key", "") or "")
            profile_defaults[f"{key}_style"] = "static_2" if str(profile.get("style", "static_1")) == "static_2" else "static_1"
            profile_defaults[f"{key}_title_config"] = str(profile.get("title_config", self.__default_title_config_template()) or self.__default_title_config_template())
            profile_defaults[f"{key}_sort_by"] = str(profile.get("sort_by", "Random") or "Random")
            profile_defaults[f"{key}_covers_input"] = str(profile.get("covers_input", "") or "")
            profile_defaults[f"{key}_covers_output"] = str(profile.get("covers_output", "") or "")
            profile_defaults[f"{key}_use_primary"] = bool(profile.get("use_primary", self._use_primary))
            profile_defaults[f"{key}_multi_1_blur"] = bool(profile.get("multi_1_blur", self._multi_1_blur))
            profile_defaults[f"{key}_resolution"] = str(profile.get("resolution", "480p") or "480p")
            panel_title = "新增服务器" if profile_name == "__new__" else f"服务器：{profile_name}"
            server_profile_panels.append(
                {
                    "component": "VExpansionPanel",
                    "props": {"elevation": 0, "class": "rounded-lg"},
                    "content": [
                        {"component": "VExpansionPanelTitle", "text": panel_title},
                        {
                            "component": "VExpansionPanelText",
                            "content": [
                                {
                                    "component": "VRow",
                                    "content": [
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": f"{key}_name", "label": "服务器名称"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_host", "label": "服务器地址"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": f"{key}_api_key", "label": "API Key"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSelect", "props": {"model": f"{key}_style", "label": "风格", "items": [{"title": "style1", "value": "static_1"}, {"title": "style2", "value": "static_2"}]}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": f"{key}_sort_by", "label": "封面来源排序", "items": [{"title": "随机", "value": "Random"}, {"title": "最新入库", "value": "DateCreated"}, {"title": "最新发行", "value": "PremiereDate"}]}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_covers_input", "label": "自定义图片目录（可选）"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_covers_output", "label": "历史封面保存目录（可选）"}}]},
                                    ],
                                },
                                {
                                    "component": "VAceEditor",
                                    "props": {
                                        "modelvalue": f"{key}_title_config",
                                        "lang": "yaml",
                                        "theme": "monokai",
                                        "style": "height: 16rem",
                                        "label": "封面标题（该服务器独立）",
                                    },
                                },
                            ],
                        },
                    ],
                }
            )

        style_variant_items = [
            {
                'component': 'VBtn',
                'props': {
                    'value': 'static',
                    'variant': 'outlined',
                    'color': 'primary',
                    'prependIcon': 'mdi-image-outline',
                    'class': 'text-none',
                },
                'text': '静态'
            },
            {
                'component': 'VBtn',
                'props': {
                    'value': 'animated',
                    'variant': 'outlined',
                    'color': 'primary',
                    'prependIcon': 'mdi-play-box-multiple-outline',
                    'class': 'text-none',
                },
                'text': '动态'
            }
        ]

        preview_style_content = []

        for style in styles:
            preview_style_content.append(
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 12,
                        'md': 4,
                    },
                    'content': [
                        {
                            'component': 'VLabel',
                            'props': {
                                'class': 'd-block w-100 cursor-pointer'
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'flat',
                                        'class': 'rounded-lg overflow-hidden',
                                        'style': f'position: relative; background-image: linear-gradient(rgba(80,80,80,0.25), rgba(80,80,80,0.25)), url({style.get("src")}); background-size: cover; background-position: center; background-repeat: no-repeat;'
                                    },
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': style.get('src'),
                                                'aspect-ratio': '16/9',
                                                'cover': True,
                                            }
                                        },
                                        {
                                            'component': 'VRadio',
                                            'props': {
                                                'value': style.get('value'),
                                                'color': '#FFFFFF',
                                                'baseColor': '#FFFFFF',
                                                'density': 'default',
                                                'hideDetails': True,
                                                'class': 'position-absolute',
                                                'style': 'top: 8px; right: 8px; z-index: 2; margin: 0; transform: scale(1.2); transform-origin: top right;'
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        # 封面风格设置标签
        style_tab = [
            {
                'component': 'VRadioGroup',
                'props': {
                    'model': 'cover_style_base',
                },
                'content': [
                    {
                        'component': 'VRow',
                        'content': preview_style_content
                    }
                ]
            },
            {
                'component': 'VExpansionPanels',
                'props': {
                    'multiple': True,
                    'class': 'mt-2'
                },
                'content': [
                    {
                        'component': 'VExpansionPanel',
                        'props': {
                            'elevation': 0,
                            'class': 'rounded-lg',
                            'style': 'background-color: rgba(var(--v-theme-surface), 0.38); border: 1px solid rgba(var(--v-border-color), 0.35); backdrop-filter: blur(6px);'
                        },
                        'content': [
                            {
                                'component': 'VExpansionPanelTitle',
                                'props': {
                                    'class': 'font-weight-medium'
                                },
                                'text': '基本参数'
                            },
                            {
                                'component': 'VExpansionPanelText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {
                                                        'component': 'VBtnToggle',
                                                        'props': {
                                                            'model': 'use_primary',
                                                            'mandatory': True,
                                                            'divided': True,
                                                            'class': 'w-100'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': True,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '海报图'
                                                            },
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': False,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '背景图'
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VLabel',
                                                        'props': {
                                                            'class': 'text-caption text-medium-emphasis mt-1 d-inline-block'
                                                        }
                                                        ,
                                                        'text': '选图优先来源'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {
                                                        'component': 'VBtnToggle',
                                                        'props': {
                                                            'model': 'multi_1_blur',
                                                            'mandatory': True,
                                                            'divided': True,
                                                            'class': 'w-100'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': True,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '模糊背景'
                                                            },
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': False,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '纯色渐变'
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VLabel',
                                                        'props': {
                                                            'class': 'text-caption text-medium-emphasis mt-1 d-inline-block'
                                                        }
                                                        ,
                                                        'text': '针对九宫格海报'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'chips': False,
                                                            'multiple': False,
                                                            'model': 'resolution',
                                                            'label': '静态分辨率',
                                                            'prependInnerIcon': 'mdi-monitor-screenshot',
                                                            'items': [
                                                                {'title': '1080p (1920x1080)', 'value': '1080p'},
                                                                {'title': '720p (1280x720)', 'value': '720p'},
                                                                {'title': '480p (854x480)', 'value': '480p'}
                                                            ],
                                                            'hint': '动态分辨率默认320*180',
                                                            'persistentHint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VExpansionPanels',
                                        'props': {
                                            'multiple': True,
                                            'class': 'mt-2'
                                        },
                                        'content': [
                                            {
                                                'component': 'VExpansionPanel',
                                                'props': {
                                                    'elevation': 0,
                                                    'class': 'rounded-lg',
                                                    'style': 'background-color: rgba(255,255,255,0.55); border: 1px dashed rgba(0,0,0,0.18);'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VExpansionPanelTitle',
                                                        'text': '背景颜色设置（全部风格生效）'
                                                    },
                                                    {
                                                        'component': 'VExpansionPanelText',
                                                        'content': [
                                                            {
                                                                'component': 'VRow',
                                                                'content': [
                                                                    {
                                                                        'component': 'VCol',
                                                                        'props': {'cols': 12, 'md': 4},
                                                                        'content': [
                                                                            {
                                                                                'component': 'VSelect',
                                                                                'props': {
                                                                                    'model': 'bg_color_mode',
                                                                                    'label': '背景颜色来源',
                                                                                    'prependInnerIcon': 'mdi-palette',
                                                                                    'items': [
                                                                                        {'title': '自动从图片提取', 'value': 'auto'},
                                                                                        {'title': '自定义（全局统一）', 'value': 'custom'},
                                                                                        {'title': '从配置获取', 'value': 'config'}
                                                                                    ]
                                                                                }
                                                                            }
                                                                        ]
                                                                    },
                                                                    {
                                                                        'component': 'VCol',
                                                                        'props': {'cols': 12, 'md': 8},
                                                                        'content': [
                                                                            {
                                                                                'component': 'VTextField',
                                                                                'props': {
                                                                                    'model': 'custom_bg_color',
                                                                                    'label': '自定义背景色',
                                                                                    'prependInnerIcon': 'mdi-eyedropper',
                                                                                    'placeholder': '#FF5722',
                                                                                    'hint': '支持 #十六进制、rgb(...)、颜色英文名',
                                                                                    'persistentHint': True
                                                                                }
                                                                            },
                                                                            {
                                                                                'component': 'VColorPicker',
                                                                                'props': {
                                                                                    'model': 'custom_bg_color',
                                                                                    'mode': 'hexa',
                                                                                    'showSwatches': True,
                                                                                    'hideCanvas': False,
                                                                                    'hideInputs': True,
                                                                                    'elevation': 0,
                                                                                    'class': 'mt-2'
                                                                                }
                                                                            }
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ]


        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {
                                "component": "VIcon",
                                "props": {
                                    "icon": "mdi-cog",
                                    "color": "primary",
                                    "class": "mr-2",
                                },
                            },
                            {"component": "span", "text": "基础设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                'component': 'VForm',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'enabled',
                                                            'label': '启用插件',
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'update_now',
                                                            'label': '立即更新封面',
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'transfer_monitor',
                                                            'label': '入库监控',
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'debug_mode',
                                                            'label': '调试模式',
                                                            'color': 'warning',
                                                        }
                                                    }
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'delay',
                                                            'label': '入库延迟（秒）',
                                                            'placeholder': '60',
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCronField',
                                                        'props': {
                                                            'model': 'cron',
                                                            'label': '定时更新封面',
                                                            'placeholder': '5位cron表达式'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'chips': False,
                                                            'multiple': False,
                                                            'model': 'sort_by',
                                                            'label': '封面来源排序，默认随机',
                                                            'items': [
                                                                {"title": "随机", "value": "Random"},
                                                                {"title": "最新入库", "value": "DateCreated"},
                                                                {"title": "最新发行", "value": "PremiereDate"}
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'chips': False,
                                                            'multiple': True,
                                                            'clearable': True,
                                                            'model': 'selected_libraries',
                                                            'label': '指定媒体库',
                                                            'items': library_items,
                                                            'counter': True,
                                                        }
                                                    }
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VBtn',
                                                        'props': {
                                                            'color': 'error',
                                                            'variant': 'flat',
                                                            'prepend-icon': 'mdi-broom',
                                                            'class': 'text-none',
                                                        },
                                                        'text': '立即清理缓存（图片+字体）',
                                                        'events': {
                                                            'click': {
                                                                'api': 'plugin/WsEmbyCover/clean_cache',
                                                                'method': 'post',
                                                            }
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    
                                ]
                            },
                        ]
                    }
                ]
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {"model": "tab", "grow": True, "color": "primary"},
                        "content": [
                            {
                                "component": "VTab",
                                "props": {"value": "style-tab"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-palette-swatch",
                                            "start": True,
                                            "color": "#cc76d1",
                                        },
                                    },
                                    {"component": "span", "text": "封面风格"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "server-tab"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-server-network",
                                            "start": True,
                                            "color": "#26A69A",
                                        },
                                    },
                                    {"component": "span", "text": "多服务器"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "title-tab"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-text-box-edit",
                                            "start": True,
                                            "color": "#1976D2",
                                        },
                                    },
                                    {"component": "span", "text": "封面标题"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "more-tab"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-palette-swatch-variant",
                                            "start": True,
                                            "color": "#f3afe4",
                                        },
                                    },
                                    {"component": "span", "text": "更多参数"},
                                ],
                            },
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VWindow",
                        "props": {"model": "tab"},
                        "content": [
                            {
                                "component": "VWindowItem",
                                "props": {"value": "style-tab"},
                                "content": [
                                    {"component": "VCardText", "content": style_tab}
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "server-tab"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": server_tab,
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "title-tab"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": title_tab,
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "more-tab"},
                                "content": [
                                    {"component": "VCardText", "content": more_tab}
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": True,
            "update_now": False,
            "transfer_monitor": True,
            "cron": "",
            "delay": 60,
            "selected_servers": [],
            "servers_config": self._servers_config or self.__default_servers_config_template(),
            "server_profiles": self._server_profiles,
            "active_server_name": self._active_server_name or "__new__",
            "active_server_edit_target": self._active_server_edit_target or self._active_server_name or "",
            "active_server_host": self._active_server_host,
            "active_server_api_key": self._active_server_api_key,
            "active_server_style": self._active_server_style or "static_1",
            "server_1_name": self.__manual_server_slot_value(1, "name", ""),
            "server_1_host": self.__manual_server_slot_value(1, "host", ""),
            "server_1_api_key": self.__manual_server_slot_value(1, "api_key", ""),
            "server_1_style": self.__manual_server_slot_value(1, "style", "static_1"),
            "server_2_name": self.__manual_server_slot_value(2, "name", ""),
            "server_2_host": self.__manual_server_slot_value(2, "host", ""),
            "server_2_api_key": self.__manual_server_slot_value(2, "api_key", ""),
            "server_2_style": self.__manual_server_slot_value(2, "style", "static_1"),
            "server_3_name": self.__manual_server_slot_value(3, "name", ""),
            "server_3_host": self.__manual_server_slot_value(3, "host", ""),
            "server_3_api_key": self.__manual_server_slot_value(3, "api_key", ""),
            "server_3_style": self.__manual_server_slot_value(3, "style", "static_1"),
            "server_4_name": self.__manual_server_slot_value(4, "name", ""),
            "server_4_host": self.__manual_server_slot_value(4, "host", ""),
            "server_4_api_key": self.__manual_server_slot_value(4, "api_key", ""),
            "server_4_style": self.__manual_server_slot_value(4, "style", "static_1"),
            "server_5_name": self.__manual_server_slot_value(5, "name", ""),
            "server_5_host": self.__manual_server_slot_value(5, "host", ""),
            "server_5_api_key": self.__manual_server_slot_value(5, "api_key", ""),
            "server_5_style": self.__manual_server_slot_value(5, "style", "static_1"),
            "include_libraries": self._include_libraries or [],
            "selected_libraries": self._selected_libraries or [],
            "sort_by": self._sort_by or "Random",
            "title_config": self._title_config or self.__default_title_config_template(),
            "tab": "title-tab",
            "cover_style": self._cover_style or "static_1",
            "cover_style_base": self._cover_style_base or "static_1",
            "multi_1_blur": self._multi_1_blur,
            "zh_font_preset": self._zh_font_preset or "chaohei",
            "en_font_preset": self._en_font_preset or "EmblemaOne",
            "zh_font_custom": self._zh_font_custom or "",
            "en_font_custom": self._en_font_custom or "",
            "zh_font_size": self._zh_font_size,
            "en_font_size": self._en_font_size,
            "blur_size": self._blur_size,
            "color_ratio": self._color_ratio,
            "title_scale": self._title_scale,
            "use_primary": self._use_primary,
            "resolution": self._resolution or "480p",
            "custom_width": self._custom_width,
            "custom_height": self._custom_height,
            "bg_color_mode": self._bg_color_mode or "auto",
            "custom_bg_color": self._custom_bg_color or "",
            "save_recent_covers": self._save_recent_covers,
            "debug_mode": bool(self._debug_mode),
            "debug_show_apikey": bool(self._debug_mode),
            "covers_history_limit_per_library": self._covers_history_limit_per_library,
            "covers_page_history_limit": self._covers_page_history_limit,
            "style_naming_v2": True,
            **profile_defaults,
        }

    def get_page(self) -> List[dict]:
        # 保留最小入口以满足插件框架要求；详情页功能代码已移除
        return []

    @staticmethod
    def __style_preview_src(index: int) -> str:

        safe_index = max(1, min(2, int(index)))
        preview_map = {
            1: "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/images/style_3.jpeg?v=20260407-130",
            2: "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/images/style_5_preview.jpg?v=20260407-248",
        }
        return preview_map.get(safe_index, preview_map[1])

    def __get_recent_cover_output_dir(self) -> Path:
        if self._covers_output:
            return Path(self._covers_output).expanduser()
        return self.get_data_path() / "output"

    @eventmanager.register(EventType.PluginAction)
    def update_covers(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "update_covers":
                return
            self.post_message(
                channel=event.event_data.get("channel"),
                title="开始更新媒体库封面 ...",
                userid=event.event_data.get("user"),
            )
        tips = self.__update_all_libraries()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title=tips,
                userid=event.event_data.get("user"),
            )

    @eventmanager.register(EventType.TransferComplete)
    def update_library_cover(self, event: Event):
        """
        媒体整理完成后，更新所在库封面
        """
        if not self._enabled:
            return
        if not self._transfer_monitor:
            return
        
        event_data = event.event_data    
        if not event_data:
            return
        
        # transfer: TransferInfo = event_data.get("transferinfo")        
        # Event data
        mediainfo: MediaInfo = event_data.get("mediainfo")

        # logger.info(f"转移信息：{transfer}")
        # logger.info(f"元数据：{meta}")
        # logger.info(f"媒体信息：{mediainfo}")
        # logger.info(f"监控到的媒体信息：{mediainfo}")
        if not mediainfo:
            return
            
        # 开始前清理可能遗留的停止信号，防止阻塞监控
        self._event.clear()

        # Delay
        if self._delay:
            logger.info(f"延迟 {self._delay} 秒后开始更新封面")
            time.sleep(int(self._delay))
            
        # Query the item in media server
        existsinfo = self.mschain.media_exists(mediainfo=mediainfo)
        if not existsinfo or not existsinfo.itemid:
            self.mschain.sync()
            existsinfo = self.mschain.media_exists(mediainfo=mediainfo)
            if not existsinfo:
                logger.warning(f"{mediainfo.title_year} 不存在媒体库中，可能服务器还未扫描完成，建议设置合适的延迟时间")
                return
        
        # Get item details including backdrop
        iteminfo = self.mschain.iteminfo(server=existsinfo.server, item_id=existsinfo.itemid)
        # logger.info(f"获取到媒体项 {mediainfo.title_year} 详情：{iteminfo}")
        if not iteminfo:
            logger.warning(f"获取 {mediainfo.title_year} 详情失败")
            return
            
        # Try to get library ID
        library_id = None
        library = {}
        item_id = existsinfo.itemid
        server = existsinfo.server
        service = self._servers.get(server)
        self.__apply_server_profile(server)
        if service:
            libraries = self.__get_server_libraries(service)
        if libraries and not library_id:
            library = next(
                (library
                 for library in libraries if library.get('Locations', []) 
                 and any(iteminfo.path.startswith(path) for path in library.get('Locations', []))),
                None
            )
        
        if not library:
            logger.warning(f"找不到 {mediainfo.title_year} 所在媒体库")
            return
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")

        update_key = (server, item_id)
        if update_key in self._current_updating_items:
            logger.info(f"媒体库 {server}：{library['Name']} 的项目 {mediainfo.title_year} 正在更新中，跳过此次更新")
            return
        # self.clean_cover_history(save=True)
        old_history = self.get_data('cover_history') or []
        # 新增去重判断逻辑
        latest_item = max(
            (item for item in old_history if str(item.get("library_id")) == str(library_id)),
            key=lambda x: x["timestamp"],
            default=None
        )
        if latest_item and str(latest_item.get("item_id")) == str(item_id):
            logger.info(f"媒体 {mediainfo.title_year} 在库中是最新记录，不更新封面图")
            return
        
        # 安全地获取字体和翻译
        try:
            self.__get_fonts()
        except Exception as e:
            logger.error(f"初始化字体或翻译时出错: {e}")
            # 继续执行，但可能会影响封面生成质量
        new_history = self.update_cover_history(
            server=server, 
            library_id=library_id, 
            item_id=item_id
        )
        # logger.info(f"最新数据： {new_history}")
        self._monitor_sort = 'DateCreated'
        self._current_updating_items.add(update_key)
        if self.__update_library(service, library):
            self._monitor_sort = ''
            self._current_updating_items.remove(update_key)
            logger.info(f"媒体库 {server}：{library['Name']} 封面更新成功")

    
    def __update_all_libraries(self):
        """
        更新所有媒体库封面
        """
        if not self._enabled:
            return
        # 所有媒体服务器
        if not self._servers:
            return
        logger.info("开始检查字体 ...")
        try:
            self.__get_fonts()
        except Exception as e:
            logger.error(f"初始化过程中出错: {e}")
            logger.warning("将尝试继续执行，但可能影响封面生成质量")
        logger.info("开始更新媒体库封面 ...")
        self.__debug_log(f"调试模式开启：selected_libraries={self._selected_libraries}")
        # 开始前确保停止信号已清除
        self._event.clear()
        global_style = self._cover_style
        total_success_count = 0
        total_fail_count = 0
        selected_pairs = self.__parse_selected_libraries()
        selected_servers = {server for server, _ in selected_pairs}
        for server, service in self._servers.items():
            if selected_servers and server not in selected_servers:
                continue
            self.__apply_server_profile(server)
            # 扫描所有媒体库
            logger.info(f"当前服务器 {server}")
            cover_style = {
                "static_1": "静态 1",
                "static_2": "静态 2",
            }.get(self._cover_style, "静态 1")
            logger.info(f"当前风格 {cover_style}")
            # 获取媒体库列表
            libraries = self.__get_server_libraries(service)
            if not libraries:
                logger.warning(f"服务器 {server} 的媒体库列表获取失败")
                continue
            self.__debug_log(f"服务器 {server} 可用媒体库数量={len(libraries)}")
            selected_library_ids = {library_id for srv, library_id in selected_pairs if srv == server}
            if selected_library_ids:
                self.__debug_log(f"服务器 {server} 指定媒体库过滤={sorted(selected_library_ids)}")
                filtered_libraries = []
                for library in libraries:
                    current_library_id = library.get("Id") if service.type == 'emby' else library.get("ItemId")
                    if str(current_library_id or "").strip() in selected_library_ids:
                        filtered_libraries.append(library)
                libraries = filtered_libraries
                if not libraries:
                    logger.warning(f"服务器 {server} 中未找到已选择的媒体库，已跳过")
                    continue
            success_count = 0
            fail_count = 0
            for library in libraries:
                if self._event.is_set():
                    logger.info("媒体库封面更新服务停止")
                    self._event.clear()
                    return
                library_name = str(library.get("Name") or "").strip() or "未知媒体库"
                logger.info(f"当前执行：{server} -> {library_name}")
                if service.type == 'emby':
                    library_id = library.get("Id")
                else:
                    library_id = library.get("ItemId")
                if self.__update_library(service, library):
                    logger.info(f"媒体库 {server}：{library['Name']} 封面更新成功")
                    success_count += 1
                else:
                    logger.warning(f"媒体库 {server}：{library['Name']} 封面更新失败")
                    fail_count += 1
            total_success_count += success_count
            total_fail_count += fail_count
        self._cover_style = global_style
        tips = f"媒体库封面更新任务结束，成功 {total_success_count} 个，失败 {total_fail_count} 个"
        logger.info(tips)
        return tips
                 

    def __update_library(self, service, library):
        library_name = library['Name']
        logger.info(f"媒体库 {service.name}：{library_name} 开始准备更新封面")
        # 自定义图像路径
        image_path = self.__check_custom_image(library_name)
        # 从配置获取标题和背景颜色
        title_result = self.__get_title_from_config(library_name, service.name)
        if len(title_result) == 3:
            title = (title_result[0], title_result[1])
            config_bg_color = title_result[2]
        else:
            title = title_result
            config_bg_color = None
        if image_path:
            logger.info(f"媒体库 {service.name}：{library_name} 从自定义路径获取封面")
            image_data = self.__generate_image_from_path(service.name, library_name, title, image_path[0], config_bg_color)
        else:
            image_data = self.__generate_from_server(service, library, title)

        if image_data:
            return self.__set_library_image(service, library, image_data)

    def __check_custom_image(self, library_name):
        if not self._covers_input:
            return None

        # 使用安全的文件名
        safe_library_name = self.__sanitize_filename(library_name)
        library_dir = os.path.join(self._covers_input, safe_library_name)
        if not os.path.isdir(library_dir):
            return None

        images = sorted([
            os.path.join(library_dir, f)
            for f in os.listdir(library_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"))
        ])
        
        return images if images else None  # 或改为 return images if images else False

    def __generate_image_from_path(self, server, library_name, title, image_path=None, config_bg_color=None):
        gc.collect()
        logger.info(f"媒体库 {server}：{library_name} 正在生成封面图 ...")
        image_data = False

        # 执行健康检查
        if not self.health_check():
            logger.error("插件健康检查失败，无法生成封面")
            return False

        # 确保分辨率配置已初始化
        if not hasattr(self, '_resolution_config') or self._resolution_config is None:
            logger.warning("分辨率配置未初始化，重新初始化")
            # 使用用户设置的分辨率，而不是硬编码的1080p
            if self._resolution == "custom":
                try:
                    custom_w = int(self._custom_width)
                    custom_h = int(self._custom_height)
                    self._resolution_config = self.__new_resolution_config((custom_w, custom_h))
                except ValueError:
                    logger.warning(f"自定义分辨率参数无效: {self._custom_width}x{self._custom_height}, 使用默认1080p")
                    self._resolution_config = self.__new_resolution_config("1080p")
            else:
                self._resolution_config = self.__new_resolution_config(self._resolution)

        # 使用分辨率配置计算字体大小
        try:
            base_zh_font_size = float(self._zh_font_size) if self._zh_font_size else 170
        except ValueError:
            base_zh_font_size = 170
            
        try:
            base_en_font_size = float(self._en_font_size) if self._en_font_size else 75
        except ValueError:
            base_en_font_size = 75

        try:
            title_scale = float(self._title_scale) if self._title_scale else 1.0
        except (ValueError, TypeError):
            title_scale = 1.0
        if title_scale <= 0:
            title_scale = 1.0
        if self._cover_style.startswith("animated"):
            zh_font_size = float(base_zh_font_size) * title_scale
            en_font_size = float(base_en_font_size) * title_scale
        else:
            # 静态风格按当前分辨率缩放
            zh_font_size = self._resolution_config.get_font_size(base_zh_font_size) * title_scale
            en_font_size = self._resolution_config.get_font_size(base_en_font_size) * title_scale

        blur_size = self._blur_size or 50
        color_ratio = self._color_ratio or 0.8

        # 检查字体路径是否有效
        if not self._zh_font_path or not self._en_font_path:
            logger.error("字体路径未设置或无效，无法生成封面")
            return False

        # 验证字体文件是否存在
        if not self.__validate_font_file(Path(self._zh_font_path)):
            logger.error(f"主标题字体文件无效: {self._zh_font_path}")
            return False

        if not self.__validate_font_file(Path(self._en_font_path)):
            logger.error(f"副标题字体文件无效: {self._en_font_path}")
            return False

        font_path = (str(self._zh_font_path), str(self._en_font_path))
        font_size = (float(zh_font_size), float(en_font_size))

        zh_font_offset = float(self._zh_font_offset or 0)
        title_spacing = float(self._title_spacing or 40) * title_scale
        en_line_spacing = float(self._en_line_spacing or 40) * title_scale
        font_offset = (float(zh_font_offset), float(title_spacing), float(en_line_spacing))

        # 记录分辨率配置信息
        logger.info(f"当前分辨率配置: {self._resolution_config}")

        # 准备背景颜色配置
        bg_color_config = {
            'mode': self._bg_color_mode,
            'custom_color': self._custom_bg_color,
            'config_color': config_bg_color
        }

        # 传递分辨率配置给图像生成函数
        if self._cover_style == 'static_1':
            create_style_static_1 = self.__load_style_creator("style_static_1", "create_style_static_1")
            safe_library_name = self.__sanitize_filename(library_name)
            if image_path:
                library_dir = Path(self._covers_input) / safe_library_name
            else:
                library_dir = Path(self._covers_path) / safe_library_name
            logger.info(f"static_1: 准备图片目录 {library_dir}")
            if self.prepare_library_images(library_dir, required_items=9):
                logger.info("static_1: 图片目录准备完成，开始生成封面")
                image_data = create_style_static_1(
                    library_dir, title, font_path,
                    font_size=font_size,
                    font_offset=font_offset,
                    is_blur=self._multi_1_blur,
                    blur_size=blur_size,
                    color_ratio=color_ratio,
                    resolution_config=self._resolution_config,
                    bg_color_config=bg_color_config)
            else:
                logger.warning(f"static_1: 图片目录准备失败 {library_dir}")
        elif self._cover_style == 'static_2':
            create_style_static_2 = self.__load_style_creator("style_static_2", "create_style_static_2")
            safe_library_name = self.__sanitize_filename(library_name)
            cache_library_dir = Path(self._covers_path) / safe_library_name
            custom_library_dir = Path(self._covers_input) / safe_library_name if self._covers_input else None
            numbered_posters = [cache_library_dir / f"{index}.jpg" for index in range(1, 6)]
            if all(path.exists() for path in numbered_posters):
                library_dir = cache_library_dir
            elif custom_library_dir and custom_library_dir.exists():
                library_dir = custom_library_dir
            else:
                library_dir = cache_library_dir
            logger.info(f"static_2: 准备图片目录 {library_dir}")
            if self.prepare_library_images(library_dir, required_items=6):
                logger.info("static_2: 图片目录准备完成，开始生成封面")
                image_data = create_style_static_2(
                    image_path=image_path,
                    library_dir=library_dir,
                    title=title,
                    font_path=font_path,
                    font_size=font_size,
                    font_offset=font_offset,
                    blur_size=blur_size,
                    color_ratio=color_ratio,
                    resolution_config=self._resolution_config,
                    bg_color_config=bg_color_config,
                )
            else:
                logger.warning(f"static_2: 图片目录准备失败 {library_dir}")
        gc.collect()
        return image_data
    
    def __generate_from_server(self, service, library, title):

        logger.info(f"媒体库 {service.name}：{library['Name']} 开始筛选媒体项")
        required_items = self.__get_required_items()
        target_items = self.__get_fetch_target_count()
        
        # 获取项目集合
        items = []
        offset = 0
        batch_size = 50  # 每次获取的项目数量
        max_attempts = 20  # 最大尝试次数，防止无限循环
        
        library_type = library.get('CollectionType')
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        parent_id = library_id
        
        # 处理合集类型的特殊情况
        if library_type == "boxsets":
            return self.__handle_boxset_library(service, library, title)
        elif library_type == "playlists":
            return self.__handle_playlist_library(service, library, title)
        elif library_type == "music":
            include_types = 'MusicAlbum,Audio'
        else:
            # 基础类型映射
            if self.__is_single_image_style():
                include_types = {
                    "PremiereDate": "Movie,Series",
                    "DateCreated": "Movie,Episode",
                    "Random": "Movie,Series"
                }.get(self._sort_by, "Movie,Series")
            else:
                # 对于多图样式，如果按最新入库排序（DateCreated），也要包含 Episode 以展示剧集的最新动态
                if self._sort_by == "DateCreated":
                    include_types = "Movie,Episode"
                else:
                    # 其他排序方式默认使用 Series 获取海报
                    include_types = "Movie,Series"
            logger.debug(f"媒体库筛选类型: {include_types}, 排序方式: {self._sort_by}")
        self._seen_keys = set()
        for attempt in range(max_attempts):
            if self._event.is_set():
                logger.info("检测到停止信号，中断媒体项获取 ...")
                return False
                
            batch_items = self.__get_items_batch(service, parent_id,
                                              offset=offset, limit=batch_size,
                                              include_types=include_types)
            
            if not batch_items:
                break  # 没有更多项目可获取
                
            # 筛选有效项目（有所需图片的项目）
            valid_items = self.__filter_valid_items(batch_items)
            items.extend(valid_items)
            
            # 如果已经有足够的有效项目，则停止获取
            if len(items) >= target_items:
                break
                
            offset += batch_size
        
        # 使用获取到的有效项目更新封面
        if len(items) > 0:
            logger.info(f"媒体库 {service.name}：{library['Name']} 找到 {len(items)} 个有效项目")
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, items[:required_items])
        else:
            logger.warning(f"媒体库 {service.name}：{library['Name']} 无法找到有效的图片项目 (筛选类型: {include_types})")
            return False
        
    def __handle_boxset_library(self, service, library, title):

        include_types = 'BoxSet,Movie'
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        parent_id = library_id
        boxsets = self.__get_items_batch(service, parent_id,
                                      include_types=include_types)
        
        required_items = self.__get_required_items()
        target_items = self.__get_fetch_target_count()
        valid_items = []
        
        # 首先检查BoxSet本身是否有合适的图片
        self._seen_keys = set()

        valid_boxsets = self.__filter_valid_items(boxsets)
        valid_items.extend(valid_boxsets)
        
        # 如果BoxSet本身没有足够的图片，则获取其中的电影
        if len(valid_items) < target_items:
            for boxset in boxsets:
                if len(valid_items) >= target_items:
                    break
                    
                # 获取此BoxSet中的电影
                movies = self.__get_items_batch(service,
                                             parent_id=boxset['Id'], 
                                             include_types=include_types)
                
                valid_movies = self.__filter_valid_items(movies)
                valid_items.extend(valid_movies)
                
                if len(valid_items) >= target_items:
                    break
        
        # 使用获取到的有效项目更新封面
        if len(valid_items) > 0:
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, valid_items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, valid_items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, valid_items[:required_items])
        else:
            print(f"媒体库 {service.name}：{library['Name']} 无法找到有效的图片项目")
            return False
        
    def __handle_playlist_library(self, service, library, title):
        """ 
        播放列表图片获取 
        """
        include_types = 'Playlist,Movie,Series,Episode,Audio'
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        parent_id = library_id
        playlists = self.__get_items_batch(service, parent_id,
                                      include_types=include_types)
        
        required_items = self.__get_required_items()
        target_items = self.__get_fetch_target_count()
        valid_items = []
        
        # 首先检查 playlist 本身是否有合适的图片
        self._seen_keys = set()

        valid_playlists = self.__filter_valid_items(playlists)
        valid_items.extend(valid_playlists)
        
        # 如果 playlist 本身没有足够的图片，则获取其中的电影
        if len(valid_items) < target_items:
            for playlist in playlists:
                if len(valid_items) >= target_items:
                    break
                    
                # 获取此 playlist 中的电影
                movies = self.__get_items_batch(service,
                                             parent_id=playlist['Id'], 
                                             include_types=include_types)
                
                valid_movies = self.__filter_valid_items(movies)
                valid_items.extend(valid_movies)
                
                if len(valid_items) >= target_items:
                    break
        
        # 使用获取到的有效项目更新封面
        if len(valid_items) > 0:
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, valid_items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, valid_items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, valid_items[:required_items])
        else:
            print(f"警告: 无法为播放列表 {service.name}：{library['Name']} 找到有效的图片项目")
            return False
        
    def __get_items_batch(self, service, parent_id, offset=0, limit=20, include_types=None):
        # 调用API获取项目
        try:
            if not service:
                return []
            
            try:
                if not self._sort_by:
                    sort_by = 'Random'
                else:
                    sort_by = self._sort_by
                if self._monitor_sort:
                    sort_by = 'DateCreated'
                    # 转移监控模式下强制包含 Episode 以获取最新入库的内容
                    include_types = 'Movie,Episode'
                if not include_types:
                    include_types = 'Movie,Series'

                url = f'[HOST]emby/Items/?api_key=[APIKEY]' \
                      f'&ParentId={parent_id}&SortBy={sort_by}&Limit={limit}' \
                      f'&StartIndex={offset}&IncludeItemTypes={include_types}' \
                      f'&Recursive=True&SortOrder=Descending'

                res = service.instance.get_data(url=url)
                if res:
                    data = res.json()
                    return data.get("Items", [])
            except Exception as err:
                logger.error(f"获取媒体项失败：{str(err)}")
            return []
                
        except Exception as err:
            logger.error(f"Failed to get latest items: {str(err)}")
            return []
        
    def __filter_valid_items(self, items):
        """筛选有效的项目（包含所需图片的项目），并按图片标签去重"""
        valid_items = []

        for item in items:
            # 1) 根据当前样式计算真实会使用的图片URL
            image_url = self.__get_image_url(item)
            if not image_url:
                continue

            # 2) 两层去重：
            #    - content_key: 内容层（如同一剧集的多集使用同一Series图）
            #    - image_key:   图片层（同一图片tag或同一路径）
            content_key = self.__build_content_key(item)
            image_key = self.__build_image_key(image_url)

            if not content_key and not image_key:
                continue

            if (content_key and content_key in self._seen_keys) or (image_key and image_key in self._seen_keys):
                continue

            # 3) 加入有效列表并记录已处理的 Key
            valid_items.append(item)
            if content_key:
                self._seen_keys.add(content_key)
            if image_key:
                self._seen_keys.add(image_key)

        return valid_items

    def __build_content_key(self, item: dict) -> Optional[str]:
        """构建内容去重Key，尽量让同一来源内容只入选一次。"""
        item_type = item.get("Type")

        if item_type == "Episode":
            if item.get("SeriesId"):
                return f"series:{item.get('SeriesId')}"
            if item.get("ParentBackdropItemId"):
                return f"parent:{item.get('ParentBackdropItemId')}"

        if item_type in ["MusicAlbum", "Audio"]:
            if item.get("AlbumId"):
                return f"album:{item.get('AlbumId')}"
            if item.get("ParentBackdropItemId"):
                return f"parent:{item.get('ParentBackdropItemId')}"

        if item.get("Id"):
            return f"item:{item.get('Id')}"

        return None

    def __build_image_key(self, image_url: str) -> Optional[str]:
        """构建图片去重Key，忽略api_key，避免同图重复。"""
        if not image_url:
            return None

        try:
            # 统一移除 api_key 参数，避免同图不同密钥导致重复
            normalized = re.sub(r"([?&])api_key=[^&]*", "", image_url).rstrip("?&")

            # 优先用路径 + tag 作为去重关键字（能精准区分图像版本）
            # 例如: /Items/{id}/Images/Backdrop/0?tag=xxx
            tag_match = re.search(r"[?&]tag=([^&]+)", image_url)
            tag = tag_match.group(1) if tag_match else ""

            parsed = urlparse(normalized)
            path = parsed.path if parsed.path else normalized
            return f"img:{path}|tag:{tag}"
        except Exception:
            return f"img:{image_url}"


    
    def __update_single_image(self, service, library, title, item):
        """更新单图封面"""
        logger.info(f"媒体库 {service.name}：{library['Name']} 从媒体项获取图片")
        updated_item_id = ''
        image_url = self.__get_image_url(item)
        if not image_url:
            return False
            
        image_path = self.__download_image(service, image_url, library['Name'], count=1)
        if not image_path:
            return False
        updated_item_id = self.__get_item_id(item)
        # 从配置获取背景颜色
        title_result = self.__get_title_from_config(library['Name'], service.name)
        config_bg_color = title_result[2] if len(title_result) == 3 else None
        image_data = self.__generate_image_from_path(service.name, library['Name'], title, image_path, config_bg_color)
            
        if not image_data:
            return False
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        # 更新id
        self.update_cover_history(
            server=service.name, 
            library_id=library_id, 
            item_id=updated_item_id
        )

        return image_data
    
    def __update_grid_image(self, service, library, title, items):
        """更新九宫格封面"""
        logger.info(f"媒体库 {service.name}：{library['Name']} 从媒体项获取图片")

        image_paths = []
        
        updated_item_ids = []
        for i, item in enumerate(items):
            if self._event.is_set():
                logger.info("检测到停止信号，中断图片下载 ...")
                return False
            image_url = self.__get_image_url(item)
            if image_url:
                image_path = self.__download_image(service, image_url, library['Name'], count=i+1)
                if image_path:
                    image_paths.append(image_path)
                    updated_item_ids.append(self.__get_item_id(item))
        
        if len(image_paths) < 1:
            return False
            
        # 生成九宫格图片
        # 从配置获取背景颜色
        title_result = self.__get_title_from_config(library['Name'], service.name)
        config_bg_color = title_result[2] if len(title_result) == 3 else None
        image_data = self.__generate_image_from_path(service.name, library['Name'], title, None, config_bg_color)
        if not image_data:
            return False
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        # 更新ids
        for item_id in reversed(updated_item_ids):
            self.update_cover_history(
                server=service.name, 
                library_id=library_id, 
                item_id=item_id
            )
            
        return image_data

    def __update_showcase_image(self, service, library, title, items):
        """更新横幅背景 + 多海报展示封面。"""
        logger.info(f"媒体库 {service.name}：{library['Name']} 生成横幅展示封面")

        if not items:
            return False

        background_path = None
        background_item_id = None
        updated_item_ids = []

        for item in items:
            if self._event.is_set():
                logger.info("检测到停止信号，中断图片下载 ...")
                return False

            if background_path:
                break
            background_url = self.__get_showcase_background_url(item)
            if background_url:
                background_path = self.__download_image(service, background_url, library["Name"], count="fanart")
                if background_path:
                    background_item_id = self.__get_item_id(item)

        if not background_path:
            for item in items:
                fallback_url = self.__get_showcase_poster_url(item)
                if fallback_url:
                    background_path = self.__download_image(service, fallback_url, library["Name"], count="fanart")
                    if background_path:
                        background_item_id = self.__get_item_id(item)
                        break

        poster_candidates = [item for item in items if self.__get_item_id(item) != background_item_id]
        if len(poster_candidates) < self.__get_required_items():
            for item in items:
                if item not in poster_candidates:
                    poster_candidates.append(item)

        for item in poster_candidates:
            if self._event.is_set():
                logger.info("检测到停止信号，中断图片下载 ...")
                return False
            if len(updated_item_ids) >= self.__get_required_items():
                break
            poster_url = self.__get_showcase_poster_url(item)
            if not poster_url:
                continue
            poster_count = len(updated_item_ids) + 1
            image_path = self.__download_image(service, poster_url, library["Name"], count=poster_count)
            if image_path:
                updated_item_ids.append(self.__get_item_id(item))

        if not background_path or not updated_item_ids:
            return False

        title_result = self.__get_title_from_config(library["Name"], service.name)
        config_bg_color = title_result[2] if len(title_result) == 3 else None
        image_data = self.__generate_image_from_path(
            service.name,
            library["Name"],
            title,
            background_path,
            config_bg_color,
        )
        if not image_data:
            return False

        if service.type == "emby":
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")

        for item_id in reversed(updated_item_ids):
            self.update_cover_history(
                server=service.name,
                library_id=library_id,
                item_id=item_id,
            )

        return image_data
    
    def __load_title_config(self, yaml_str: str) -> dict:
        try:
            # 替换全角冒号为半角
            yaml_str = yaml_str.replace("：", ":")
            # 替换制表符为两个空格，统一缩进
            yaml_str = yaml_str.replace("\t", "  ")

            # 处理数字或字母开头的媒体库名，确保它们被正确解析为字符串键
            # 在YAML中，数字开头的键可能被解析为数字，需要加引号
            lines = yaml_str.split('\n')
            processed_lines = []
            for line in lines:
                # 检查是否是键值对行（包含冒号且不是注释）
                if ':' in line and not line.strip().startswith('#'):
                    # 分割键和值
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key_part = parts[0].strip()
                        value_part = parts[1]

                        # 如果键不是以引号开头，且包含数字或特殊字符，则添加引号
                        if key_part and not (key_part.startswith('"') or key_part.startswith("'")):
                            # 检查是否需要加引号（数字开头、包含特殊字符等）
                            if (key_part[0].isdigit() or
                                any(char in key_part for char in [' ', '-', '.', '(', ')', '[', ']'])):
                                key_part = f'"{key_part}"'

                        processed_lines.append(f"{key_part}:{value_part}")
                    else:
                        processed_lines.append(line)
                else:
                    processed_lines.append(line)

            processed_yaml = '\n'.join(processed_lines)
            preview_limit = 800
            flat_yaml = " ".join(part.strip() for part in processed_yaml.splitlines() if part.strip())
            if len(flat_yaml) > preview_limit:
                logger.debug(f"处理后的YAML(扁平, 前{preview_limit}字): {flat_yaml[:preview_limit]}... (已截断)")
            else:
                logger.debug(f"处理后的YAML(扁平): {flat_yaml}")

            title_config = yaml.safe_load(processed_yaml) or {}
            if not isinstance(title_config, dict):
                return {}
            filtered = {}
            for key, value in title_config.items():
                if value is None:
                    # 允许仅声明服务器键，未配置媒体库时不告警
                    continue
                if isinstance(value, dict):
                    # 新格式：服务器名 -> 媒体库配置字典
                    server_filtered = {}
                    for lib_key, lib_value in value.items():
                        if lib_value is None:
                            continue
                        if (
                            isinstance(lib_value, list)
                            and len(lib_value) >= 2
                            and isinstance(lib_value[0], str)
                            and isinstance(lib_value[1], str)
                        ):
                            if len(lib_value) >= 3 and isinstance(lib_value[2], str):
                                server_filtered[str(lib_key)] = [lib_value[0], lib_value[1], lib_value[2]]
                            else:
                                server_filtered[str(lib_key)] = [lib_value[0], lib_value[1]]
                            if len(lib_value) > 3:
                                logger.info(f"配置项 {key}/{lib_key} 包含多行，只使用前三行")
                        else:
                            logger.warning(f"标题配置项格式不正确，已忽略: {key}/{lib_key} -> {lib_value}")
                    if server_filtered:
                        filtered[str(key)] = server_filtered
                    continue

                if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], str) and isinstance(value[1], str):
                    # 兼容旧格式：媒体库 -> [中文, 英文, 可选背景色]
                    if len(value) >= 3 and isinstance(value[2], str):
                        filtered[str(key)] = [value[0], value[1], value[2]]
                    else:
                        filtered[str(key)] = [value[0], value[1]]
                    if len(value) > 3:
                        logger.info(f"配置项 {key} 包含多行，只使用前三行")
                    continue

                # 忽略格式不正确的项
                logger.warning(f"标题配置项格式不正确，已忽略: {key} -> {value}")
                continue

            logger.debug(f"解析后的配置: {filtered}")
            return filtered
        except Exception as e:
            # 整体 YAML 无法解析（比如语法错误），返回空配置
            logger.warning(f"YAML 解析失败，使用空配置: {e}")
            return {}

    def __get_title_from_config(self, library_name, server_name: Optional[str] = None):
        """
        从 yaml 配置中获取媒体库的主副标题和背景颜色
        支持：
        1. 服务器分组：服务器名 -> 媒体库名 -> [中文, 英文, 可选背景色]
        2. 旧格式：媒体库名 -> [中文, 英文, 可选背景色]
        """
        zh_title = library_name
        en_title = ''
        bg_color = None
        title_config = {}
        if self._current_config:
            title_config = self._current_config
        elif self._title_config:
            title_config = self.__load_title_config(self._title_config)

        scoped_config = title_config
        nested_mode = any(isinstance(v, dict) for v in title_config.values()) if isinstance(title_config, dict) else False
        if server_name and isinstance(title_config, dict):
            server_key = None
            if server_name in title_config and isinstance(title_config.get(server_name), dict):
                server_key = server_name
            else:
                for key, value in title_config.items():
                    if isinstance(value, dict) and str(key).strip().lower() == str(server_name).strip().lower():
                        server_key = str(key)
                        break
            if server_key:
                scoped_config = title_config.get(server_key, {})
                logger.debug(f"标题配置按服务器匹配成功: {server_key}")
            elif nested_mode:
                # 新格式下未找到服务器时，不跨服务器扫描
                scoped_config = {}
                logger.debug(f"标题配置未找到服务器分组: {server_name}")

        # 添加调试信息
        logger.debug(f"查找媒体库名称: '{library_name}' (类型: {type(library_name)})")
        logger.debug(f"当前作用域配置键: {list(scoped_config.keys()) if isinstance(scoped_config, dict) else []}")

        # 多种匹配策略，确保数字或字母开头的媒体库名能够正确匹配
        for lib_name, config_values in (scoped_config.items() if isinstance(scoped_config, dict) else []):
            if not isinstance(config_values, list) or len(config_values) < 2:
                continue
            # 策略1: 直接字符串比较
            if str(lib_name) == str(library_name):
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"找到匹配的配置(直接匹配): {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break

            # 策略2: 去除空格后比较
            if str(lib_name).strip() == str(library_name).strip():
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"找到匹配的配置(去空格匹配): {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break

            # 策略3: 忽略大小写比较
            if str(lib_name).lower() == str(library_name).lower():
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"找到匹配的配置(忽略大小写匹配): {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break
        else:
            logger.debug(f"未找到媒体库 '{library_name}' 的配置，使用默认标题")
            # 如果没有找到配置，检查是否是数字开头的媒体库名导致的问题
            if library_name and (library_name[0].isdigit() or library_name[0].isalpha()):
                logger.info(f"媒体库名 '{library_name}' 以数字或字母开头，如果需要自定义标题，请在配置中使用引号包围媒体库名，例如: \"{library_name}\":")

        return (zh_title, en_title, bg_color)

    def __safe_log_url(self, url: str) -> str:
        text = str(url or "")
        if self._debug_mode:
            return text
        return re.sub(r"(api_key=)[^&]+", r"\1***", text)

    def __debug_log(self, message: str):
        if self._debug_mode:
            logger.info(f"[DEBUG] {message}")
    
    def __get_server_libraries(self, service):
        try:
            if not service:
                return []
            try:
                if service.type == 'emby':
                    url = f'[HOST]emby/Library/VirtualFolders/Query?api_key=[APIKEY]'
                else:
                    url = f'[HOST]emby/Library/VirtualFolders/?api_key=[APIKEY]'
                request_url = url
                try:
                    replace_url = getattr(service.instance, "_replace_url", None)
                    if callable(replace_url):
                        request_url = replace_url(url)
                except Exception:
                    request_url = url
                request_url_safe = self.__safe_log_url(request_url)
                self.__debug_log(f"请求媒体库列表：server={getattr(service, 'name', 'unknown')} url={request_url_safe}")
                res = service.instance.get_data(url=url)
                if not res:
                    logger.warning(f"获取媒体库列表失败(无响应)：server={getattr(service, 'name', 'unknown')} url={request_url_safe}")
                    return []
                self.__debug_log(f"媒体库列表响应：server={getattr(service, 'name', 'unknown')} status={res.status_code}")
                if res.status_code >= 400:
                    body_preview = ""
                    try:
                        body_preview = (res.text or "").strip().replace("\n", " ")[:300]
                    except Exception:
                        body_preview = ""
                    logger.warning(
                        f"获取媒体库列表失败(HTTP {res.status_code})：server={getattr(service, 'name', 'unknown')} "
                        f"url={request_url_safe} response={body_preview}"
                    )
                    return []
                try:
                    data = res.json()
                except Exception as json_err:
                    body_preview = ""
                    try:
                        body_preview = (res.text or "").strip().replace("\n", " ")[:300]
                    except Exception:
                        body_preview = ""
                    logger.warning(
                        f"获取媒体库列表失败(JSON解析失败)：server={getattr(service, 'name', 'unknown')} "
                        f"url={request_url_safe} err={json_err} response={body_preview}"
                    )
                    return []
                count_hint = len(data) if isinstance(data, list) else len(data.get("Items", [])) if isinstance(data, dict) else 0
                self.__debug_log(f"媒体库列表解析成功：server={getattr(service, 'name', 'unknown')} count={count_hint}")
                if service.type == 'emby':
                    return data.get("Items", []) if isinstance(data, dict) else []
                return data if isinstance(data, list) else []
            except Exception as err:
                logger.error(f"获取媒体库列表失败：{str(err)}")
            return []
        except Exception as err:
            logger.error(f"获取媒体库列表失败：{str(err)}")
            return []
    
    def __get_all_libraries(self, server, service):
        try:
            lib_items = []
            libraries = self.__get_server_libraries(service)
            for library in libraries:
                if service.type == 'emby':
                    library_id = library.get("Id")
                else:
                    library_id = library.get("ItemId")
                if library['Name'] and library_id:
                    lib_item = {
                        "title": f"{server} - {library['Name']}",
                        "value": f"{server}::{library_id}"
                    }
                    lib_items.append(lib_item)
            return lib_items
        except Exception as err:
            logger.error(f"获取所有媒体库失败：{str(err)}")
            return []

    def __parse_selected_libraries(self) -> List[Tuple[str, str]]:
        selected: List[Tuple[str, str]] = []
        for item in (self._selected_libraries or []):
            raw = str(item or "").strip()
            if not raw:
                continue
            if "::" in raw:
                server, library_id = raw.split("::", 1)
            elif "-" in raw:
                # 向后兼容旧格式：server-library_id
                server, library_id = raw.split("-", 1)
            else:
                continue
            server = server.strip()
            library_id = library_id.strip()
            if server and library_id:
                selected.append((server, library_id))
        return selected

    def __get_showcase_background_url(self, item):
        """获取横幅展示风格使用的背景图URL，优先背景图。"""
        if item.get("Type") == "Episode":
            if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                item_id = item.get("ParentBackdropItemId")
                tag = item["ParentBackdropImageTags"][0]
                return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
            if item.get("SeriesPrimaryImageTag"):
                item_id = item.get("SeriesId")
                tag = item.get("SeriesPrimaryImageTag")
                return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
            item_id = item.get("ParentBackdropItemId")
            tag = item["ParentBackdropImageTags"][0]
            return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'

        if item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
            item_id = item.get("Id")
            tag = item["BackdropImageTags"][0]
            return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'

        return self.__get_showcase_poster_url(item)

    def __get_showcase_poster_url(self, item):
        """获取横幅展示风格使用的海报URL，优先竖版主海报。"""
        if item.get("Type") == "Episode":
            if item.get("SeriesPrimaryImageTag"):
                item_id = item.get("SeriesId")
                tag = item.get("SeriesPrimaryImageTag")
                return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
            if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                item_id = item.get("ParentBackdropItemId")
                tag = item["ParentBackdropImageTags"][0]
                return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'

        if item.get("ImageTags") and item.get("ImageTags").get("Primary"):
            item_id = item.get("Id")
            tag = item.get("ImageTags").get("Primary")
            return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        if item.get("PrimaryImageTag"):
            item_id = item.get("PrimaryImageItemId") or item.get("Id")
            tag = item.get("PrimaryImageTag")
            return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        if item.get("AlbumPrimaryImageTag"):
            item_id = item.get("AlbumId")
            tag = item.get("AlbumPrimaryImageTag")
            return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
            item_id = item.get("ParentBackdropItemId")
            tag = item["ParentBackdropImageTags"][0]
            return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'

        if item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
            item_id = item.get("Id")
            tag = item["BackdropImageTags"][0]
            return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'

        return None
        
    def __get_image_url(self, item):
        """
        从媒体项信息中获取图片URL
        """
        # Emby/Jellyfin
        if item['Type'] in 'MusicAlbum,Audio':
            if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                item_id = item.get("ParentBackdropItemId")
                tag = item["ParentBackdropImageTags"][0]
                return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
            elif item.get("PrimaryImageTag"):
                item_id = item.get("PrimaryImageItemId")
                tag = item.get("PrimaryImageTag")
                return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
            elif item.get("AlbumPrimaryImageTag"):
                item_id = item.get("AlbumId")
                tag = item.get("AlbumPrimaryImageTag")
                return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        elif self._cover_style == 'static_2':
            return self.__get_showcase_poster_url(item)

        elif False:
            if self._use_primary:
                if item.get("Type") == 'Episode':
                    if item.get("SeriesPrimaryImageTag"):
                        item_id = item.get("SeriesId")
                        tag = item.get("SeriesPrimaryImageTag")
                        return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                    elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                        item_id = item.get("ParentBackdropItemId")
                        tag = item["ParentBackdropImageTags"][0]
                        return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("ImageTags") and item.get("ImageTags").get("Primary"):
                    item_id = item.get("Id")
                    tag = item.get("ImageTags").get("Primary")
                    return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                    tag = item["ParentBackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
                    item_id = item.get("Id")
                    tag = item["BackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
            else:
                if item.get("Type") == 'Episode':
                    if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                        item_id = item.get("ParentBackdropItemId")
                        tag = item["ParentBackdropImageTags"][0]
                        return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                    elif item.get("SeriesPrimaryImageTag"):
                        item_id = item.get("SeriesId")
                        tag = item.get("SeriesPrimaryImageTag")
                        return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                    tag = item["ParentBackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
                    item_id = item.get("Id")
                    tag = item["BackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("ImageTags") and item.get("ImageTags").get("Primary"):
                    item_id = item.get("Id")
                    tag = item.get("ImageTags").get("Primary")
                    return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'

        elif self._cover_style.startswith('static'):
            if self._use_primary:
                if item.get("Type") == 'Episode':
                    if item.get("SeriesPrimaryImageTag"):
                        item_id = item.get("SeriesId")
                        tag = item.get("SeriesPrimaryImageTag")
                        return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                    elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                        item_id = item.get("ParentBackdropItemId")
                        tag = item["ParentBackdropImageTags"][0]
                        return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("ImageTags") and item.get("ImageTags").get("Primary"):
                    item_id = item.get("Id")
                    tag = item.get("ImageTags").get("Primary")
                    return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                    tag = item["ParentBackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
                    item_id = item.get("Id")
                    tag = item["BackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
            else:
                if item.get("Type") == 'Episode':
                    if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                        item_id = item.get("ParentBackdropItemId")
                        tag = item["ParentBackdropImageTags"][0]
                        return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                    elif item.get("SeriesPrimaryImageTag"):
                        item_id = item.get("SeriesId")
                        tag = item.get("SeriesPrimaryImageTag")
                        return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
                elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                    tag = item["ParentBackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
                    item_id = item.get("Id")
                    tag = item["BackdropImageTags"][0]
                    return f'[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]'
                elif item.get("ImageTags") and item.get("ImageTags").get("Primary"):
                    item_id = item.get("Id")
                    tag = item.get("ImageTags").get("Primary")
                    return f'[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]'
            
    def __get_item_id(self, item):
        """
        从媒体项信息中获取项目ID
        """
        # Emby/Jellyfin
        if item['Type'] in 'MusicAlbum,Audio':
            if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                item_id = item.get("ParentBackdropItemId")
            elif item.get("PrimaryImageTag"):
                item_id = item.get("PrimaryImageItemId")
            elif item.get("AlbumPrimaryImageTag"):
                item_id = item.get("AlbumId")

        elif self._cover_style == 'static_2':
            if item.get("Type") == "Episode" and item.get("SeriesId"):
                item_id = item.get("SeriesId")
            else:
                item_id = item.get("Id") or item.get("PrimaryImageItemId") or item.get("ParentBackdropItemId")

        elif False:
            if self._use_primary:
                if (item.get("ImageTags") and item.get("ImageTags").get("Primary")) \
                    or (item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0):
                    item_id = item.get("Id")
                elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
            else:
                if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                elif (item.get("ImageTags") and item.get("ImageTags").get("Primary")) \
                    or (item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0):
                    item_id = item.get("Id")

        elif self._cover_style.startswith('static'):
            if self._use_primary:
                if (item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0) \
                    or (item.get("ImageTags") and item.get("ImageTags").get("Primary")):
                    item_id = item.get("Id")
                elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
            else:
                if item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                    item_id = item.get("ParentBackdropItemId")
                elif (item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0) \
                    or (item.get("ImageTags") and item.get("ImageTags").get("Primary")):
                    item_id = item.get("Id")

        return item_id

    def __download_image(self, service, imageurl, library_name, count=None, retries=3, delay=1):
        """
        下载图片，保存到本地目录 self._covers_path/library_name/ 下，文件名为 1-9.jpg
        若已存在则跳过下载，直接返回图片路径。
        下载失败时重试若干次。
        """
        try:
            # 确保媒体库名称是安全的文件名（处理数字或字母开头的名称）
            safe_library_name = self.__sanitize_filename(library_name)

            # 创建目标子目录
            subdir = os.path.join(self._covers_path, safe_library_name)
            os.makedirs(subdir, exist_ok=True)

            # 文件命名：item_id 为主，适合排序
            if count is not None:
                filename = f"{count}.jpg"
            else:
                filename = f"img_{int(time.time())}.jpg"

            filepath = os.path.join(subdir, filename)

            # 如果文件已存在，直接返回路径
            # if os.path.exists(filepath):
            #     return filepath

            # 重试机制
            for attempt in range(1, retries + 1):
                image_content = None

                if '[HOST]' in imageurl:
                    if not service:
                        return None

                    r = service.instance.get_data(url=imageurl)
                    if r and r.status_code == 200:
                        image_content = r.content
                else:
                    r = RequestUtils().get_res(url=imageurl)
                    if r and r.status_code == 200:
                        image_content = r.content

                # 如果成功，保存并返回
                if image_content:
                    with open(filepath, 'wb') as f:
                        f.write(image_content)
                    return filepath

                # 如果失败，记录并等待后重试
                logger.warning(f"第 {attempt} 次尝试下载失败：{imageurl}")
                if attempt < retries:
                    time.sleep(delay)

            logger.error(f"图片下载失败（重试 {retries} 次）：{imageurl}")
            return None

        except Exception as err:
            logger.error(f"下载图片异常：{str(err)}")
            return None


    def __save_image_to_local(self, image_content, server_name: str, library_name: str, extension: str):
        """
        保存图片到本地路径
        """
        try:
            if not self._save_recent_covers:
                return
            # 确保目录存在
            local_path = str(self.__get_recent_cover_output_dir())
            os.makedirs(local_path, exist_ok=True)

            safe_server = self.__sanitize_filename(server_name) or "server"
            safe_library = self.__sanitize_filename(library_name) or "library"
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = extension.strip(".").lower() if extension else "jpg"
            filename = f"{safe_server}_{safe_library}_{timestamp}.{ext}"

            file_path = os.path.join(local_path, filename)
            with open(file_path, "wb") as f:
                f.write(image_content)
            logger.info(f"图片已保存到本地: {file_path}")
            self.__trim_saved_cover_history(local_path, safe_server, safe_library)
        except Exception as err:
            logger.error(f"保存图片到本地失败: {str(err)}")

    def __trim_saved_cover_history(self, local_path: str, safe_server: str, safe_library: str):
        limit = self.__clamp_value(
            self._covers_history_limit_per_library,
            1,
            100,
            10,
            "covers_history_limit_per_library[trim]",
            int,
        )
        pattern = f"{safe_server}_{safe_library}_"
        candidate_files: List[Path] = []
        try:
            for file_name in os.listdir(local_path):
                lower_name = file_name.lower()
                if not lower_name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".apng")):
                    continue
                if not file_name.startswith(pattern):
                    continue
                file_path = Path(local_path) / file_name
                if file_path.is_file():
                    candidate_files.append(file_path)
            if len(candidate_files) <= limit:
                return
            candidate_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old_file in candidate_files[limit:]:
                old_file.unlink(missing_ok=True)
                logger.info(f"已按历史数量限制删除旧封面: {old_file}")
        except Exception as e:
            logger.warning(f"清理历史封面失败: {e}")
        

    def __set_library_image(self, service, library, image_base64):
        """
        设置媒体库封面
        """

        """设置Emby媒体库封面"""
        try:
            if service.type == 'emby':
                library_id = library.get("Id")
            else:
                library_id = library.get("ItemId")
            
            url = f'[HOST]emby/Items/{library_id}/Images/Primary?api_key=[APIKEY]'
            request_url = url
            try:
                replace_url = getattr(service.instance, "_replace_url", None)
                if callable(replace_url):
                    request_url = replace_url(url)
            except Exception:
                request_url = url
            self.__debug_log(
                f"设置封面请求：server={service.name} library={library.get('Name')} library_id={library_id} "
                f"url={self.__safe_log_url(request_url)}"
            )
            # 根据 base64 前几个字节简单判断格式
            content_type = "image/png"
            extension = "png"
            if image_base64.startswith("R0lG"):
                content_type = "image/gif"
                extension = "gif"
            elif image_base64.startswith("UklG"):
                content_type = "image/webp"
                extension = "webp"
            elif image_base64.startswith("iVBOR"):
                content_type = "image/png"
                extension = "png"
            elif image_base64.startswith("/9j/"):
                content_type = "image/jpeg"
                extension = "jpg"

            # 在发送前保存一份图片到本地
            try:
                image_bytes = base64.b64decode(image_base64)
            except Exception as decode_err:
                logger.error(f"封面数据解码失败: {decode_err}")
                return False

            if self._save_recent_covers:
                try:
                    self.__save_image_to_local(image_bytes, service.name, library['Name'], extension)
                except Exception as save_err:
                    logger.error(f"保存发送前图片失败: {str(save_err)}")

            logger.info(
                f"准备上传封面: {library['Name']} 格式={content_type} 大小={len(image_bytes) / 1024:.1f}KB"
            )
            self.__debug_log(
                f"封面上传参数：content_type={content_type} extension={extension} base64_len={len(image_base64)} bytes={len(image_bytes)}"
            )

            def _post_cover(data_text):
                return service.instance.post_data(
                    url=url,
                    data=data_text,
                    headers={"Content-Type": content_type},
                )

            res = _post_cover(image_base64)
            if not res:
                logger.warning(f"设置「{library['Name']}」封面首次上传无响应，准备重试")
                time.sleep(1)
                res = _post_cover(image_base64)
            else:
                self.__debug_log(f"封面上传响应：status={res.status_code}")

            if res and res.status_code in [200, 204]:
                self.__debug_log(f"封面上传成功：library={library['Name']} status={res.status_code}")
                return True

            if res is not None:
                err_text = ""
                try:
                    err_text = (res.text or "").strip()
                except Exception:
                    err_text = ""
                if err_text:
                    logger.error(f"设置「{library['Name']}」封面失败，错误码：{res.status_code}，响应：{err_text[:300]}")
                else:
                    logger.error(f"设置「{library['Name']}」封面失败，错误码：{res.status_code}")
            else:
                logger.error(
                    f"设置「{library['Name']}」封面失败，错误码：No response（可能是反向代理超时、连接重置或媒体服务器拒绝连接）"
                )
            return False
        except Exception as err:
            logger.error(f"设置「{library['Name']}」封面失败：{str(err)}")
        return False

    def clean_cover_history(self, save=True):
        history = self.get_data('cover_history') or []
        cleaned = []

        for item in history:
            try:
                cleaned_item = {
                    "server": item["server"],
                    "library_id": str(item["library_id"]),
                    "item_id": str(item["item_id"]),
                    "timestamp": float(item["timestamp"])
                }
                cleaned.append(cleaned_item)
            except (KeyError, ValueError, TypeError):
                # 如果字段缺失或格式错误则跳过该项
                continue

        if save:
            self.save_data('cover_history', cleaned)

        return cleaned


    def update_cover_history(self, server, library_id, item_id):
        now = time.time()
        item_id = str(item_id)
        library_id = str(library_id)

        history_item = {
            "server": server,
            "library_id": library_id,
            "item_id": item_id,
            "timestamp": now
        }

        # 原始数据
        history = self.get_data('cover_history') or []

        # 用于分组管理：(server, library_id) => list of items
        grouped = defaultdict(list)
        for item in history:
            key = (item["server"], str(item["library_id"]))
            grouped[key].append(item)

        key = (server, library_id)
        items = grouped[key]

        # 查找是否已有该 item_id
        existing = next((i for i in items if str(i["item_id"]) == item_id), None)

        if existing:
            # 若已存在且是最新的，跳过
            if existing["timestamp"] >= max(i["timestamp"] for i in items):
                return
            else:
                existing["timestamp"] = now
        else:
            items.append(history_item)

        # 排序 + 截取前9
        grouped[key] = sorted(items, key=lambda x: x["timestamp"], reverse=True)[:9]

        # 重新整合所有分组的数据
        new_history = []
        for item_list in grouped.values():
            new_history.extend(item_list)

        self.save_data('cover_history', new_history)
        return [ 
            item for item in new_history
            if str(item.get("library_id")) == str(library_id)
        ]

    def prepare_library_images(self, library_dir: str, required_items: int = 9):
        """
        准备目录下的 1~required_items.jpg 图片文件:
        1. 检查已有的目标编号文件
        2. 保留已有的文件，只补足缺失的编号
        3. 补充文件时尽量避免连续使用相同的源图片
        """
        os.makedirs(library_dir, exist_ok=True)

        required_items = max(1, int(required_items))

        # 检查哪些编号的文件已存在，哪些缺失
        existing_numbers = []
        missing_numbers = []
        for i in range(1, required_items + 1):
            target_file_path = os.path.join(library_dir, f"{i}.jpg")
            if os.path.exists(target_file_path):
                existing_numbers.append(i)
            else:
                missing_numbers.append(i)

        # 如果已经存在所有文件，直接返回
        if not missing_numbers:
            return True

        logger.info(f"信息: {library_dir} 中缺少以下编号的图片: {missing_numbers}，将进行补充。")

        target_name_pattern = rf"^[1-9][0-9]*\.jpg$"

        # 获取可用作源的图片（排除已有的目标编号文件）
        # 使用 scandir 并限制采样数量，避免超大目录扫描导致长时间无日志
        source_image_filenames = []
        max_source_scan = 512
        scanned_entries = 0
        for entry in os.scandir(library_dir):
            scanned_entries += 1
            if not entry.is_file():
                continue

            f = entry.name
            # 排除 N.jpg（N 为正整数）作为源
            if re.match(target_name_pattern, f, re.IGNORECASE):
                continue
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                source_image_filenames.append(f)
                if len(source_image_filenames) >= max_source_scan:
                    break

        if scanned_entries > 2000:
            logger.info(f"信息: {library_dir} 文件较多，已快速采样 {len(source_image_filenames)} 张作为补图源")

        # 如果没有源图片可用
        if not source_image_filenames:
            # 如果已经有部分目标编号图片，可以从这些现有文件中选择
            if existing_numbers:
                logger.info(f"信息: {library_dir} 中没有其他图片可用，将从现有目标编号图片中随机选择进行复制。")
                existing_file_paths = [os.path.join(library_dir, f"{i}.jpg") for i in existing_numbers]
                source_image_paths = existing_file_paths
            else:
                logger.info(f"警告: {library_dir} 中没有任何可用的图片来生成 1-{required_items}.jpg。")
                return False
        else:
            # 将文件名转换为完整路径
            source_image_paths = [os.path.join(library_dir, f) for f in sorted(source_image_filenames)]

        # 如果源图片数量不足，需要重复使用
        if len(source_image_paths) < len(missing_numbers):
            logger.info(f"信息: 源图片数量({len(source_image_paths)})小于缺失数量({len(missing_numbers)})，某些图片将被重复使用。")
        
        # 为每个缺失的编号选择一个源图片，尽量避免连续重复
        last_used_source = None
        for missing_num in missing_numbers:
            target_path = os.path.join(library_dir, f"{missing_num}.jpg")
            
            # 如果只有一个源文件，没有选择，直接使用
            if len(source_image_paths) == 1:
                selected_source = source_image_paths[0]
            else:
                # 尝试选择一个与上次不同的源文件
                available_sources = [s for s in source_image_paths if s != last_used_source]
                
                # 如果没有其他选择（可能上次用了唯一的源文件），则使用所有源
                if not available_sources:
                    available_sources = source_image_paths
                    
                # 随机选择一个源文件
                selected_source = random.choice(available_sources)
                
            # 记录本次使用的源文件，用于下次比较
            last_used_source = selected_source
            
            try:
                if not os.path.exists(selected_source):
                    logger.info(f"错误: 源文件 {selected_source} 在尝试复制前找不到了！")
                    return False
                    
                shutil.copy(selected_source, target_path)
                logger.info(f"信息: 已创建 {missing_num}.jpg (源自: {os.path.basename(selected_source)})")
                
            except Exception as e:
                logger.info(f"错误: 复制文件 {selected_source} 到 {target_path} 时发生错误: {e}")
                return False

        logger.info(f"信息: {library_dir} 已成功补充所有缺失的图片，现在包含完整的 1-{required_items}.jpg")
        return True

    def __get_fonts(self):
        def detect_string_type(s: str):
            if not s:
                return None
            s = s.strip()

            # 判断是否是 HTTP(S) 链接
            if re.match(r'^https?://[^\s]+$', s, re.IGNORECASE):
                return 'url'

            # 判断是否像路径（包含 / 或 \，或以 ~、.、/ 开头）
            if os.path.isabs(s) or s.startswith(('.', '~', '/')) or re.search(r'[\\/]', s):
                return 'path'

            return None
        
        font_dir_path = self._font_path
        Path(font_dir_path).mkdir(parents=True, exist_ok=True)

        _, _, zh_preset_paths, en_preset_paths = self.__get_font_presets()

        if not self._zh_font_preset:
            self._zh_font_preset = "chaohei"

        default_font_url = {
            "chaohei": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/chaohei.ttf",
            "yasong": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/yasong.ttf",
            "EmblemaOne": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/EmblemaOne.woff2",
            "Melete": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/Melete.otf",
            "Phosphate": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/phosphate.ttf",
            "JosefinSans": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/josefinsans.woff2",
            "LilitaOne": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/lilitaone.woff2",
            "Monoton": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/Monoton.woff2",
            "Plaster": "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/Plaster.woff2",
        }
        default_zh_url = default_font_url.get(self._zh_font_preset, "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/chaohei.ttf")

        if not self._en_font_preset:
            self._en_font_preset = "EmblemaOne"

        default_en_url = default_font_url.get(self._en_font_preset, "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/fonts/EmblemaOne.woff2")
        
        log_prefix = "默认"
        zh_custom_type = detect_string_type(self._zh_font_custom)
        en_custom_type = detect_string_type(self._en_font_custom)
        current_zh_font_url = self._zh_font_custom if zh_custom_type == 'url' else default_zh_url
        current_en_font_url = self._en_font_custom if en_custom_type == 'url' else default_en_url
        zh_local_path_config = self._zh_font_custom if zh_custom_type == 'path' else zh_preset_paths.get(self._zh_font_preset)
        en_local_path_config = self._en_font_custom if en_custom_type == 'path' else en_preset_paths.get(self._en_font_preset)

        downloaded_zh_font_base = f"{self._zh_font_preset}_custom" if zh_custom_type == 'url' else self._zh_font_preset
        downloaded_en_font_base = f"{self._en_font_preset}_custom" if en_custom_type == 'url' else self._en_font_preset
        hash_zh_file_name = f"{downloaded_zh_font_base}_url.hash"
        hash_en_file_name = f"{downloaded_en_font_base}_url.hash"
        final_zh_font_path_attr = "_zh_font_path"
        final_en_font_path_attr = "_en_font_path"

        logger.info(f"当前主标题字体URL: {current_zh_font_url} (本地路径: {zh_local_path_config})")

        active_fonts_to_process = [
            {
                "lang": "主标题",
                "url": current_zh_font_url,
                "local_path_config": zh_local_path_config,
                "download_base_name": downloaded_zh_font_base,
                "hash_file_name": hash_zh_file_name,
                "final_attr_name": final_zh_font_path_attr,
                "fallback_ext": ".ttf"
            },
            {
                "lang": "副标题",
                "url": current_en_font_url,
                "local_path_config": en_local_path_config,
                "download_base_name": downloaded_en_font_base,
                "hash_file_name": hash_en_file_name,
                "final_attr_name": final_en_font_path_attr,
                "fallback_ext": ".ttf"
            }
        ]


        for font_info in active_fonts_to_process:
            lang = font_info["lang"]
            url = font_info["url"]
            local_path_cfg = font_info["local_path_config"]
            download_base = font_info["download_base_name"]
            hash_filename = font_info["hash_file_name"]
            final_attr = font_info["final_attr_name"]
            fallback_ext = font_info["fallback_ext"]


            extension = self.get_file_extension_from_url(url, fallback_ext=fallback_ext)
            downloaded_font_file_path = Path(font_dir_path) / f"{download_base}{extension}"
            hash_file_path = Path(font_dir_path) / hash_filename
            
            current_font_path = None
            using_local_font = False
            if local_path_cfg:
                local_font_p = Path(local_path_cfg)
                if self.__validate_font_file(local_font_p):
                    logger.info(f"{lang}字体: 使用本地指定路径 {local_font_p}")
                    current_font_path = local_font_p
                    using_local_font = True
                else:
                    logger.warning(f"{log_prefix}{lang}字体: 本地指定路径 {local_font_p} 无效或文件不存在。")

            if not using_local_font:
                url_hash = hashlib.md5(url.encode()).hexdigest()
                url_has_changed = True
                if hash_file_path.exists():
                    try:
                        if hash_file_path.read_text() == url_hash:
                            url_has_changed = False
                    except Exception as e:
                        logger.warning(f"读取哈希文件失败 {hash_file_path}: {e}。将重新下载。")
                
                font_file_is_valid = self.__validate_font_file(downloaded_font_file_path)

                if url_has_changed or not font_file_is_valid:
                    if url_has_changed:
                        logger.info(f"{log_prefix}{lang}字体URL已更改或首次下载。")
                    if not font_file_is_valid and downloaded_font_file_path.exists():
                         logger.info(f"{log_prefix}{lang}字体文件 {downloaded_font_file_path} 无效或损坏，将重新下载。")
                    elif not downloaded_font_file_path.exists():
                         logger.info(f"{log_prefix}{lang}字体文件 {downloaded_font_file_path} 不存在，将下载。")

                    # 使用安全的字体下载方法
                    if self.download_font_safely_with_timeout(url, downloaded_font_file_path):
                        try:
                            hash_file_path.write_text(url_hash)
                        except Exception as e:
                            logger.error(f"写入哈希文件失败 {hash_file_path}: {e}")
                        current_font_path = downloaded_font_file_path
                    else:
                        logger.critical(f"无法获取必要的{log_prefix}{lang}支持字体: {url}")
                        if font_file_is_valid :
                             logger.warning(f"下载失败，但找到一个已存在的（可能旧版本）有效字体文件 {downloaded_font_file_path}，将尝试使用。")
                             current_font_path = downloaded_font_file_path
                        else:
                             current_font_path = None
                else:
                    logger.info(f"{log_prefix}{lang}字体: 使用已下载/缓存的有效字体 {downloaded_font_file_path}")
                    current_font_path = downloaded_font_file_path
            
            # 安全设置字体路径
            if current_font_path and current_font_path.exists():
                setattr(self, final_attr, current_font_path)
                status_log = '(本地路径)' if using_local_font else '(已下载/缓存)'
                logger.info(f"{log_prefix}{lang}字体最终路径: {getattr(self,final_attr)} {status_log}")
            else:
                # 字体获取失败，设置为None并记录错误
                setattr(self, final_attr, None)
                logger.error(f"{log_prefix}{lang}字体获取失败，这可能导致封面生成失败")

        # 检查是否所有必要的字体都已获取
        if not self._zh_font_path or not self._en_font_path:
            logger.critical("关键字体文件缺失，插件可能无法正常工作。请检查网络连接或手动下载字体文件。")

    def __sanitize_filename(self, filename: str) -> str:
        """
        将媒体库名称转换为安全的文件名，特别处理数字或字母开头的名称
        """
        if not filename:
            return "unknown"

        # 移除或替换不安全的字符
        import re
        # 替换Windows和Unix系统中不允许的字符
        unsafe_chars = r'[<>:"/\\|?*]'
        safe_name = re.sub(unsafe_chars, '_', filename)

        # 移除前后空格
        safe_name = safe_name.strip()

        # 如果名称为空，使用默认名称
        if not safe_name:
            return "unknown"

        # 确保不以点开头（在某些系统中是隐藏文件）
        if safe_name.startswith('.'):
            safe_name = '_' + safe_name[1:]

        # 限制长度（避免路径过长）
        if len(safe_name) > 100:
            safe_name = safe_name[:100]

        if safe_name != filename and filename not in self._sanitize_log_cache:
            self._sanitize_log_cache.add(filename)
            logger.debug(f"文件名安全化: '{filename}' -> '{safe_name}'")
        return safe_name

    def health_check(self) -> bool:
        """
        插件健康检查，确保关键组件正常
        """
        try:
            # 检查分辨率配置
            if not hasattr(self, '_resolution_config') or self._resolution_config is None:
                logger.warning("分辨率配置缺失，重新初始化")
                # 使用用户设置的分辨率，而不是硬编码的1080p
                if self._resolution == "custom":
                    self._resolution_config = self.__new_resolution_config((self._custom_width, self._custom_height))
                else:
                    self._resolution_config = self.__new_resolution_config(self._resolution)

            # 检查字体文件
            if not self._zh_font_path or not self._en_font_path:
                logger.warning("字体文件缺失，尝试重新获取")
                self.__get_fonts()

            # 验证字体文件有效性
            if self._zh_font_path and not self.__validate_font_file(Path(self._zh_font_path)):
                logger.warning("主标题字体文件无效，尝试重新下载")
                return False

            if self._en_font_path and not self.__validate_font_file(Path(self._en_font_path)):
                logger.warning("副标题字体文件无效，尝试重新下载")
                return False

            logger.info("插件健康检查通过")
            return True

        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return False

    def download_font_safely_with_timeout(self, font_url: str, font_path: Path, timeout: int = 60) -> bool:
        """
        带超时的安全字体下载方法，避免首次下载时阻塞过久
        """
        try:
            logger.info(f"开始下载字体（超时限制: {timeout}秒）: {font_url}")
            return self.download_font_safely(font_url, font_path, retries=1, timeout=timeout)

        except Exception as e:
            logger.error(f"字体下载过程中出现异常: {e}")
            return False

    def download_font_safely(self, font_url: str, font_path: Path, retries: int = 2, timeout: int = 30):
        """
        从链接下载字体文件到指定目录，使用优化的网络助手
        :param font_url: 字体文件URL
        :param font_path: 保存路径
        :param retries: 每种策略的最大重试次数（减少重试次数）
        :param timeout: 下载超时时间
        :return: 是否下载成功
        """
        logger.info(f"准备下载字体: {font_url} -> {font_path}")

        # 确保在开始下载前删除任何可能存在的损坏文件
        if font_path.exists():
            try:
                font_path.unlink()
                logger.info(f"删除之前的字体文件以便重新下载: {font_path}")
            except OSError as unlink_error:
                logger.error(f"无法删除现有字体文件 {font_path}: {unlink_error}")
                return False
        
        # 准备下载策略
        strategies = []

        # 判断是否为GitHub链接
        is_github_url = "github.com" in font_url or "raw.githubusercontent.com" in font_url

        # 对于GitHub链接，优先使用GitHub镜像站
        if is_github_url and settings.GITHUB_PROXY:
            github_proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{font_url}"
            strategies.append(("GitHub镜像站", github_proxy_url))

        # 直接使用原始URL
        strategies.append(("直连", font_url))

        # 遍历所有策略
        for strategy_name, target_url in strategies:
            logger.info(f"尝试使用策略：{strategy_name} 下载字体: {target_url}")

            # 创建临时文件路径
            temp_path = font_path.with_suffix('.temp')

            try:
                response = requests.get(
                    target_url,
                    timeout=timeout,
                    headers={'User-Agent': 'MoviePilot-WsEmbyCover/1.3'},
                    stream=True,
                    verify=False if strategy_name == "GitHub镜像站" else True,
                )
                if response.status_code == 200:
                    temp_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(temp_path, "wb") as f:
                        f.write(response.content)
                    if self.__validate_font_file(temp_path):
                        temp_path.replace(font_path)
                        logger.info(f"字体下载成功: 使用策略 {strategy_name}")
                        return True
                    logger.warning("下载的字体文件验证失败，可能已损坏")
                    if temp_path.exists():
                        temp_path.unlink()
                else:
                    logger.warning(f"策略 {strategy_name} 下载失败，HTTP状态码: {response.status_code}")

            except Exception as e:
                logger.warning(f"策略 {strategy_name} 下载出错: {e}")
                # 清理可能的临时文件
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        
        # 所有策略都失败
        logger.error(f"所有下载策略均失败，无法下载字体，建议手动下载字体: {font_url}")
        # 确保目标路径没有损坏的文件
        if font_path.exists():
            try:
                font_path.unlink()
                logger.info(f"已删除部分下载的文件: {font_path}")
            except OSError as unlink_error:
                logger.error(f"无法删除部分下载的文件 {font_path}: {unlink_error}")
        
        return False

    def get_file_extension_from_url(self, url: str, fallback_ext: str = ".ttf") -> str:
        """
        从链接获取字体扩展名扩展名
        """
        try:
            parsed_url = urlparse(url)
            path_part = parsed_url.path
            if path_part:
                filename = os.path.basename(path_part)
                _ , ext = os.path.splitext(filename)
                return ext if ext else fallback_ext
            else:
                logger.warning(f"无法从URL中提取路径部分: {url}. 使用备用扩展名: {fallback_ext}")
                return fallback_ext
        except Exception as e:
            logger.error(f"解析URL时出错 '{url}': {e}. 使用备用扩展名: {fallback_ext}")
            return fallback_ext
        
    def _validate_font_file(self, font_path: Path):
        if not font_path or not font_path.exists() or not font_path.is_file():
            return False
        
        try:
            with open(font_path, "rb") as f:
                header = f.read(4) 
                if (header.startswith(b'\x00\x01\x00\x00') or
                    header.startswith(b'OTTO') or
                    header.startswith(b'true') or
                    header.startswith(b'wOFF') or
                    header.startswith(b'wOF2')):
                    return True
                if font_path.suffix.lower() == ".svg":
                    f.seek(0)
                    sample = f.read(100).decode(errors='ignore').strip()
                    if sample.startswith('<svg') or sample.startswith('<?xml'):
                        return True
                if font_path.suffix.lower() == ".bdf":
                    f.seek(0)
                    sample = f.read(9).decode(errors='ignore')
                    if sample == "STARTFONT":
                        return True
            logger.warning(f"字体文件存在但可能已损坏或格式无法识别: {font_path}")
            return False
        except Exception as e:
            logger.warning(f"验证字体文件时出错 {font_path}: {e}")
            return False

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")
