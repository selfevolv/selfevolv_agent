"""按序列号选择相机后端并管理腕部/第三视角双相机。

工厂兼容原 Dabai 与现用 RealSense；双相机管理器统一启动、等待就绪、
读取最新帧和停止，让采集代码不直接处理设备型号差异。
"""

from __future__ import annotations

from typing import Any

from robot_workbench.hardware.cameras.base import CameraError, ThreadedColorCamera
from robot_workbench.hardware.cameras.frames import CameraFrame
from robot_workbench.hardware.cameras.models import DabaiDC1Camera, RealSenseD435iCamera


def backend_for_serial(serial: str) -> str:
    """根据序列号识别 Dabai 或 RealSense 后端，不识别时抛出相机错误。"""
    if serial.startswith("CC1"):
        return "dabai"
    if serial.isdigit():
        return "realsense"
    raise CameraError(f"无法根据序列号识别相机后端：{serial}")


def create_camera(
    name: str,
    serial: str,
    width: int = 640,
    height: int = 480,
    fps: float | None = None,
    warmup_frames: int = 15,
    backend: str = "auto",
) -> ThreadedColorCamera:
    """按型号或序列号创建相机对象；尺寸为像素、频率为 Hz，创建时不启动采流。"""
    selected = backend_for_serial(serial) if backend == "auto" else backend
    if selected == "dabai":
        return DabaiDC1Camera(
            name,
            serial,
            width,
            height,
            20.0 if fps is None else fps,
            warmup_frames,
        )
    if selected == "realsense":
        return RealSenseD435iCamera(
            name,
            serial,
            width,
            height,
            30.0 if fps is None else fps,
            warmup_frames,
        )
    raise CameraError(f"不支持的相机后端：{backend}")


class DualCameraRig:
    """统一管理第三视角与腕部相机的启动、帧快照和资源释放。"""
    def __init__(
        self,
        third_serial: str,
        wrist_serial: str,
        width: int = 640,
        height: int = 480,
        fps: float | None = None,
        warmup_frames: int = 15,
        backend: str = "auto",
    ) -> None:
        """初始化配置及内部状态，不启动设备或网络请求。"""
        self.workers: dict[str, ThreadedColorCamera] = {
            "third_person": create_camera(
                "third_person",
                third_serial,
                width,
                height,
                fps,
                warmup_frames,
                backend,
            ),
            "wrist": create_camera(
                "wrist",
                wrist_serial,
                width,
                height,
                fps,
                warmup_frames,
                backend,
            ),
        }

    def start(self) -> None:
        """启动后台采流；双相机管理器还会等待两路就绪，失败时清理已启动资源。"""
        try:
            for worker in self.workers.values():
                worker.start()
            for worker in self.workers.values():
                worker.wait_ready()
        except Exception:
            self.close(suppress_errors=True)
            raise

    def snapshots(self) -> dict[str, CameraFrame]:
        """按逻辑角色返回双相机的最新帧副本。"""
        return {name: worker.snapshot() for name, worker in self.workers.items()}

    def config(self) -> dict[str, dict[str, Any]]:
        """返回已生效的相机流配置；未配置的相机抛出相机错误。"""
        return {name: worker.config() for name, worker in self.workers.items()}

    def close(self, suppress_errors: bool = False) -> None:
        """停止本对象的采流并释放资源；线程未退出或释放失败时报告错误。"""
        errors: list[BaseException] = []
        for worker in self.workers.values():
            try:
                worker.close()
            except BaseException as exc:
                errors.append(exc)
        if errors and not suppress_errors:
            raise CameraError("；".join(str(error) for error in errors))
