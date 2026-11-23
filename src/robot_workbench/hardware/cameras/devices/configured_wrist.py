"""腕部相机的配置驱动测试入口。

每次运行读取 piper_config.ini 中的腕部序列号，自动选择 Dabai/RealSense，
并将拍照、深度拍照、录像等命令交给公共相机 CLI。
"""

from robot_workbench.hardware.cameras.cli import run_camera_cli
from robot_workbench.hardware.cameras.rig import create_camera
from robot_workbench.core.config import load_config


def create_configured_camera(**kwargs):
    """每次读取现场配置的逻辑角色序列号，按传入流参数创建相机。"""
    config = load_config()
    return create_camera("wrist", config.wrist_serial, **kwargs)


if __name__ == "__main__":
    raise SystemExit(run_camera_cli(create_configured_camera, "Configured wrist camera"))
