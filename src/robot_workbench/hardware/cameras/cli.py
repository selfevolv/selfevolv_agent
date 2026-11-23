"""各型号和角色相机共用的独立命令行测试实现。

解析 info、photo、depth-photo、record 子命令及分辨率/FPS/预热参数，
输出图片、深度预览或录像与结果摘要；不启动机械臂或数据采集会话。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from robot_workbench.hardware.cameras.base import CameraError, ThreadedColorCamera


CameraFactory = Callable[..., ThreadedColorCamera]


def _parser(label: str) -> argparse.ArgumentParser:
    """创建独立相机检查的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description=f"{label} 独立测试")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--warmup-frames", type=int, default=15)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="显示设备、USB 与固件信息")
    photo = commands.add_parser("photo", help="拍摄一张彩色照片")
    photo.add_argument("--output", type=Path, required=True)
    depth = commands.add_parser("depth-photo", help="拍摄一张 uint16 毫米深度图")
    depth.add_argument("--output", type=Path, required=True)
    depth.add_argument("--preview", type=Path)
    record = commands.add_parser("record", help="录制标准彩色视频")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--seconds", type=float, default=10.0)
    return parser


def _make(factory: CameraFactory, args: argparse.Namespace) -> ThreadedColorCamera:
    """校验像素尺寸、Hz 帧率和预热帧数后创建相机。"""
    if args.width <= 0 or args.height <= 0 or args.warmup_frames < 0:
        raise CameraError("分辨率必须为正数，预热帧数不能为负数")
    if args.fps is not None and args.fps <= 0:
        raise CameraError("FPS 必须为正数")
    kwargs = {
        "width": args.width,
        "height": args.height,
        "warmup_frames": args.warmup_frames,
    }
    if args.fps is not None:
        kwargs["fps"] = args.fps
    return factory(**kwargs)


def _write_image(path: Path, image: np.ndarray) -> None:
    """创建目标目录并保存图像；写入失败时抛出相机错误。"""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image):
        raise CameraError(f"图片写入失败：{path}")


def _photo(camera: ThreadedColorCamera, output: Path) -> dict[str, object]:
    """等待相机预热就绪后保存一帧彩色图像，结束时释放资源。"""
    try:
        camera.start()
        camera.wait_ready()
        frame = camera.snapshot()
        _write_image(output, frame.image)
        return {
            "output": str(output.expanduser().resolve()),
            "shape": list(frame.image.shape),
            "sequence": frame.sequence,
            "timestamp_ns": frame.timestamp_ns,
        }
    finally:
        camera.close()


def _depth_photo(
    camera: ThreadedColorCamera,
    output: Path,
    preview: Path | None,
) -> dict[str, object]:
    """保存毫米单位深度图，并按需生成可视化预览和统计。"""
    frame = camera.capture_depth()
    _write_image(output, frame.depth_mm)
    result: dict[str, object] = {
        "output": str(output.expanduser().resolve()),
        "shape": list(frame.depth_mm.shape),
        "dtype": str(frame.depth_mm.dtype),
        "valid_depth_ratio": float(np.count_nonzero(frame.depth_mm) / frame.depth_mm.size),
        "depth_scale_m": frame.depth_scale_m,
        "sequence": frame.sequence,
    }
    if preview is not None:
        valid = frame.depth_mm[frame.depth_mm > 0]
        maximum = float(np.percentile(valid, 95)) if valid.size else 1.0
        normalized = np.clip(frame.depth_mm.astype(np.float32) * 255.0 / maximum, 0, 255)
        colorized = cv2.applyColorMap(normalized.astype(np.uint8), cv2.COLORMAP_TURBO)
        colorized[frame.depth_mm == 0] = 0
        _write_image(preview, colorized)
        result["preview"] = str(preview.expanduser().resolve())
    return result


def _record(
    camera: ThreadedColorCamera,
    output: Path,
    seconds: float,
) -> dict[str, object]:
    """在指定秒数内按帧序号录制视频，结束时释放相机和编码器。"""
    if seconds <= 0:
        raise CameraError("录像时长必须为正数")
    writer: cv2.VideoWriter | None = None
    frames_written = 0
    try:
        camera.start()
        camera.wait_ready()
        first = camera.snapshot()
        height, width = first.image.shape[:2]
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*"mp4v"),
            camera.fps,
            (width, height),
        )
        if not writer.isOpened():
            raise CameraError(f"无法创建录像：{output}")
        deadline = time.monotonic() + seconds
        last_sequence = -1
        while time.monotonic() < deadline:
            frame = camera.snapshot()
            if frame.sequence != last_sequence:
                writer.write(frame.image)
                frames_written += 1
                last_sequence = frame.sequence
            time.sleep(max(0.001, 0.25 / camera.fps))
        if frames_written == 0:
            raise CameraError("录像期间没有取得新帧")
        return {
            "output": str(output),
            "seconds": seconds,
            "fps": camera.fps,
            "frames_written": frames_written,
            "resolution": [width, height],
        }
    finally:
        if writer is not None:
            writer.release()
        camera.close()


def run_camera_cli(factory: CameraFactory, label: str) -> int:
    """执行相机信息、拍照、深度拍照或录像命令，返回命令行退出码。"""
    parser = _parser(label)
    args = parser.parse_args()
    try:
        camera = _make(factory, args)
        if args.command == "info":
            result = camera.probe_info()
        elif args.command == "photo":
            result = _photo(camera, args.output)
        elif args.command == "depth-photo":
            result = _depth_photo(camera, args.output, args.preview)
        elif args.command == "record":
            result = _record(camera, args.output, args.seconds)
        else:
            parser.error(f"未知命令：{args.command}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CameraError, OSError, ValueError) as exc:
        print(f"相机测试失败：{exc}", file=sys.stderr)
        return 2
