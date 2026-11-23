"""Intel RealSense D435i 的 librealsense 采帧后端。

按序列号启动彩色流，处理预热、时间戳和颜色格式；深度流仅用于独立诊断拍照，
采集数据集仍只记录两路 RGB 图像。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from robot_workbench.hardware.cameras.base import CameraError, ThreadedColorCamera
from robot_workbench.hardware.cameras.frames import DepthFrame


def _rs():
    """按需导入 RealSense SDK，缺少依赖时报告相机错误。"""
    try:
        import pyrealsense2 as rs
    except ImportError as exc:
        raise CameraError(
            "缺少 pyrealsense2；请在当前项目环境安装 requirements_collect.txt"
        ) from exc
    return rs


class RealSenseD435iCamera(ThreadedColorCamera):
    """使用 librealsense 按物理序列号操作 D435i 彩色流及独立深度诊断。"""
    backend_name = "librealsense"
    model_name = "Intel RealSense D435i"

    def __init__(
        self,
        name: str,
        serial: str,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        warmup_frames: int = 15,
    ) -> None:
        """初始化配置及内部状态，不启动设备或网络请求。"""
        super().__init__(name, serial, width, height, fps, warmup_frames)
        self._pipeline: Any | None = None

    def _device(self):
        """按物理序列号查找当前 RealSense 设备，未找到时报告错误。"""
        rs = _rs()
        for device in rs.context().query_devices():
            if device.get_info(rs.camera_info.serial_number) == self.serial:
                return device
        raise CameraError(f"找不到 RealSense 序列号 {self.serial}")

    def probe_info(self) -> dict[str, Any]:
        """读取相机设备信息，不启动图像采集流。"""
        rs = _rs()
        device = self._device()
        fields = {
            "model": rs.camera_info.name,
            "serial": rs.camera_info.serial_number,
            "firmware": rs.camera_info.firmware_version,
            "physical_port": rs.camera_info.physical_port,
            "usb_type": rs.camera_info.usb_type_descriptor,
            "product_line": rs.camera_info.product_line,
        }
        result = {
            "name": self.name,
            "backend": self.backend_name,
            "connected": True,
            "supports_depth": True,
        }
        result.update(
            {
                label: device.get_info(key)
                for label, key in fields.items()
                if device.supports(key)
            }
        )
        return result

    def _open(self) -> dict[str, Any]:
        """打开相机采集流并返回实际生效的配置。"""
        rs = _rs()
        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.color,
            self.width,
            self.height,
            rs.format.bgr8,
            int(self.fps),
        )
        try:
            profile = pipeline.start(config)
        except RuntimeError as exc:
            raise CameraError(f"RealSense {self.serial} 彩色流启动失败：{exc}") from exc
        self._pipeline = pipeline
        device = profile.get_device()
        return {
            "physical_port": device.get_info(rs.camera_info.physical_port),
            "usb_type": device.get_info(rs.camera_info.usb_type_descriptor),
            "firmware": device.get_info(rs.camera_info.firmware_version),
            "format": "BGR8",
            "supports_depth": True,
            "dataset_streams": ["color"],
        }

    def _read_color(self) -> np.ndarray:
        """等待并读取一帧 BGR 彩色图像，失败时抛出相机错误。"""
        if self._pipeline is None:
            raise CameraError("RealSense 尚未打开")
        try:
            frame = self._pipeline.wait_for_frames(5000).get_color_frame()
        except RuntimeError as exc:
            raise CameraError(f"RealSense {self.serial} 彩色帧超时：{exc}") from exc
        if not frame:
            raise CameraError(f"RealSense {self.serial} 未返回彩色帧")
        return np.asanyarray(frame.get_data()).copy()

    def _close(self) -> None:
        """关闭当前后端占用的采集资源。"""
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None

    def capture_depth(self) -> DepthFrame:
        """独立采集一帧毫米单位深度图；不支持深度或已启动彩色流时拒绝。"""
        if self._thread is not None:
            raise CameraError("独立深度拍照不能与后台彩色采集同时运行")
        rs = _rs()
        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.depth,
            self.width,
            self.height,
            rs.format.z16,
            int(self.fps),
        )
        try:
            profile = pipeline.start(config)
            frame = None
            for _ in range(max(1, self.warmup_frames + 1)):
                frame = pipeline.wait_for_frames(5000).get_depth_frame()
            if not frame:
                raise CameraError(f"RealSense {self.serial} 未返回深度帧")
            raw = np.asanyarray(frame.get_data())
            scale = profile.get_device().first_depth_sensor().get_depth_scale()
            depth_mm = np.rint(raw.astype(np.float64) * scale * 1000.0)
            depth_mm = np.clip(depth_mm, 0, np.iinfo(np.uint16).max).astype(np.uint16)
            return DepthFrame(
                depth_mm=depth_mm,
                timestamp_ns=time.perf_counter_ns(),
                sequence=int(frame.get_frame_number()),
                depth_scale_m=float(scale),
            )
        except RuntimeError as exc:
            raise CameraError(f"RealSense {self.serial} 深度拍照失败：{exc}") from exc
        finally:
            try:
                pipeline.stop()
            except RuntimeError:
                pass
