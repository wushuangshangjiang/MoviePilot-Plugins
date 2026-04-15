import importlib.util
from pathlib import Path


def _load_v2_module():
    current_file = Path(__file__).resolve()
    repo_root = current_file.parents[2]
    v2_file = repo_root / "plugins.v2" / "wsembycover" / "__init__.py"
    spec = importlib.util.spec_from_file_location("wsembycover_v2_bridge", str(v2_file))
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载 v2 插件文件: {v2_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_v2_module = _load_v2_module()
WsEmbyCover = _v2_module.WsEmbyCover

