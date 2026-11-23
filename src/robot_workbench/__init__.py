"""配置工作台依赖搜索路径；硬件 SDK 由设备模块按需导入。"""
from pathlib import Path
import os
import sys
_root = Path(__file__).resolve().parents[2]
for _path in (str(_root / 'vendor'), os.environ.get('PIPER_RLINF_ROOT', str(Path.home() / 'code/github/RLinf')), str(_root / '.runtime/deps/pyagx')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
