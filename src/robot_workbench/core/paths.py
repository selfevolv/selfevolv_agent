"""定义项目、运行文件和数据存储路径，不依赖网页应用。"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = PROJECT_ROOT / "src"
STORAGE_ROOT = Path("/mnt/data/home/youlika/selfevolv_piper")
DEFAULT_DATA_ROOT = STORAGE_ROOT / "data"
DEFAULT_MODEL_DIR = STORAGE_ROOT / "models"
SHARED_BASE_PARAMS = Path.home() / ".cache/openpi/openpi-assets/checkpoints/pi05_base/params"
CAMERA_TEST_ROOT = Path("/tmp/selfevolv_piper/camera_tests")


def runtime_root() -> Path:
    """返回环境变量指定的运行目录，否则使用项目内的 .runtime。"""
    return Path(os.environ.get("PIPER_RUNTIME_ROOT", PROJECT_ROOT / ".runtime")).expanduser()


def media_root() -> Path:
    """返回环境变量指定的媒体目录，否则使用运行目录内的生成视频缓存。"""
    return Path(os.environ.get("PIPER_WEB_V2_MEDIA_ROOT", runtime_root() / "cache/generated_videos")).expanduser()
