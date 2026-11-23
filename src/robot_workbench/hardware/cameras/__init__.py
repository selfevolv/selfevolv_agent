"""PIPER 可独立导入的相机包公共入口。

导出彩色/深度帧类型、线程化相机接口、错误类型和双相机管理器；
设备型号与命令行测试入口分别位于 models/ 和 devices/。
"""

from robot_workbench.hardware.cameras.base import (
    CameraError, ThreadedColorCamera, UnsupportedDepthError,
)
from robot_workbench.hardware.cameras.frames import CameraFrame, DepthFrame
from robot_workbench.hardware.cameras.rig import DualCameraRig, create_camera

__all__ = [
    "CameraError",
    "CameraFrame",
    "DepthFrame",
    "DualCameraRig",
    "ThreadedColorCamera",
    "UnsupportedDepthError",
    "create_camera",
]
