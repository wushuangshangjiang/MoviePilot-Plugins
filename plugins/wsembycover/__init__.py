import base64
import datetime
import gc
import hashlib
import importlib
import mimetypes
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
from urllib.parse import urlparse, quote, unquote
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
    # 鎻掍欢鍚嶇О
    plugin_name = "无双Emby封面"
    # 鎻掍欢鎻忚堪
    plugin_desc = "生成媒体库动态/静态封面，支持 Emby/Jellyfin"
    # 鎻掍欢鍥炬爣
    plugin_icon = "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/icons/emby.png"
    # 鎻掍欢鐗堟湰
    plugin_version = "1.71"
    # 鎻掍欢浣滆€?
    plugin_author = "wushuangshangjiang"
    # 浣滆€呬富椤?
    author_url = "https://github.com/wushuangshangjiang/MoviePilot-Plugins"
    # 鎻掍欢閰嶇疆椤笽D鍓嶇紑
    plugin_config_prefix = "wsembycover_"
    # 鍔犺浇椤哄簭
    plugin_order = 2
    # 鍙娇鐢ㄧ殑鐢ㄦ埛绾у埆
    auth_level = 1

    # 閫€鍑轰簨浠?
    _event = threading.Event()

    # 绉佹湁灞炴€?
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
    _page_tab = "generate-tab"
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

            # 鏍峰紡鍛藉悕鍗囩骇鍏煎锛堜粎瀵规棫閰嶇疆鎵ц涓€娆¤縼绉伙級
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
            self._page_tab = config.get("page_tab", "generate-tab")
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

        # 鍒濆鍖栧垎杈ㄧ巼閰嶇疆锛堢‘淇濆畨鍏ㄥ垵濮嬪寲锛?
        try:
            self._resolution_config = self.__new_resolution_config(self._resolution)
        except Exception as e:
            logger.warning(f"鍒嗚鲸鐜囬厤缃垵濮嬪寲澶辫触锛屼娇鐢ㄩ粯璁ら厤缃? {e}")
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
            logger.info("鏈厤缃彲鐢ㄥ獟浣撴湇鍔″櫒")
        
        # 鍋滄鐜版湁浠诲姟
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
            logger.info("媒体库封面更新服务启动，立即运行一次")
            # 鍏抽棴涓€娆℃€у紑鍏?
            self._update_now = False
            # 淇濆瓨閰嶇疆
            self.__update_config()
            # 鍚姩鏈嶅姟
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __clamp_value(self, value, minimum, maximum, default_value, name, cast_type):
        try:
            parsed = cast_type(value)
        except (ValueError, TypeError):
            logger.warning(f"{name} 閰嶇疆鍊奸潪娉?({value})锛屽凡鍥為€€榛樿鍊?{default_value}")
            return default_value

        if parsed < minimum or parsed > maximum:
            clamped = max(minimum, min(maximum, parsed))
            logger.warning(f"{name} 閰嶇疆鍊艰秴鍑鸿寖鍥?({parsed})锛屽凡闄愬埗涓?{clamped}")
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
            logger.error(f"鏈嶅姟鍣ㄩ厤缃В鏋愬け璐? {e}")
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
                    host=str(value.get("host", "") or value.get("鍦板潃", "")).strip(),
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
            # 鏂版牸寮忥細
            # 鏈嶅姟鍣?:
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
            # 鍏煎鍒楄〃涓殑鏂版牸寮忓崟椤癸細{鏈嶅姟鍣ㄥ悕: [host, apikey]}
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
        return '''# 閰嶇疆灏侀潰鏍囬锛堟敮鎸佹寜鏈嶅姟鍣ㄥ垎缁勶級
# 鎺ㄨ崘鏍煎紡锛堟寜鏈嶅姟鍣ㄥ垎缁勶級锛?
#
# 鏈嶅姟鍣?:
#   濯掍綋搴撳悕绉?
#     - 涓绘爣棰?
#     - 鍓爣棰?
#   鍙︿竴涓獟浣撳簱:
#     - 涓绘爣棰?
#     - 鍓爣棰?
#
# 鍏煎鏃ф牸寮忥紙涓嶅垎鏈嶅姟鍣級锛?
# 濯掍綋搴撳悕绉?
#   - 涓绘爣棰?
#   - 鍓爣棰?
#   - "#FF5722"  # 鑳屾櫙棰滆壊锛堝彲閫夛紝蹇呴』鍔犲紩鍙凤級
#
'''

    @staticmethod
    def __default_servers_config_template() -> str:
        return '''# 閰嶇疆澶氭湇鍔″櫒
# 鏍煎紡濡備笅锛?
#
# 鏈嶅姟鍣?:
#   - http://127.0.0.1:8096
#   - xxxxx
# 鏈嶅姟鍣?:
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
            "page_tab": self._page_tab,
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
            {"title": "娼粦", "value": "chaohei", "aliases": ["chaohei", "wendao", "娼粦", "chao_hei"]},
            {"title": "雅宋", "value": "yasong", "aliases": ["yasong", "雅宋", "multi_1_zh", "ya_song"]},
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
                    logger.warning(f"娓呯悊鍥剧墖澶辫触 {entry}: {e}")
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
                logger.warning(f"娓呯悊瀛椾綋澶辫触 {entry}: {e}")
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
        return [
            {
                "path": "/clean_cache",
                "endpoint": self.api_clean_cache,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "立即清理全部缓存（图片+字体）",
            },
            {
                "path": "clean_cache",
                "endpoint": self.api_clean_cache,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "立即清理全部缓存（兼容路由）",
            },
            {
                "path": "/clean_images",
                "endpoint": self.api_clean_images,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理图片缓存",
            },
            {
                "path": "clean_images",
                "endpoint": self.api_clean_images,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理图片缓存（兼容路由）",
            },
            {
                "path": "/clean_fonts",
                "endpoint": self.api_clean_fonts,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理字体缓存",
            },
            {
                "path": "clean_fonts",
                "endpoint": self.api_clean_fonts,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "立即清理字体缓存（兼容路由）",
            },
            {
                "path": "/delete_saved_cover",
                "endpoint": self.api_delete_saved_cover,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "删除单张已保存封面",
            },
            {
                "path": "delete_saved_cover",
                "endpoint": self.api_delete_saved_cover,
                "auth": "bear",
                "methods": ["POST", "GET"],
                "summary": "删除单张已保存封面（兼容路由）",
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
                "summary": "立即生成媒体库封面（兼容路由）",
            },
        ]

    def api_clean_images(self):
        try:
            logger.info("[WsEmbyCover] 收到立即清理图片缓存请求")
            self.__clean_generated_images()
            self._clean_images = False
            self.__update_config()
            return {"code": 0, "msg": "鍥剧墖缂撳瓨娓呯悊瀹屾垚"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戠珛鍗虫竻鐞嗗浘鐗囧け璐? {e}", exc_info=True)
            return {"code": 1, "msg": f"鍥剧墖缂撳瓨娓呯悊澶辫触: {e}"}

    def api_clean_fonts(self):
        try:
            logger.info("[WsEmbyCover] 收到立即清理字体缓存请求")
            self.__clean_downloaded_fonts()
            self._clean_fonts = False
            self.__update_config()
            return {"code": 0, "msg": "瀛椾綋缂撳瓨娓呯悊瀹屾垚"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戠珛鍗虫竻鐞嗗瓧浣撳け璐? {e}", exc_info=True)
            return {"code": 1, "msg": f"瀛椾綋缂撳瓨娓呯悊澶辫触: {e}"}

    def api_clean_cache(self):
        try:
            logger.info("[WsEmbyCover] 收到立即清理全部缓存请求（图片+字体）")
            self.__clean_generated_images()
            self.__clean_downloaded_fonts()
            self._clean_images = False
            self._clean_fonts = False
            self.__update_config()
            return {"code": 0, "msg": "缓存清理完成（图片+字体）"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戠珛鍗虫竻鐞嗗叏閮ㄧ紦瀛樺け璐? {e}", exc_info=True)
            return {"code": 1, "msg": f"缂撳瓨娓呯悊澶辫触: {e}"}

    def api_delete_saved_cover(self, file: str = ""):
        try:
            target_file = self.__resolve_saved_cover_path(file)
            if not target_file:
                return {"code": 1, "msg": "鏃犳晥鏂囦欢璺緞"}
            if not target_file.exists() or not target_file.is_file():
                return {"code": 1, "msg": "文件不存在"}
            target_file.unlink(missing_ok=True)
            logger.info(f"銆怶sEmbyCover銆戝凡鍒犻櫎灏侀潰鏂囦欢: {target_file}")
            return {"code": 0, "msg": "灏侀潰鏂囦欢鍒犻櫎鎴愬姛"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戝垹闄ゅ皝闈㈡枃浠跺け璐? {e}", exc_info=True)
            return {"code": 1, "msg": f"灏侀潰鏂囦欢鍒犻櫎澶辫触: {e}"}

    def api_generate_now(self, style: str = ""):
        old_style = self._cover_style
        try:
            if not self._enabled:
                logger.warning("[WsEmbyCover] 立即生成失败：插件未启用，请先在设置页启用并保存")
                return {"code": 1, "msg": "插件未启用，请先在设置页启用并保存"}
            if not self._servers:
                logger.warning("銆怶sEmbyCover銆戠珛鍗崇敓鎴愬け璐ワ細鏈厤缃獟浣撴湇鍔″櫒锛岃鍏堝湪璁剧疆椤靛～鍐欏苟淇濆瓨")
                return {"code": 1, "msg": "鏈厤缃獟浣撴湇鍔″櫒锛岃鍏堝湪璁剧疆椤靛～鍐欏苟淇濆瓨"}

            target_style = (style or "").strip()
            allowed_styles = {
                "static_1", "static_2",
            }
            if target_style:
                if target_style not in allowed_styles:
                    return {"code": 1, "msg": f"涓嶆敮鎸佺殑椋庢牸: {target_style}"}
                self._cover_style = target_style
            logger.info(f"銆怶sEmbyCover銆戞敹鍒扮珛鍗崇敓鎴愯姹傦紝椋庢牸: {self._cover_style}")
            tips = self.__update_all_libraries()
            return {"code": 0, "msg": tips or "封面生成任务已完成"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戠珛鍗崇敓鎴愬け璐? {e}", exc_info=True)
            return {"code": 1, "msg": f"灏侀潰鐢熸垚澶辫触: {e}"}
        finally:
            self._cover_style = old_style

    def __set_page_tab(self, tab: str):
        self._page_tab = tab if tab in ["generate-tab", "history-tab", "clean-tab"] else "generate-tab"
        logger.info(f"銆怶sEmbyCover銆戝凡鍒囨崲椤甸潰Tab: {self._page_tab}")

    def api_set_page_tab_generate(self):
        self.__set_page_tab("generate-tab")
        return {"code": 0, "msg": "宸插垏鎹㈠埌灏侀潰鐢熸垚"}

    def api_set_page_tab_history(self):
        self.__set_page_tab("history-tab")
        return {"code": 0, "msg": "宸插垏鎹㈠埌鍘嗗彶灏侀潰"}

    def api_set_page_tab_clean(self):
        self.__set_page_tab("clean-tab")
        return {"code": 0, "msg": "宸插垏鎹㈠埌娓呯悊缂撳瓨"}

    def api_set_generate_style(self, style: str = ""):
        try:
            target_style = str(style or "").strip()
            if target_style not in {"static_1", "static_2"}:
                return {"code": 1, "msg": f"涓嶆敮鎸佺殑椋庢牸: {target_style}"}
            self._cover_style = target_style
            self._cover_style_base = target_style
            self._active_server_style = target_style
            self.__sync_profile_styles_with_selected_style()
            self.__update_config()
            logger.info(f"銆怶sEmbyCover銆戠敓鎴愰〉宸插垏鎹㈤鏍? {target_style}")
            return {"code": 0, "msg": f"宸插垏鎹㈠埌 {target_style}"}
        except Exception as e:
            logger.error(f"銆怶sEmbyCover銆戠敓鎴愰〉鍒囨崲椋庢牸澶辫触: {e}", exc_info=True)
            return {"code": 1, "msg": f"鍒囨崲澶辫触: {e}"}

    def api_saved_cover_image(self, file: str = ""):
        target_file = self.__resolve_saved_cover_path(file)
        if not target_file or not target_file.exists() or not target_file.is_file():
            return {"code": 1, "msg": "图片不存在"}
        mime_type, _ = mimetypes.guess_type(str(target_file))
        if not mime_type:
            mime_type = "image/jpeg"
        try:
            from fastapi.responses import FileResponse
            return FileResponse(path=str(target_file), media_type=mime_type)
        except Exception:
            try:
                from starlette.responses import FileResponse
                return FileResponse(path=str(target_file), media_type=mime_type)
            except Exception as e:
                logger.error(f"銆怶sEmbyCover銆戣繑鍥炲浘鐗囧け璐? {e}")
                return {"code": 1, "msg": "杩斿洖鍥剧墖澶辫触"}

    def get_service(self) -> List[Dict[str, Any]]:
        """
        娉ㄥ唽鎻掍欢鍏叡鏈嶅姟
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
        
        # 鎬绘槸鏄剧ず鍋滄鎸夐挳锛屼互渚夸腑鏂暱鏃堕棿杩愯鐨勪换鍔?
        services.append({
            "id": "StopWsEmbyCover",
            "name": "鍋滄褰撳墠鏇存柊浠诲姟",
            "trigger": None,
            "func": self.stop_task,
            "kwargs": {}
        })
        return services

    def stop_task(self):
        """
        鎵嬪姩鍋滄褰撳墠姝ｅ湪鎵ц鐨勪换鍔?
        """
        if not self._event.is_set():
            logger.info("姝ｅ湪鍙戦€佸仠姝换鍔′俊鍙?..")
            self._event.set()
            return True, "已发送停止信号，请等待当前任务清理完成"
        return True, "任务已处于停止状态或正在停止中"

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        鎷艰鎻掍欢閰嶇疆椤甸潰
        """
        # 姣忔鐢ㄦ埛鎵撳紑鎻掍欢璁剧疆椤甸潰鏃讹紝寮哄埗閲嶇疆鍥炲皝闈㈢敓鎴愰〉绛撅紝婊¤冻涓嶈蹇嗛〉绛剧殑闇€姹?
        self._page_tab = "generate-tab"
        
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
                                    'label': '澶氭湇鍔″櫒閰嶇疆',
                                    'placeholder': self.__default_servers_config_template()
                                 }
                             }
                         ]
                     },
                ]
            },
        ]

        # 鏍囬閰嶇疆
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
                                    'label': '涓嫳鏍囬閰嶇疆',
                                    'placeholder': '''鏈嶅姟鍣?:
  鍔ㄧ敾鐢靛奖:
    - 鍔ㄧ敾鐢靛奖
    - ANI MOVIE
  鍗庤鐢靛奖:
    - 鍗庤鐢靛奖
    - CHN MOVIE'''
                                 }
                             }
                         ]
                     },
                ]
            },
        ]

        # 鍏朵粬璁剧疆鏍囩
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
                                    'text': '自定义图片目录：请将图片放在与媒体库同名的子目录中，例如 /mnt/custom_images/华语电影/1.jpg；此处填写 /mnt/custom_images 即可。'
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
                                    'label': '鑷畾涔夊浘鐗囩洰褰曪紙鍙€夛級',
                                    'prependInnerIcon': 'mdi-file-image',
                                    'hint': '浣跨敤浣犳寚瀹氱殑鍥剧墖鐢熸垚灏侀潰锛屽浘鐗囨斁鍦ㄤ笌濯掍綋搴撳悓鍚嶇殑鏂囦欢澶逛笅',
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
                                    'label': '鍘嗗彶灏侀潰淇濆瓨鐩綍锛堝彲閫夛級',
                                    'prependInnerIcon': 'mdi-file-image',
                                    'hint': '鐢熸垚鐨勫皝闈㈤粯璁や繚瀛樺湪鏈彃浠舵暟鎹洰褰曚笅',
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
                                    'label': '淇濆瓨鏈€杩戠敓鎴愮殑灏侀潰',
                                    'hint': '榛樿寮€鍚紝淇濆瓨鍘嗗彶灏侀潰',
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
                                    'hint': '鍗曚釜濯掍綋搴撳皝闈繚鐣欎笂闄愶紝榛樿 10',
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
                                    'label': '鍘嗗彶灏侀潰鏄剧ず鏁伴噺',
                                    'prependInnerIcon': 'mdi-image-multiple-outline',
                                    'hint': '鍘嗗彶灏侀潰銆屾樉绀烘暟閲忋€嶏紝榛樿 50',
                                    'persistentHint': True
                                },
                            }
                        ]
                    }
                ]
            },
            
        ]
        # 鏇村鍙傛暟鏍囩
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
                                    'text': '字体设置为可选项。若字体无法下载，可手动下载并填写本地路径；主标题和副标题可使用不同字体。'
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
                                    'label': '鑷畾涔変富鏍囬瀛椾綋',
                                    'prependInnerIcon': 'mdi-ideogram-cjk',
                                    'placeholder': '鐣欑┖浣跨敤棰勮瀛椾綋',
                                    'hint': '瀛椾綋閾炬帴 / 璺緞',
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
                                    'label': '鑷畾涔夊壇鏍囬瀛椾綋',
                                    'prependInnerIcon': 'mdi-format-font',
                                    'placeholder': '鐣欑┖浣跨敤棰勮瀛椾綋',
                                    'hint': '瀛椾綋閾炬帴 / 璺緞',
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
                                    'label': '主标题字号',
                                    'prependInnerIcon': 'mdi-format-size',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
                                    'hint': '鏍规嵁鑷繁鍠滃ソ璁剧疆锛岄粯璁?180',
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
                                    'label': '副标题字号',
                                    'prependInnerIcon': 'mdi-format-size',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
                                    'hint': '鏍规嵁鑷繁鍠滃ソ璁剧疆锛岄粯璁?75',
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
                                    'label': '鑳屾櫙妯＄硦灏哄',
                                    'prependInnerIcon': 'mdi-blur',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
                                    'hint': '鏁板瓧瓒婂ぇ瓒婃ā绯婏紝榛樿 50',
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
                                    'label': '鑳屾櫙棰滆壊娣峰悎鍗犳瘮',
                                    'prependInnerIcon': 'mdi-format-color-fill',
                                    'placeholder': '鐣欑┖浣跨敤棰勮鍗犳瘮',
                                    'hint': '棰滆壊鎵€鍗犵殑姣斾緥锛?-1锛岄粯璁?0.8',
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
                                    'label': '鏍囬鏁翠綋缂╂斁',
                                    'prependInnerIcon': 'mdi-arrow-expand-all',
                                    'placeholder': '鐣欑┖浣跨敤棰勮姣斾緥',
                                    'hint': '以 1080p 为基准，1.0 为默认值',
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
                                    'label': '涓绘爣棰樺亸绉婚噺',
                                    'prependInnerIcon': 'mdi-arrow-up-down',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
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
                                    'label': '涓诲壇鏍囬闂磋窛',
                                    'prependInnerIcon': 'mdi-arrow-up-down',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
                                    'hint': '澶т簬 0锛岄粯璁?40',
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
                                    'label': '鍓爣棰樿闂磋窛',
                                    'prependInnerIcon': 'mdi-format-line-height',
                                    'placeholder': '鐣欑┖浣跨敤棰勮灏哄',
                                    'hint': '澶т簬 0锛岄粯璁?40',
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
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_host", "label": "鏈嶅姟鍣ㄥ湴鍧€"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": f"{key}_api_key", "label": "API Key"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [{"component": "VSelect", "props": {"model": f"{key}_style", "label": "椋庢牸", "items": [{"title": "style1", "value": "static_1"}, {"title": "style2", "value": "static_2"}]}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSelect", "props": {"model": f"{key}_sort_by", "label": "封面来源排序", "items": [{"title": "随机", "value": "Random"}, {"title": "最新入库", "value": "DateCreated"}, {"title": "最新发布", "value": "PremiereDate"}]}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_covers_input", "label": "鑷畾涔夊浘鐗囩洰褰曪紙鍙€夛級"}}]},
                                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": f"{key}_covers_output", "label": "鍘嗗彶灏侀潰淇濆瓨鐩綍锛堝彲閫夛級"}}]},
                                    ],
                                },
                                {
                                    "component": "VAceEditor",
                                    "props": {
                                        "modelvalue": f"{key}_title_config",
                                        "lang": "yaml",
                                        "theme": "monokai",
                                        "style": "height: 16rem",
                                        "label": "灏侀潰鏍囬锛堣鏈嶅姟鍣ㄧ嫭绔嬶級",
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
                'text': '静态',
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
                'text': '动态',
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

        # 灏侀潰椋庢牸璁剧疆鏍囩
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
                                'text': '鍩烘湰鍙傛暟'
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
                                                                'text': '海报图',
                                                            },
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': False,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '背景图',
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VLabel',
                                                        'props': {
                                                            'class': 'text-caption text-medium-emphasis mt-1 d-inline-block'
                                                        }
                                                        ,
                                                        'text': '閫夊浘浼樺厛鏉ユ簮'
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
                                                                'text': '妯＄硦鑳屾櫙'
                                                            },
                                                            {
                                                                'component': 'VBtn',
                                                                'props': {
                                                                    'value': False,
                                                                    'variant': 'outlined',
                                                                    'color': 'primary',
                                                                    'class': 'text-none'
                                                                },
                                                                'text': '绾壊娓愬彉'
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'VLabel',
                                                        'props': {
                                                            'class': 'text-caption text-medium-emphasis mt-1 d-inline-block'
                                                        }
                                                        ,
                                                        'text': '针对九宫格海报',
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
                                                            'label': '闈欐€佸垎杈ㄧ巼',
                                                            'prependInnerIcon': 'mdi-monitor-screenshot',
                                                            'items': [
                                                                {'title': '1080p (1920x1080)', 'value': '1080p'},
                                                                {'title': '720p (1280x720)', 'value': '720p'},
                                                                {'title': '480p (854x480)', 'value': '480p'}
                                                            ],
                                                            'hint': '鍔ㄦ€佸垎杈ㄧ巼榛樿320*180',
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
                                                        'text': '鑳屾櫙棰滆壊璁剧疆锛堝叏閮ㄩ鏍肩敓鏁堬級'
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
                                                                                    'label': '鑳屾櫙棰滆壊鏉ユ簮',
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
                                                                                    'label': '鑷畾涔夎儗鏅壊',
                                                                                    'prependInnerIcon': 'mdi-eyedropper',
                                                                                    'placeholder': '#FF5722',
                                                                                    'hint': '鏀寔 #鍗佸叚杩涘埗銆乺gb(...)銆侀鑹茶嫳鏂囧悕',
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
                                                    'md': 2
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
                                                    'md': 2
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
                                                    'md': 2
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
                                                    'md': 2
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
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 2
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VBtn',
                                                        'props': {
                                                            'color': 'error',
                                                            'variant': 'flat',
                                                            'prepend-icon': 'mdi-broom',
                                                            'class': 'text-none w-100',
                                                            'type': 'button'
                                                        },
                                                        'text': '立即清理缓存',
                                                        'events': {
                                                            'click': {
                                                                'api': 'plugin/WsEmbyCover/clean_cache',
                                                                'method': 'get'
                                                            }
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
                                                            'placeholder': '5位cron表达式',
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
                                                                {"title": "最新发布", "value": "PremiereDate"}
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
            "clean_images": self._clean_images,
            "clean_fonts": self._clean_fonts,
            "save_recent_covers": self._save_recent_covers,
            "debug_mode": bool(self._debug_mode),
            "debug_show_apikey": bool(self._debug_mode),
            "covers_history_limit_per_library": self._covers_history_limit_per_library,
            "covers_page_history_limit": self._covers_page_history_limit,
            "page_tab": "generate-tab",
            "style_naming_v2": True,
            **profile_defaults,
        }

    def get_page(self) -> List[dict]:
        pass

    @staticmethod
    def __style_preview_src(index: int) -> str:

        safe_index = max(1, min(2, int(index)))
        preview_map = {
            1: "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/images/style_3.jpeg?v=20260407-130",
            2: "https://raw.githubusercontent.com/wushuangshangjiang/MoviePilot-Plugins/main/images/style_5_preview.jpg?v=20260407-248",
        }
        return preview_map.get(safe_index, preview_map[1])

    def __get_recent_generated_covers(self, limit: int = 20) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        cover_dirs: List[Path] = []

        if self._covers_output:
            cover_dirs.append(Path(self._covers_output))
        data_path = self.get_data_path()
        default_output = data_path / "output"
        if default_output.exists():
            cover_dirs.append(default_output)

        allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".apng", ".webp"}
        seen = set()
        for directory in cover_dirs:
            key = str(directory)
            if key in seen:
                continue
            seen.add(key)
            if not directory.exists() or not directory.is_dir():
                continue
            for file_path in directory.iterdir():
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in allowed_ext:
                    continue
                try:
                    stat = file_path.stat()
                    
                    try:
                        from PIL import Image
                        from io import BytesIO
                        import base64
                        
                        # 鍔ㄦ€佺敓鎴愮缉鐣ュ浘杩涜 Base64 浼犺緭
                        # 1. 褰诲簳缁曞紑 /api/v1/plugin 澶栭儴鎺ュ彛瀛樺湪鐨?401 閴存潈闂
                        # 2. 灏嗗嚑鍗?MB 鐨勫姩鍥惧帇缂╀负浜嗗嚑鍗?KB 鐨勭缉鐣ュ浘锛岃В鍐冲墠绔姞杞藉崱姝婚棶棰?
                        with Image.open(file_path) as img:
                            if hasattr(img, 'is_animated') and img.is_animated:
                                img.seek(0)
                                
                            thumb = img.copy()
                            if thumb.mode != 'RGB':
                                thumb = thumb.convert('RGB')
                                
                            thumb.thumbnail((480, 270))
                            buf = BytesIO()
                            thumb.save(buf, format="JPEG", quality=75)
                            image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                            image_src = f"data:image/jpeg;base64,{image_b64}"
                            
                    except Exception as img_err:
                        logger.debug(f"鐢熸垚缂╃暐鍥惧け璐?{file_path}: {img_err}")
                        continue

                    items.append(
                        {
                            "name": file_path.name,
                            "path": str(file_path),
                            "mtime_ts": float(stat.st_mtime),
                            "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "size": self.__format_size(stat.st_size),
                            "src": image_src,
                        }
                    )
                except Exception as e:
                    logger.debug(f"璇诲彇灏侀潰鏂囦欢淇℃伅澶辫触: {file_path} -> {e}")

        items.sort(key=lambda x: x.get("mtime_ts", 0.0), reverse=True)
        return items[:max(1, int(limit))]

    @staticmethod
    def __format_size(size_bytes: int) -> str:
        try:
            size = float(size_bytes)
        except (TypeError, ValueError):
            return "0 B"
        units = ["B", "KB", "MB", "GB"]
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
            size /= 1024
        return f"{int(size_bytes)} B"

    def __get_saved_cover_dirs(self) -> List[Path]:
        result: List[Path] = []
        if self._covers_output:
            result.append(Path(self._covers_output))
        data_path = self.get_data_path()
        default_output = data_path / "output"
        result.append(default_output)
        unique: List[Path] = []
        seen = set()
        for directory in result:
            key = str(directory)
            if key in seen:
                continue
            seen.add(key)
            unique.append(directory)
        return unique

    def __resolve_saved_cover_path(self, raw_path: str) -> Optional[Path]:
        if not raw_path:
            return None
        decoded = unquote(str(raw_path)).strip()
        target = Path(decoded).expanduser()
        if not target.is_absolute():
            return None
        allowed_dirs = self.__get_saved_cover_dirs()
        for directory in allowed_dirs:
            try:
                root = directory.resolve()
                file_path = target.resolve()
                if str(file_path).startswith(str(root) + os.sep) or file_path == root:
                    return file_path
            except Exception:
                continue
        return None

    def __get_recent_cover_output_dir(self) -> Path:
        if self._covers_output:
            return Path(self._covers_output).expanduser()
        return self.get_data_path() / "output"

    @eventmanager.register(EventType.PluginAction)
    def update_covers(self, event: Event):
        """
        杩滅▼鍏ㄩ噺鍚屾
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "update_covers":
                return
            self.post_message(
                channel=event.event_data.get("channel"),
                title="寮€濮嬫洿鏂板獟浣撳簱灏侀潰 ...",
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
        濯掍綋鏁寸悊瀹屾垚鍚庯紝鏇存柊鎵€鍦ㄥ簱灏侀潰
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

        # logger.info(f"杞Щ淇℃伅锛歿transfer}")
        # logger.info(f"鍏冩暟鎹細{meta}")
        # logger.info(f"濯掍綋淇℃伅锛歿mediainfo}")
        # logger.info(f"鐩戞帶鍒扮殑濯掍綋淇℃伅锛歿mediainfo}")
        if not mediainfo:
            return
            
        # 寮€濮嬪墠娓呯悊鍙兘閬楃暀鐨勫仠姝俊鍙凤紝闃叉闃诲鐩戞帶
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
                logger.warning(f"{mediainfo.title_year} 涓嶅瓨鍦ㄥ獟浣撳簱涓紝鍙兘鏈嶅姟鍣ㄨ繕鏈壂鎻忓畬鎴愶紝寤鸿璁剧疆鍚堥€傜殑寤惰繜鏃堕棿")
                return
        
        # Get item details including backdrop
        iteminfo = self.mschain.iteminfo(server=existsinfo.server, item_id=existsinfo.itemid)
        # logger.info(f"鑾峰彇鍒板獟浣撻」 {mediainfo.title_year} 璇︽儏锛歿iteminfo}")
        if not iteminfo:
            logger.warning(f"鑾峰彇 {mediainfo.title_year} 璇︽儏澶辫触")
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
            logger.warning(f"鎵句笉鍒?{mediainfo.title_year} 鎵€鍦ㄥ獟浣撳簱")
            return
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")

        update_key = (server, item_id)
        if update_key in self._current_updating_items:
            logger.info(f"媒体库 {server}：{library['Name']} 的项目 {mediainfo.title_year} 正在更新中，跳过本次更新")
            return
        # self.clean_cover_history(save=True)
        old_history = self.get_data('cover_history') or []
        # 鏂板鍘婚噸鍒ゆ柇閫昏緫
        latest_item = max(
            (item for item in old_history if str(item.get("library_id")) == str(library_id)),
            key=lambda x: x["timestamp"],
            default=None
        )
        if latest_item and str(latest_item.get("item_id")) == str(item_id):
            logger.info(f"濯掍綋 {mediainfo.title_year} 鍦ㄥ簱涓槸鏈€鏂拌褰曪紝涓嶆洿鏂板皝闈㈠浘")
            return
        
        # 瀹夊叏鍦拌幏鍙栧瓧浣撳拰缈昏瘧
        try:
            self.__get_fonts()
        except Exception as e:
            logger.error(f"鍒濆鍖栧瓧浣撴垨缈昏瘧鏃跺嚭閿? {e}")
            # 缁х画鎵ц锛屼絾鍙兘浼氬奖鍝嶅皝闈㈢敓鎴愯川閲?
        new_history = self.update_cover_history(
            server=server, 
            library_id=library_id, 
            item_id=item_id
        )
        # logger.info(f"鏈€鏂版暟鎹細 {new_history}")
        self._monitor_sort = 'DateCreated'
        self._current_updating_items.add(update_key)
        if self.__update_library(service, library):
            self._monitor_sort = ''
            self._current_updating_items.remove(update_key)
            logger.info(f"媒体库 {server}：{library['Name']} 封面更新成功")

    
    def __update_all_libraries(self):
        """
        鏇存柊鎵€鏈夊獟浣撳簱灏侀潰
        """
        if not self._enabled:
            return
        # 鎵€鏈夊獟浣撴湇鍔″櫒
        if not self._servers:
            return
        logger.info("寮€濮嬫鏌ュ瓧浣?...")
        try:
            self.__get_fonts()
        except Exception as e:
            logger.error(f"鍒濆鍖栬繃绋嬩腑鍑洪敊: {e}")
            logger.warning("将尝试继续执行，但可能影响封面生成质量")
        logger.info("寮€濮嬫洿鏂板獟浣撳簱灏侀潰 ...")
        self.__debug_log(f"璋冭瘯妯″紡寮€鍚細selected_libraries={self._selected_libraries}")
        # 寮€濮嬪墠纭繚鍋滄淇″彿宸叉竻闄?
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
            # 鎵弿鎵€鏈夊獟浣撳簱
            logger.info(f"褰撳墠鏈嶅姟鍣?{server}")
            cover_style = {
                "static_1": "闈欐€?1",
                "static_2": "闈欐€?2",
            }.get(self._cover_style, "闈欐€?1")
            logger.info(f"褰撳墠椋庢牸 {cover_style}")
            # 鑾峰彇濯掍綋搴撳垪琛?
            libraries = self.__get_server_libraries(service)
            if not libraries:
                logger.warning(f"鏈嶅姟鍣?{server} 鐨勫獟浣撳簱鍒楄〃鑾峰彇澶辫触")
                continue
            self.__debug_log(f"鏈嶅姟鍣?{server} 鍙敤濯掍綋搴撴暟閲?{len(libraries)}")
            selected_library_ids = {library_id for srv, library_id in selected_pairs if srv == server}
            if selected_library_ids:
                self.__debug_log(f"鏈嶅姟鍣?{server} 鎸囧畾濯掍綋搴撹繃婊?{sorted(selected_library_ids)}")
                filtered_libraries = []
                for library in libraries:
                    current_library_id = library.get("Id") if service.type == 'emby' else library.get("ItemId")
                    if str(current_library_id or "").strip() in selected_library_ids:
                        filtered_libraries.append(library)
                libraries = filtered_libraries
                if not libraries:
                    logger.warning(f"鏈嶅姟鍣?{server} 涓湭鎵惧埌宸查€夋嫨鐨勫獟浣撳簱锛屽凡璺宠繃")
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
        # 鑷畾涔夊浘鍍忚矾寰?
        image_path = self.__check_custom_image(library_name)
        # 浠庨厤缃幏鍙栨爣棰樺拰鑳屾櫙棰滆壊
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

        # 浣跨敤瀹夊叏鐨勬枃浠跺悕
        safe_library_name = self.__sanitize_filename(library_name)
        library_dir = os.path.join(self._covers_input, safe_library_name)
        if not os.path.isdir(library_dir):
            return None

        images = sorted([
            os.path.join(library_dir, f)
            for f in os.listdir(library_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"))
        ])
        
        return images if images else None  # 鎴栨敼涓?return images if images else False

    def __generate_image_from_path(self, server, library_name, title, image_path=None, config_bg_color=None):
        gc.collect()
        logger.info(f"媒体库 {server}：{library_name} 正在生成封面图 ...")
        image_data = False

        # 鎵ц鍋ュ悍妫€鏌?
        if not self.health_check():
            logger.error("鎻掍欢鍋ュ悍妫€鏌ュけ璐ワ紝鏃犳硶鐢熸垚灏侀潰")
            return False

        # 纭繚鍒嗚鲸鐜囬厤缃凡鍒濆鍖?
        if not hasattr(self, '_resolution_config') or self._resolution_config is None:
            logger.warning("分辨率配置未初始化，重新初始化")
            # 浣跨敤鐢ㄦ埛璁剧疆鐨勫垎杈ㄧ巼锛岃€屼笉鏄‖缂栫爜鐨?080p
            if self._resolution == "custom":
                try:
                    custom_w = int(self._custom_width)
                    custom_h = int(self._custom_height)
                    self._resolution_config = self.__new_resolution_config((custom_w, custom_h))
                except ValueError:
                    logger.warning(f"鑷畾涔夊垎杈ㄧ巼鍙傛暟鏃犳晥: {self._custom_width}x{self._custom_height}, 浣跨敤榛樿1080p")
                    self._resolution_config = self.__new_resolution_config("1080p")
            else:
                self._resolution_config = self.__new_resolution_config(self._resolution)

        # 浣跨敤鍒嗚鲸鐜囬厤缃绠楀瓧浣撳ぇ灏?
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
            # 闈欐€侀鏍兼寜褰撳墠鍒嗚鲸鐜囩缉鏀?
            zh_font_size = self._resolution_config.get_font_size(base_zh_font_size) * title_scale
            en_font_size = self._resolution_config.get_font_size(base_en_font_size) * title_scale

        blur_size = self._blur_size or 50
        color_ratio = self._color_ratio or 0.8

        # 妫€鏌ュ瓧浣撹矾寰勬槸鍚︽湁鏁?
        if not self._zh_font_path or not self._en_font_path:
            logger.error("字体路径未配置或无效，无法生成封面")
            return False

        # 楠岃瘉瀛椾綋鏂囦欢鏄惁瀛樺湪
        if not self.__validate_font_file(Path(self._zh_font_path)):
            logger.error(f"涓绘爣棰樺瓧浣撴枃浠舵棤鏁? {self._zh_font_path}")
            return False

        if not self.__validate_font_file(Path(self._en_font_path)):
            logger.error(f"鍓爣棰樺瓧浣撴枃浠舵棤鏁? {self._en_font_path}")
            return False

        font_path = (str(self._zh_font_path), str(self._en_font_path))
        font_size = (float(zh_font_size), float(en_font_size))

        zh_font_offset = float(self._zh_font_offset or 0)
        title_spacing = float(self._title_spacing or 40) * title_scale
        en_line_spacing = float(self._en_line_spacing or 40) * title_scale
        font_offset = (float(zh_font_offset), float(title_spacing), float(en_line_spacing))

        # 璁板綍鍒嗚鲸鐜囬厤缃俊鎭?
        logger.info(f"褰撳墠鍒嗚鲸鐜囬厤缃? {self._resolution_config}")

        # 鍑嗗鑳屾櫙棰滆壊閰嶇疆
        bg_color_config = {
            'mode': self._bg_color_mode,
            'custom_color': self._custom_bg_color,
            'config_color': config_bg_color
        }

        # 浼犻€掑垎杈ㄧ巼閰嶇疆缁欏浘鍍忕敓鎴愬嚱鏁?
        if self._cover_style == 'static_1':
            create_style_static_1 = self.__load_style_creator("style_static_1", "create_style_static_1")
            safe_library_name = self.__sanitize_filename(library_name)
            if image_path:
                library_dir = Path(self._covers_input) / safe_library_name
            else:
                library_dir = Path(self._covers_path) / safe_library_name
            logger.info(f"static_1: 鍑嗗鍥剧墖鐩綍 {library_dir}")
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
                logger.warning(f"static_1: 鍥剧墖鐩綍鍑嗗澶辫触 {library_dir}")
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
            logger.info(f"static_2: 鍑嗗鍥剧墖鐩綍 {library_dir}")
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
                logger.warning(f"static_2: 鍥剧墖鐩綍鍑嗗澶辫触 {library_dir}")
        gc.collect()
        return image_data
    
    def __generate_from_server(self, service, library, title):

        logger.info(f"媒体库 {service.name}：{library['Name']} 开始筛选媒体项")
        required_items = self.__get_required_items()
        target_items = self.__get_fetch_target_count()
        
        # 鑾峰彇椤圭洰闆嗗悎
        items = []
        offset = 0
        batch_size = 50  # 姣忔鑾峰彇鐨勯」鐩暟閲?
        max_attempts = 20  # 鏈€澶у皾璇曟鏁帮紝闃叉鏃犻檺寰幆
        
        library_type = library.get('CollectionType')
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        parent_id = library_id
        
        # 澶勭悊鍚堥泦绫诲瀷鐨勭壒娈婃儏鍐?
        if library_type == "boxsets":
            return self.__handle_boxset_library(service, library, title)
        elif library_type == "playlists":
            return self.__handle_playlist_library(service, library, title)
        elif library_type == "music":
            include_types = 'MusicAlbum,Audio'
        else:
            # 鍩虹绫诲瀷鏄犲皠
            if self.__is_single_image_style():
                include_types = {
                    "PremiereDate": "Movie,Series",
                    "DateCreated": "Movie,Episode",
                    "Random": "Movie,Series"
                }.get(self._sort_by, "Movie,Series")
            else:
                # 瀵逛簬澶氬浘鏍峰紡锛屽鏋滄寜鏈€鏂板叆搴撴帓搴忥紙DateCreated锛夛紝涔熻鍖呭惈 Episode 浠ュ睍绀哄墽闆嗙殑鏈€鏂板姩鎬?
                if self._sort_by == "DateCreated":
                    include_types = "Movie,Episode"
                else:
                    # 鍏朵粬鎺掑簭鏂瑰紡榛樿浣跨敤 Series 鑾峰彇娴锋姤
                    include_types = "Movie,Series"
            logger.debug(f"濯掍綋搴撶瓫閫夌被鍨? {include_types}, 鎺掑簭鏂瑰紡: {self._sort_by}")
        self._seen_keys = set()
        for attempt in range(max_attempts):
            if self._event.is_set():
                logger.info("妫€娴嬪埌鍋滄淇″彿锛屼腑鏂獟浣撻」鑾峰彇 ...")
                return False
                
            batch_items = self.__get_items_batch(service, parent_id,
                                              offset=offset, limit=batch_size,
                                              include_types=include_types)
            
            if not batch_items:
                break  # 娌℃湁鏇村椤圭洰鍙幏鍙?
                
            # 绛涢€夋湁鏁堥」鐩紙鏈夋墍闇€鍥剧墖鐨勯」鐩級
            valid_items = self.__filter_valid_items(batch_items)
            items.extend(valid_items)
            
            # 濡傛灉宸茬粡鏈夎冻澶熺殑鏈夋晥椤圭洰锛屽垯鍋滄鑾峰彇
            if len(items) >= target_items:
                break
                
            offset += batch_size
        
        # 浣跨敤鑾峰彇鍒扮殑鏈夋晥椤圭洰鏇存柊灏侀潰
        if len(items) > 0:
            logger.info(f"媒体库 {service.name}：{library['Name']} 找到 {len(items)} 个有效项目")
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, items[:required_items])
        else:
            logger.warning(f"媒体库 {service.name}：{library['Name']} 无法找到有效图片项目（筛选类型: {include_types}）")
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
        
        # 棣栧厛妫€鏌oxSet鏈韩鏄惁鏈夊悎閫傜殑鍥剧墖
        self._seen_keys = set()

        valid_boxsets = self.__filter_valid_items(boxsets)
        valid_items.extend(valid_boxsets)
        
        # 濡傛灉BoxSet鏈韩娌℃湁瓒冲鐨勫浘鐗囷紝鍒欒幏鍙栧叾涓殑鐢靛奖
        if len(valid_items) < target_items:
            for boxset in boxsets:
                if len(valid_items) >= target_items:
                    break
                    
                # 鑾峰彇姝oxSet涓殑鐢靛奖
                movies = self.__get_items_batch(service,
                                             parent_id=boxset['Id'], 
                                             include_types=include_types)
                
                valid_movies = self.__filter_valid_items(movies)
                valid_items.extend(valid_movies)
                
                if len(valid_items) >= target_items:
                    break
        
        # 浣跨敤鑾峰彇鍒扮殑鏈夋晥椤圭洰鏇存柊灏侀潰
        if len(valid_items) > 0:
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, valid_items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, valid_items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, valid_items[:required_items])
        else:
            print(f"媒体库 {service.name}：{library['Name']} 无法找到有效图片项目")
            return False
        
    def __handle_playlist_library(self, service, library, title):
        """ 
        鎾斁鍒楄〃鍥剧墖鑾峰彇 
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
        
        # 棣栧厛妫€鏌?playlist 鏈韩鏄惁鏈夊悎閫傜殑鍥剧墖
        self._seen_keys = set()

        valid_playlists = self.__filter_valid_items(playlists)
        valid_items.extend(valid_playlists)
        
        # 濡傛灉 playlist 鏈韩娌℃湁瓒冲鐨勫浘鐗囷紝鍒欒幏鍙栧叾涓殑鐢靛奖
        if len(valid_items) < target_items:
            for playlist in playlists:
                if len(valid_items) >= target_items:
                    break
                    
                # 鑾峰彇姝?playlist 涓殑鐢靛奖
                movies = self.__get_items_batch(service,
                                             parent_id=playlist['Id'], 
                                             include_types=include_types)
                
                valid_movies = self.__filter_valid_items(movies)
                valid_items.extend(valid_movies)
                
                if len(valid_items) >= target_items:
                    break
        
        # 浣跨敤鑾峰彇鍒扮殑鏈夋晥椤圭洰鏇存柊灏侀潰
        if len(valid_items) > 0:
            if self.__is_single_image_style():
                return self.__update_single_image(service, library, title, valid_items[0])
            elif self._cover_style == "static_2":
                return self.__update_showcase_image(service, library, title, valid_items[:target_items])
            else:
                return self.__update_grid_image(service, library, title, valid_items[:required_items])
        else:
            print(f"警告: 无法为播放列表 {service.name}：{library['Name']} 找到有效图片项目")
            return False
        
    def __get_items_batch(self, service, parent_id, offset=0, limit=20, include_types=None):
        # 璋冪敤API鑾峰彇椤圭洰
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
                    # 杞Щ鐩戞帶妯″紡涓嬪己鍒跺寘鍚?Episode 浠ヨ幏鍙栨渶鏂板叆搴撶殑鍐呭
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
                logger.error(f"鑾峰彇濯掍綋椤瑰け璐ワ細{str(err)}")
            return []
                
        except Exception as err:
            logger.error(f"Failed to get latest items: {str(err)}")
            return []
        
    def __filter_valid_items(self, items):
        """Filter valid media items and deduplicate by content/image keys."""
        valid_items = []

        for item in items:
            # 1) 鏍规嵁褰撳墠鏍峰紡璁＄畻鐪熷疄浼氫娇鐢ㄧ殑鍥剧墖URL
            image_url = self.__get_image_url(item)
            if not image_url:
                continue

            # 2) 涓ゅ眰鍘婚噸锛?
            #    - content_key: 鍐呭灞傦紙濡傚悓涓€鍓ч泦鐨勫闆嗕娇鐢ㄥ悓涓€Series鍥撅級
            #    - image_key:   鍥剧墖灞傦紙鍚屼竴鍥剧墖tag鎴栧悓涓€璺緞锛?
            content_key = self.__build_content_key(item)
            image_key = self.__build_image_key(image_url)

            if not content_key and not image_key:
                continue

            if (content_key and content_key in self._seen_keys) or (image_key and image_key in self._seen_keys):
                continue

            # 3) 鍔犲叆鏈夋晥鍒楄〃骞惰褰曞凡澶勭悊鐨?Key
            valid_items.append(item)
            if content_key:
                self._seen_keys.add(content_key)
            if image_key:
                self._seen_keys.add(image_key)

        return valid_items

    def __build_content_key(self, item: dict) -> Optional[str]:
        """Build a de-duplication key for media content."""
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
        """Build a de-duplication key for image URLs."""
        if not image_url:
            return None

        try:
            # 缁熶竴绉婚櫎 api_key 鍙傛暟锛岄伩鍏嶅悓鍥句笉鍚屽瘑閽ュ鑷撮噸澶?
            normalized = re.sub(r"([?&])api_key=[^&]*", "", image_url).rstrip("?&")

            # 浼樺厛鐢ㄨ矾寰?+ tag 浣滀负鍘婚噸鍏抽敭瀛楋紙鑳界簿鍑嗗尯鍒嗗浘鍍忕増鏈級
            # 渚嬪: /Items/{id}/Images/Backdrop/0?tag=xxx
            tag_match = re.search(r"[?&]tag=([^&]+)", image_url)
            tag = tag_match.group(1) if tag_match else ""

            parsed = urlparse(normalized)
            path = parsed.path if parsed.path else normalized
            return f"img:{path}|tag:{tag}"
        except Exception:
            return f"img:{image_url}"


    
    def __update_single_image(self, service, library, title, item):
        """鏇存柊鍗曞浘灏侀潰"""
        logger.info(f"媒体库 {service.name}：{library['Name']} 从媒体项获取图片")
        updated_item_id = ''
        image_url = self.__get_image_url(item)
        if not image_url:
            return False
            
        image_path = self.__download_image(service, image_url, library['Name'], count=1)
        if not image_path:
            return False
        updated_item_id = self.__get_item_id(item)
        # 浠庨厤缃幏鍙栬儗鏅鑹?
        title_result = self.__get_title_from_config(library['Name'], service.name)
        config_bg_color = title_result[2] if len(title_result) == 3 else None
        image_data = self.__generate_image_from_path(service.name, library['Name'], title, image_path, config_bg_color)
            
        if not image_data:
            return False
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        # 鏇存柊id
        self.update_cover_history(
            server=service.name, 
            library_id=library_id, 
            item_id=updated_item_id
        )

        return image_data
    
    def __update_grid_image(self, service, library, title, items):
        """Update grid cover."""
        logger.info(f"媒体库 {service.name}：{library['Name']} 从媒体项获取图片")

        image_paths = []
        
        updated_item_ids = []
        for i, item in enumerate(items):
            if self._event.is_set():
                logger.info("妫€娴嬪埌鍋滄淇″彿锛屼腑鏂浘鐗囦笅杞?...")
                return False
            image_url = self.__get_image_url(item)
            if image_url:
                image_path = self.__download_image(service, image_url, library['Name'], count=i+1)
                if image_path:
                    image_paths.append(image_path)
                    updated_item_ids.append(self.__get_item_id(item))
        
        if len(image_paths) < 1:
            return False
            
        # 鐢熸垚涔濆鏍煎浘鐗?
        # 浠庨厤缃幏鍙栬儗鏅鑹?
        title_result = self.__get_title_from_config(library['Name'], service.name)
        config_bg_color = title_result[2] if len(title_result) == 3 else None
        image_data = self.__generate_image_from_path(service.name, library['Name'], title, None, config_bg_color)
        if not image_data:
            return False
        if service.type == 'emby':
            library_id = library.get("Id")
        else:
            library_id = library.get("ItemId")
        # 鏇存柊ids
        for item_id in reversed(updated_item_ids):
            self.update_cover_history(
                server=service.name, 
                library_id=library_id, 
                item_id=item_id
            )
            
        return image_data

    def __update_showcase_image(self, service, library, title, items):
        """Update showcase cover with banner background and posters."""
        logger.info(f"媒体库 {service.name}：{library['Name']} 生成横幅展示封面")

        if not items:
            return False

        background_path = None
        background_item_id = None
        updated_item_ids = []

        for item in items:
            if self._event.is_set():
                logger.info("妫€娴嬪埌鍋滄淇″彿锛屼腑鏂浘鐗囦笅杞?...")
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
                logger.info("妫€娴嬪埌鍋滄淇″彿锛屼腑鏂浘鐗囦笅杞?...")
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
            # 鏇挎崲鍏ㄨ鍐掑彿涓哄崐瑙?
            yaml_str = yaml_str.replace("：", ":")
            # 鏇挎崲鍒惰〃绗︿负涓や釜绌烘牸锛岀粺涓€缂╄繘
            yaml_str = yaml_str.replace("\t", "  ")

            # 澶勭悊鏁板瓧鎴栧瓧姣嶅紑澶寸殑濯掍綋搴撳悕锛岀‘淇濆畠浠姝ｇ‘瑙ｆ瀽涓哄瓧绗︿覆閿?
            # 鍦╕AML涓紝鏁板瓧寮€澶寸殑閿彲鑳借瑙ｆ瀽涓烘暟瀛楋紝闇€瑕佸姞寮曞彿
            lines = yaml_str.split('\n')
            processed_lines = []
            for line in lines:
                # 妫€鏌ユ槸鍚︽槸閿€煎琛岋紙鍖呭惈鍐掑彿涓斾笉鏄敞閲婏級
                if ':' in line and not line.strip().startswith('#'):
                    # 鍒嗗壊閿拰鍊?
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key_part = parts[0].strip()
                        value_part = parts[1]

                        # 濡傛灉閿笉鏄互寮曞彿寮€澶达紝涓斿寘鍚暟瀛楁垨鐗规畩瀛楃锛屽垯娣诲姞寮曞彿
                        if key_part and not (key_part.startswith('"') or key_part.startswith("'")):
                            # 妫€鏌ユ槸鍚﹂渶瑕佸姞寮曞彿锛堟暟瀛楀紑澶淬€佸寘鍚壒娈婂瓧绗︾瓑锛?
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
                logger.debug(f"处理后的 YAML(扁平, 前{preview_limit}字符): {flat_yaml[:preview_limit]}... (已截断)")
            else:
                logger.debug(f"澶勭悊鍚庣殑YAML(鎵佸钩): {flat_yaml}")

            title_config = yaml.safe_load(processed_yaml) or {}
            if not isinstance(title_config, dict):
                return {}
            filtered = {}
            for key, value in title_config.items():
                if value is None:
                    # 鍏佽浠呭０鏄庢湇鍔″櫒閿紝鏈厤缃獟浣撳簱鏃朵笉鍛婅
                    continue
                if isinstance(value, dict):
                    # 鏂版牸寮忥細鏈嶅姟鍣ㄥ悕 -> 濯掍綋搴撻厤缃瓧鍏?
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
                                logger.info(f"配置项 {key}/{lib_key} 包含多行，仅使用前三行")
                        else:
                            logger.warning(f"鏍囬閰嶇疆椤规牸寮忎笉姝ｇ‘锛屽凡蹇界暐: {key}/{lib_key} -> {lib_value}")
                    if server_filtered:
                        filtered[str(key)] = server_filtered
                    continue

                if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], str) and isinstance(value[1], str):
                    # 鍏煎鏃ф牸寮忥細濯掍綋搴?-> [涓枃, 鑻辨枃, 鍙€夎儗鏅壊]
                    if len(value) >= 3 and isinstance(value[2], str):
                        filtered[str(key)] = [value[0], value[1], value[2]]
                    else:
                        filtered[str(key)] = [value[0], value[1]]
                    if len(value) > 3:
                        logger.info(f"配置项 {key} 包含多行，仅使用前三行")
                    continue

                # 蹇界暐鏍煎紡涓嶆纭殑椤?
                logger.warning(f"鏍囬閰嶇疆椤规牸寮忎笉姝ｇ‘锛屽凡蹇界暐: {key} -> {value}")
                continue

            logger.debug(f"瑙ｆ瀽鍚庣殑閰嶇疆: {filtered}")
            return filtered
        except Exception as e:
            # 鏁翠綋 YAML 鏃犳硶瑙ｆ瀽锛堟瘮濡傝娉曢敊璇級锛岃繑鍥炵┖閰嶇疆
            logger.warning(f"YAML 瑙ｆ瀽澶辫触锛屼娇鐢ㄧ┖閰嶇疆: {e}")
            return {}

    def __get_title_from_config(self, library_name, server_name: Optional[str] = None):
        """Read title config from YAML and resolve title/background settings."""
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
                logger.debug(f"鏍囬閰嶇疆鎸夋湇鍔″櫒鍖归厤鎴愬姛: {server_key}")
            elif nested_mode:
                # 鏂版牸寮忎笅鏈壘鍒版湇鍔″櫒鏃讹紝涓嶈法鏈嶅姟鍣ㄦ壂鎻?
                scoped_config = {}
                logger.debug(f"鏍囬閰嶇疆鏈壘鍒版湇鍔″櫒鍒嗙粍: {server_name}")

        # 娣诲姞璋冭瘯淇℃伅
        logger.debug(f"鏌ユ壘濯掍綋搴撳悕绉? '{library_name}' (绫诲瀷: {type(library_name)})")
        logger.debug(f"褰撳墠浣滅敤鍩熼厤缃敭: {list(scoped_config.keys()) if isinstance(scoped_config, dict) else []}")

        # 澶氱鍖归厤绛栫暐锛岀‘淇濇暟瀛楁垨瀛楁瘝寮€澶寸殑濯掍綋搴撳悕鑳藉姝ｇ‘鍖归厤
        for lib_name, config_values in (scoped_config.items() if isinstance(scoped_config, dict) else []):
            if not isinstance(config_values, list) or len(config_values) < 2:
                continue
            # 绛栫暐1: 鐩存帴瀛楃涓叉瘮杈?
            if str(lib_name) == str(library_name):
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"鎵惧埌鍖归厤鐨勯厤缃?鐩存帴鍖归厤): {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break

            # 绛栫暐2: 鍘婚櫎绌烘牸鍚庢瘮杈?
            if str(lib_name).strip() == str(library_name).strip():
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"鎵惧埌鍖归厤鐨勯厤缃?鍘荤┖鏍煎尮閰?: {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break

            # 绛栫暐3: 蹇界暐澶у皬鍐欐瘮杈?
            if str(lib_name).lower() == str(library_name).lower():
                zh_title = config_values[0]
                en_title = config_values[1] if len(config_values) > 1 else ''
                bg_color = config_values[2] if len(config_values) > 2 else None
                logger.debug(f"鎵惧埌鍖归厤鐨勯厤缃?蹇界暐澶у皬鍐欏尮閰?: {lib_name} -> {zh_title}, {en_title}, {bg_color}")
                break
        else:
            logger.debug(f"鏈壘鍒板獟浣撳簱 '{library_name}' 鐨勯厤缃紝浣跨敤榛樿鏍囬")
            # 濡傛灉娌℃湁鎵惧埌閰嶇疆锛屾鏌ユ槸鍚︽槸鏁板瓧寮€澶寸殑濯掍綋搴撳悕瀵艰嚧鐨勯棶棰?
            if library_name and (library_name[0].isdigit() or library_name[0].isalpha()):
                logger.info(f"濯掍綋搴撳悕 '{library_name}' 浠ユ暟瀛楁垨瀛楁瘝寮€澶达紝濡傛灉闇€瑕佽嚜瀹氫箟鏍囬锛岃鍦ㄩ厤缃腑浣跨敤寮曞彿鍖呭洿濯掍綋搴撳悕锛屼緥濡? \"{library_name}\":")

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
                self.__debug_log(f"璇锋眰濯掍綋搴撳垪琛細server={getattr(service, 'name', 'unknown')} url={request_url_safe}")
                res = service.instance.get_data(url=url)
                if not res:
                    logger.warning(f"鑾峰彇濯掍綋搴撳垪琛ㄥけ璐?鏃犲搷搴?锛歴erver={getattr(service, 'name', 'unknown')} url={request_url_safe}")
                    return []
                self.__debug_log(f"濯掍綋搴撳垪琛ㄥ搷搴旓細server={getattr(service, 'name', 'unknown')} status={res.status_code}")
                if res.status_code >= 400:
                    body_preview = ""
                    try:
                        body_preview = (res.text or "").strip().replace("\n", " ")[:300]
                    except Exception:
                        body_preview = ""
                    logger.warning(
                        f"鑾峰彇濯掍綋搴撳垪琛ㄥけ璐?HTTP {res.status_code})锛歴erver={getattr(service, 'name', 'unknown')} "
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
                        f"鑾峰彇濯掍綋搴撳垪琛ㄥけ璐?JSON瑙ｆ瀽澶辫触)锛歴erver={getattr(service, 'name', 'unknown')} "
                        f"url={request_url_safe} err={json_err} response={body_preview}"
                    )
                    return []
                count_hint = len(data) if isinstance(data, list) else len(data.get("Items", [])) if isinstance(data, dict) else 0
                self.__debug_log(f"濯掍綋搴撳垪琛ㄨВ鏋愭垚鍔燂細server={getattr(service, 'name', 'unknown')} count={count_hint}")
                if service.type == 'emby':
                    return data.get("Items", []) if isinstance(data, dict) else []
                return data if isinstance(data, list) else []
            except Exception as err:
                logger.error(f"鑾峰彇濯掍綋搴撳垪琛ㄥけ璐ワ細{str(err)}")
            return []
        except Exception as err:
            logger.error(f"鑾峰彇濯掍綋搴撳垪琛ㄥけ璐ワ細{str(err)}")
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
                # 鍚戝悗鍏煎鏃ф牸寮忥細server-library_id
                server, library_id = raw.split("-", 1)
            else:
                continue
            server = server.strip()
            library_id = library_id.strip()
            if server and library_id:
                selected.append((server, library_id))
        return selected

    def __get_showcase_background_url(self, item):
        """Get background image URL for showcase style."""
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
        """Get poster image URL for showcase style."""
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
        """Get image URL from media item metadata."""
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
        """Get media item id."""
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
        """Download image and save to local cache folder."""
        try:
            # 纭繚濯掍綋搴撳悕绉版槸瀹夊叏鐨勬枃浠跺悕锛堝鐞嗘暟瀛楁垨瀛楁瘝寮€澶寸殑鍚嶇О锛?
            safe_library_name = self.__sanitize_filename(library_name)

            # 鍒涘缓鐩爣瀛愮洰褰?
            subdir = os.path.join(self._covers_path, safe_library_name)
            os.makedirs(subdir, exist_ok=True)

            # 鏂囦欢鍛藉悕锛歩tem_id 涓轰富锛岄€傚悎鎺掑簭
            if count is not None:
                filename = f"{count}.jpg"
            else:
                filename = f"img_{int(time.time())}.jpg"

            filepath = os.path.join(subdir, filename)

            # 濡傛灉鏂囦欢宸插瓨鍦紝鐩存帴杩斿洖璺緞
            # if os.path.exists(filepath):
            #     return filepath

            # 閲嶈瘯鏈哄埗
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

                # 濡傛灉鎴愬姛锛屼繚瀛樺苟杩斿洖
                if image_content:
                    with open(filepath, 'wb') as f:
                        f.write(image_content)
                    return filepath

                # 濡傛灉澶辫触锛岃褰曞苟绛夊緟鍚庨噸璇?
                logger.warning(f"绗?{attempt} 娆″皾璇曚笅杞藉け璐ワ細{imageurl}")
                if attempt < retries:
                    time.sleep(delay)

            logger.error(f"图片下载失败（重试 {retries} 次）：{imageurl}")
            return None

        except Exception as err:
            logger.error(f"下载图片异常：{str(err)}")
            return None


    def __save_image_to_local(self, image_content, server_name: str, library_name: str, extension: str):
        """
        淇濆瓨鍥剧墖鍒版湰鍦拌矾寰?
        """
        try:
            if not self._save_recent_covers:
                return
            # 纭繚鐩綍瀛樺湪
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
            logger.info(f"鍥剧墖宸蹭繚瀛樺埌鏈湴: {file_path}")
            self.__trim_saved_cover_history(local_path, safe_server, safe_library)
        except Exception as err:
            logger.error(f"淇濆瓨鍥剧墖鍒版湰鍦板け璐? {str(err)}")

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
                logger.info(f"宸叉寜鍘嗗彶鏁伴噺闄愬埗鍒犻櫎鏃у皝闈? {old_file}")
        except Exception as e:
            logger.warning(f"娓呯悊鍘嗗彶灏侀潰澶辫触: {e}")
        

    def __set_library_image(self, service, library, image_base64):
        """
        璁剧疆濯掍綋搴撳皝闈?
        """

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
                f"璁剧疆灏侀潰璇锋眰锛歴erver={service.name} library={library.get('Name')} library_id={library_id} "
                f"url={self.__safe_log_url(request_url)}"
            )
            # 鏍规嵁 base64 鍓嶅嚑涓瓧鑺傜畝鍗曞垽鏂牸寮?
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

            # 鍦ㄥ彂閫佸墠淇濆瓨涓€浠藉浘鐗囧埌鏈湴
            try:
                image_bytes = base64.b64decode(image_base64)
            except Exception as decode_err:
                logger.error(f"灏侀潰鏁版嵁瑙ｇ爜澶辫触: {decode_err}")
                return False

            if self._save_recent_covers:
                try:
                    self.__save_image_to_local(image_bytes, service.name, library['Name'], extension)
                except Exception as save_err:
                    logger.error(f"淇濆瓨鍙戦€佸墠鍥剧墖澶辫触: {str(save_err)}")

            logger.info(
                f"鍑嗗涓婁紶灏侀潰: {library['Name']} 鏍煎紡={content_type} 澶у皬={len(image_bytes) / 1024:.1f}KB"
            )
            self.__debug_log(
                f"灏侀潰涓婁紶鍙傛暟锛歝ontent_type={content_type} extension={extension} base64_len={len(image_base64)} bytes={len(image_bytes)}"
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
                self.__debug_log(f"灏侀潰涓婁紶鍝嶅簲锛歴tatus={res.status_code}")

            if res and res.status_code in [200, 204]:
                self.__debug_log(f"灏侀潰涓婁紶鎴愬姛锛歭ibrary={library['Name']} status={res.status_code}")
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
                    f"设置「{library['Name']}」封面失败，错误码：No response（可能是反向代理超时、连接重置或媒体服务拒绝连接）"
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
                # 濡傛灉瀛楁缂哄け鎴栨牸寮忛敊璇垯璺宠繃璇ラ」
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

        # 鍘熷鏁版嵁
        history = self.get_data('cover_history') or []

        # 鐢ㄤ簬鍒嗙粍绠＄悊锛?server, library_id) => list of items
        grouped = defaultdict(list)
        for item in history:
            key = (item["server"], str(item["library_id"]))
            grouped[key].append(item)

        key = (server, library_id)
        items = grouped[key]

        # 鏌ユ壘鏄惁宸叉湁璇?item_id
        existing = next((i for i in items if str(i["item_id"]) == item_id), None)

        if existing:
            # 鑻ュ凡瀛樺湪涓旀槸鏈€鏂扮殑锛岃烦杩?
            if existing["timestamp"] >= max(i["timestamp"] for i in items):
                return
            else:
                existing["timestamp"] = now
        else:
            items.append(history_item)

        # 鎺掑簭 + 鎴彇鍓?
        grouped[key] = sorted(items, key=lambda x: x["timestamp"], reverse=True)[:9]

        # 閲嶆柊鏁村悎鎵€鏈夊垎缁勭殑鏁版嵁
        new_history = []
        for item_list in grouped.values():
            new_history.extend(item_list)

        self.save_data('cover_history', new_history)
        return [ 
            item for item in new_history
            if str(item.get("library_id")) == str(library_id)
        ]

    def prepare_library_images(self, library_dir: str, required_items: int = 9):
        """Prepare numbered image files in the library cache directory."""
        os.makedirs(library_dir, exist_ok=True)

        required_items = max(1, int(required_items))

        # 妫€鏌ュ摢浜涚紪鍙风殑鏂囦欢宸插瓨鍦紝鍝簺缂哄け
        existing_numbers = []
        missing_numbers = []
        for i in range(1, required_items + 1):
            target_file_path = os.path.join(library_dir, f"{i}.jpg")
            if os.path.exists(target_file_path):
                existing_numbers.append(i)
            else:
                missing_numbers.append(i)

        # 濡傛灉宸茬粡瀛樺湪鎵€鏈夋枃浠讹紝鐩存帴杩斿洖
        if not missing_numbers:
            return True

        logger.info(f"信息: {library_dir} 中缺少以下编号的图片: {missing_numbers}，将进行补充。")

        target_name_pattern = rf"^[1-9][0-9]*\.jpg$"

        # 鑾峰彇鍙敤浣滄簮鐨勫浘鐗囷紙鎺掗櫎宸叉湁鐨勭洰鏍囩紪鍙锋枃浠讹級
        # 浣跨敤 scandir 骞堕檺鍒堕噰鏍锋暟閲忥紝閬垮厤瓒呭ぇ鐩綍鎵弿瀵艰嚧闀挎椂闂存棤鏃ュ織
        source_image_filenames = []
        max_source_scan = 512
        scanned_entries = 0
        for entry in os.scandir(library_dir):
            scanned_entries += 1
            if not entry.is_file():
                continue

            f = entry.name
            # 鎺掗櫎 N.jpg锛圢 涓烘鏁存暟锛変綔涓烘簮
            if re.match(target_name_pattern, f, re.IGNORECASE):
                continue
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                source_image_filenames.append(f)
                if len(source_image_filenames) >= max_source_scan:
                    break

        if scanned_entries > 2000:
            logger.info(f"淇℃伅: {library_dir} 鏂囦欢杈冨锛屽凡蹇€熼噰鏍?{len(source_image_filenames)} 寮犱綔涓鸿ˉ鍥炬簮")

        # 濡傛灉娌℃湁婧愬浘鐗囧彲鐢?
        if not source_image_filenames:
            # 濡傛灉宸茬粡鏈夐儴鍒嗙洰鏍囩紪鍙峰浘鐗囷紝鍙互浠庤繖浜涚幇鏈夋枃浠朵腑閫夋嫨
            if existing_numbers:
                logger.info(f"信息: {library_dir} 中没有其他可用图片，将从现有目标编号图片中随机选择并复制。")
                existing_file_paths = [os.path.join(library_dir, f"{i}.jpg") for i in existing_numbers]
                source_image_paths = existing_file_paths
            else:
                logger.info(f"警告: {library_dir} 中没有任何可用图片来生成 1-{required_items}.jpg。")
                return False
        else:
            # 灏嗘枃浠跺悕杞崲涓哄畬鏁磋矾寰?
            source_image_paths = [os.path.join(library_dir, f) for f in sorted(source_image_filenames)]

        # 濡傛灉婧愬浘鐗囨暟閲忎笉瓒筹紝闇€瑕侀噸澶嶄娇鐢?
        if len(source_image_paths) < len(missing_numbers):
            logger.info(f"信息: 源图片数量({len(source_image_paths)})小于缺失数量({len(missing_numbers)})，将重复使用部分图片。")
        
        # 涓烘瘡涓己澶辩殑缂栧彿閫夋嫨涓€涓簮鍥剧墖锛屽敖閲忛伩鍏嶈繛缁噸澶?
        last_used_source = None
        for missing_num in missing_numbers:
            target_path = os.path.join(library_dir, f"{missing_num}.jpg")
            
            # 濡傛灉鍙湁涓€涓簮鏂囦欢锛屾病鏈夐€夋嫨锛岀洿鎺ヤ娇鐢?
            if len(source_image_paths) == 1:
                selected_source = source_image_paths[0]
            else:
                # 灏濊瘯閫夋嫨涓€涓笌涓婃涓嶅悓鐨勬簮鏂囦欢
                available_sources = [s for s in source_image_paths if s != last_used_source]
                
                # 濡傛灉娌℃湁鍏朵粬閫夋嫨锛堝彲鑳戒笂娆＄敤浜嗗敮涓€鐨勬簮鏂囦欢锛夛紝鍒欎娇鐢ㄦ墍鏈夋簮
                if not available_sources:
                    available_sources = source_image_paths
                    
                # 闅忔満閫夋嫨涓€涓簮鏂囦欢
                selected_source = random.choice(available_sources)
                
            # 璁板綍鏈浣跨敤鐨勬簮鏂囦欢锛岀敤浜庝笅娆℃瘮杈?
            last_used_source = selected_source
            
            try:
                if not os.path.exists(selected_source):
                    logger.info(f"错误: 源文件 {selected_source} 在复制前不存在。")
                    return False
                    
                shutil.copy(selected_source, target_path)
                logger.info(f"淇℃伅: 宸插垱寤?{missing_num}.jpg (婧愯嚜: {os.path.basename(selected_source)})")
                
            except Exception as e:
                logger.info(f"閿欒: 澶嶅埗鏂囦欢 {selected_source} 鍒?{target_path} 鏃跺彂鐢熼敊璇? {e}")
                return False

        logger.info(f"淇℃伅: {library_dir} 宸叉垚鍔熻ˉ鍏呮墍鏈夌己澶辩殑鍥剧墖锛岀幇鍦ㄥ寘鍚畬鏁寸殑 1-{required_items}.jpg")
        return True

    def __get_fonts(self):
        def detect_string_type(s: str):
            if not s:
                return None
            s = s.strip()

            # 鍒ゆ柇鏄惁鏄?HTTP(S) 閾炬帴
            if re.match(r'^https?://[^\s]+$', s, re.IGNORECASE):
                return 'url'

            # 鍒ゆ柇鏄惁鍍忚矾寰勶紙鍖呭惈 / 鎴?\锛屾垨浠?~銆?銆? 寮€澶达級
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
        
        log_prefix = "榛樿"
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

        logger.info(f"褰撳墠涓绘爣棰樺瓧浣揢RL: {current_zh_font_url} (鏈湴璺緞: {zh_local_path_config})")

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
                    logger.info(f"{lang}瀛椾綋: 浣跨敤鏈湴鎸囧畾璺緞 {local_font_p}")
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
                        logger.info(f"{log_prefix}{lang}字体 URL 已变更或首次下载。")
                    if not font_file_is_valid and downloaded_font_file_path.exists():
                         logger.info(f"{log_prefix}{lang}字体文件 {downloaded_font_file_path} 无效或损坏，将重新下载。")
                    elif not downloaded_font_file_path.exists():
                         logger.info(f"{log_prefix}{lang}字体文件 {downloaded_font_file_path} 不存在，将下载。")

                    # 浣跨敤瀹夊叏鐨勫瓧浣撲笅杞芥柟娉?
                    if self.download_font_safely_with_timeout(url, downloaded_font_file_path):
                        try:
                            hash_file_path.write_text(url_hash)
                        except Exception as e:
                            logger.error(f"鍐欏叆鍝堝笇鏂囦欢澶辫触 {hash_file_path}: {e}")
                        current_font_path = downloaded_font_file_path
                    else:
                        logger.critical(f"无法获取必要字体资源: {log_prefix}{lang} -> {url}")
                        if font_file_is_valid :
                             logger.warning(f"下载失败，但已找到有效缓存字体 {downloaded_font_file_path}，将尝试继续使用。")
                             current_font_path = downloaded_font_file_path
                        else:
                             current_font_path = None
                else:
                    logger.info(f"{log_prefix}{lang}瀛椾綋: 浣跨敤宸蹭笅杞?缂撳瓨鐨勬湁鏁堝瓧浣?{downloaded_font_file_path}")
                    current_font_path = downloaded_font_file_path
            
            # 瀹夊叏璁剧疆瀛椾綋璺緞
            if current_font_path and current_font_path.exists():
                setattr(self, final_attr, current_font_path)
                status_log = '(鏈湴璺緞)' if using_local_font else '(宸蹭笅杞?缂撳瓨)'
                logger.info(f"{log_prefix}{lang}瀛椾綋鏈€缁堣矾寰? {getattr(self,final_attr)} {status_log}")
            else:
                # 瀛椾綋鑾峰彇澶辫触锛岃缃负None骞惰褰曢敊璇?
                setattr(self, final_attr, None)
                logger.error(f"{log_prefix}{lang}瀛椾綋鑾峰彇澶辫触锛岃繖鍙兘瀵艰嚧灏侀潰鐢熸垚澶辫触")

        # 妫€鏌ユ槸鍚︽墍鏈夊繀瑕佺殑瀛椾綋閮藉凡鑾峰彇
        if not self._zh_font_path or not self._en_font_path:
            logger.critical("关键字体文件缺失，插件可能无法正常工作。请检查网络连接或手动下载字体文件。")

    def __sanitize_filename(self, filename: str) -> str:
        """Sanitize library name to a safe filename."""
        if not filename:
            return "unknown"

        # 绉婚櫎鎴栨浛鎹笉瀹夊叏鐨勫瓧绗?
        import re
        # 鏇挎崲Windows鍜孶nix绯荤粺涓笉鍏佽鐨勫瓧绗?
        unsafe_chars = r'[<>:"/\\|?*]'
        safe_name = re.sub(unsafe_chars, '_', filename)

        # 绉婚櫎鍓嶅悗绌烘牸
        safe_name = safe_name.strip()

        # 濡傛灉鍚嶇О涓虹┖锛屼娇鐢ㄩ粯璁ゅ悕绉?
        if not safe_name:
            return "unknown"

        # 纭繚涓嶄互鐐瑰紑澶达紙鍦ㄦ煇浜涚郴缁熶腑鏄殣钘忔枃浠讹級
        if safe_name.startswith('.'):
            safe_name = '_' + safe_name[1:]

        # 闄愬埗闀垮害锛堥伩鍏嶈矾寰勮繃闀匡級
        if len(safe_name) > 100:
            safe_name = safe_name[:100]

        if safe_name != filename and filename not in self._sanitize_log_cache:
            self._sanitize_log_cache.add(filename)
            logger.debug(f"鏂囦欢鍚嶅畨鍏ㄥ寲: '{filename}' -> '{safe_name}'")
        return safe_name

    def health_check(self) -> bool:
        """Basic health check before generating covers."""
        try:
            # 妫€鏌ュ垎杈ㄧ巼閰嶇疆
            if not hasattr(self, '_resolution_config') or self._resolution_config is None:
                logger.warning("分辨率配置缺失，重新初始化")
                # 浣跨敤鐢ㄦ埛璁剧疆鐨勫垎杈ㄧ巼锛岃€屼笉鏄‖缂栫爜鐨?080p
                if self._resolution == "custom":
                    self._resolution_config = self.__new_resolution_config((self._custom_width, self._custom_height))
                else:
                    self._resolution_config = self.__new_resolution_config(self._resolution)

            # 妫€鏌ュ瓧浣撴枃浠?
            if not self._zh_font_path or not self._en_font_path:
                logger.warning("字体文件缺失，尝试重新获取")
                self.__get_fonts()

            # 楠岃瘉瀛椾綋鏂囦欢鏈夋晥鎬?
            if self._zh_font_path and not self.__validate_font_file(Path(self._zh_font_path)):
                logger.warning("涓绘爣棰樺瓧浣撴枃浠舵棤鏁堬紝灏濊瘯閲嶆柊涓嬭浇")
                return False

            if self._en_font_path and not self.__validate_font_file(Path(self._en_font_path)):
                logger.warning("鍓爣棰樺瓧浣撴枃浠舵棤鏁堬紝灏濊瘯閲嶆柊涓嬭浇")
                return False

            logger.info("鎻掍欢鍋ュ悍妫€鏌ラ€氳繃")
            return True

        except Exception as e:
            logger.error(f"鍋ュ悍妫€鏌ュけ璐? {e}")
            return False

    def download_font_safely_with_timeout(self, font_url: str, font_path: Path, timeout: int = 60) -> bool:
        """Download font with timeout protection."""
        try:
            logger.info(f"寮€濮嬩笅杞藉瓧浣擄紙瓒呮椂闄愬埗: {timeout}绉掞級: {font_url}")
            return self.download_font_safely(font_url, font_path, retries=1, timeout=timeout)

        except Exception as e:
            logger.error(f"瀛椾綋涓嬭浇杩囩▼涓嚭鐜板紓甯? {e}")
            return False

    def download_font_safely(self, font_url: str, font_path: Path, retries: int = 2, timeout: int = 30):
        """Download font file with retries and timeout."""
        logger.info(f"鍑嗗涓嬭浇瀛椾綋: {font_url} -> {font_path}")

        # 纭繚鍦ㄥ紑濮嬩笅杞藉墠鍒犻櫎浠讳綍鍙兘瀛樺湪鐨勬崯鍧忔枃浠?
        if font_path.exists():
            try:
                font_path.unlink()
                logger.info(f"鍒犻櫎涔嬪墠鐨勫瓧浣撴枃浠朵互渚块噸鏂颁笅杞? {font_path}")
            except OSError as unlink_error:
                logger.error(f"鏃犳硶鍒犻櫎鐜版湁瀛椾綋鏂囦欢 {font_path}: {unlink_error}")
                return False
        
        # 鍑嗗涓嬭浇绛栫暐
        strategies = []

        # 鍒ゆ柇鏄惁涓篏itHub閾炬帴
        is_github_url = "github.com" in font_url or "raw.githubusercontent.com" in font_url

        # 瀵逛簬GitHub閾炬帴锛屼紭鍏堜娇鐢℅itHub闀滃儚绔?
        if is_github_url and settings.GITHUB_PROXY:
            github_proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{font_url}"
            strategies.append(("GitHub镜像", github_proxy_url))

        # 鐩存帴浣跨敤鍘熷URL
        strategies.append(("鐩磋繛", font_url))

        # 閬嶅巻鎵€鏈夌瓥鐣?
        for strategy_name, target_url in strategies:
            logger.info(f"尝试使用策略：{strategy_name} 下载字体: {target_url}")

            # 鍒涘缓涓存椂鏂囦欢璺緞
            temp_path = font_path.with_suffix('.temp')

            try:
                response = requests.get(
                    target_url,
                    timeout=timeout,
                    headers={'User-Agent': 'MoviePilot-WsEmbyCover/1.3'},
                    stream=True,
                    verify=False if strategy_name == "GitHub镜像" else True,
                )
                if response.status_code == 200:
                    temp_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(temp_path, "wb") as f:
                        f.write(response.content)
                    if self.__validate_font_file(temp_path):
                        temp_path.replace(font_path)
                        logger.info(f"瀛椾綋涓嬭浇鎴愬姛: 浣跨敤绛栫暐 {strategy_name}")
                        return True
                    logger.warning("下载的字体文件校验失败，可能已损坏")
                    if temp_path.exists():
                        temp_path.unlink()
                else:
                    logger.warning(f"绛栫暐 {strategy_name} 涓嬭浇澶辫触锛孒TTP鐘舵€佺爜: {response.status_code}")

            except Exception as e:
                logger.warning(f"绛栫暐 {strategy_name} 涓嬭浇鍑洪敊: {e}")
                # 娓呯悊鍙兘鐨勪复鏃舵枃浠?
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        
        # 鎵€鏈夌瓥鐣ラ兘澶辫触
        logger.error(f"鎵€鏈変笅杞界瓥鐣ュ潎澶辫触锛屾棤娉曚笅杞藉瓧浣擄紝寤鸿鎵嬪姩涓嬭浇瀛椾綋: {font_url}")
        # 纭繚鐩爣璺緞娌℃湁鎹熷潖鐨勬枃浠?
        if font_path.exists():
            try:
                font_path.unlink()
                logger.info(f"宸插垹闄ら儴鍒嗕笅杞界殑鏂囦欢: {font_path}")
            except OSError as unlink_error:
                logger.error(f"鏃犳硶鍒犻櫎閮ㄥ垎涓嬭浇鐨勬枃浠?{font_path}: {unlink_error}")
        
        return False

    def get_file_extension_from_url(self, url: str, fallback_ext: str = ".ttf") -> str:
        # Get file extension from URL path.
        try:
            parsed_url = urlparse(url)
            path_part = parsed_url.path
            if path_part:
                filename = os.path.basename(path_part)
                _ , ext = os.path.splitext(filename)
                return ext if ext else fallback_ext
            else:
                logger.warning(f"鏃犳硶浠嶶RL涓彁鍙栬矾寰勯儴鍒? {url}. 浣跨敤澶囩敤鎵╁睍鍚? {fallback_ext}")
                return fallback_ext
        except Exception as e:
            logger.error(f"瑙ｆ瀽URL鏃跺嚭閿?'{url}': {e}. 浣跨敤澶囩敤鎵╁睍鍚? {fallback_ext}")
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
            logger.warning(f"瀛椾綋鏂囦欢瀛樺湪浣嗗彲鑳藉凡鎹熷潖鎴栨牸寮忔棤娉曡瘑鍒? {font_path}")
            return False
        except Exception as e:
            logger.warning(f"楠岃瘉瀛椾綋鏂囦欢鏃跺嚭閿?{font_path}: {e}")
            return False

    def stop_service(self):
        # Stop plugin service.
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"鍋滄鏈嶅姟澶辫触: {str(e)}")

