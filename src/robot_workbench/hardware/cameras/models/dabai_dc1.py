"""原 Dabai DC1 彩色相机的 V4L2 后端。

保留按物理序列号定位视频设备与 OpenCV 采帧的旧相机支持；实现统一线程化接口，
不提供深度帧，供旧设备复用及独立命令行测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from robot_workbench.hardware.cameras.base import CameraError, ThreadedColorCamera


class DabaiDC1Camera(ThreadedColorCamera):
    """保留按物理序列号定位 Dabai DC1 的 V4L2 彩色采集方式。"""

    backend_name = "v4l2"
    model_name = "Dabai DC1"

    def __init__(
        self,
        name: str,
        serial: str,
        width: int = 640,
        height: int = 480,
        fps: float = 20.0,
        warmup_frames: int = 15,
    ) -> None:
        """初始化配置及内部状态，不启动设备或网络请求。"""
        super().__init__(name, serial, width, height, fps, warmup_frames)
        self.device = self.device_path(serial)
        self._capture: cv2.VideoCapture | None = None

    @staticmethod
    def device_path(serial: str) -> str:
        """根据 Dabai 序列号构造稳定的 V4L2 设备路径。"""
        return str(
            Path(
                "/dev/v4l/by-id/"
                f"usb-Sonix_Technology_Co.__Ltd._Dabai_DC1_{serial}-video-index0"
            )
        )

    def probe_info(self) -> dict[str, Any]:
        """读取相机设备信息，不启动图像采集流。"""
        path = Path(self.device)
        return {
            "name": self.name,
            "backend": self.backend_name,
            "model": self.model_name,
            "serial": self.serial,
            "device": self.device,
            "connected": path.exists(),
            "supports_depth": False,
        }

    def _open(self) -> dict[str, Any]:
        """打开相机采集流并返回实际生效的配置。"""
        if not Path(self.device).exists():
            raise CameraError(f"找不到 Dabai DC1：{self.device}")
        capture = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            raise CameraError(f"无法打开设备 {self.device}")
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        self._capture = capture
        return {
            "device": self.device,
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(capture.get(cv2.CAP_PROP_FPS)),
            "format": "MJPG",
            "supports_depth": False,
        }

    def _read_color(self) -> np.ndarray:
        """等待并读取一帧 BGR 彩色图像，失败时抛出相机错误。"""
        if self._capture is None:
            raise CameraError("Dabai DC1 尚未打开")
        ok, image = self._capture.read()
        if not ok:
            raise CameraError(f"从 {self.device} 读取彩色帧失败")
        return image

    def _close(self) -> None:
        """关闭当前后端占用的采集资源。"""
        if self._capture is not None:
            self._capture.release()
            self._capture = None
