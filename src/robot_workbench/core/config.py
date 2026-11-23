"""集中管理 PIPER 设备角色、相机和采集/训练相关的 INI 配置。

定义允许的参数选项和配置数据结构；加载时校验角色与数值，保存时保留原有注释
并原子替换文件，供命令行程序和 Web API 共用。
"""

from __future__ import annotations

from robot_workbench.collection.schema import pyagx_repo_id

import configparser
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path


from .paths import PROJECT_ROOT, DEFAULT_DATA_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".runtime/config/piper_config.ini"
_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_]+)\s*=.*$")
_WRITE_LOCK = threading.Lock()

COLLECTION_HZ_OPTIONS = (10.0, 15.0, 20.0, 30.0)
COLLECTION_RESOLUTION_OPTIONS = ((640, 480), (1280, 720))
COLLECTION_WARMUP_OPTIONS = (15, 60, 120, 180)
COLLECTION_JPEG_QUALITY_OPTIONS = (85, 90, 95)


class ConfigError(ValueError):
    """表示设备配置缺失、格式错误或数值不满足约束。"""


@dataclass(frozen=True)
class PiperConfig:
    """保存设备角色和运行配置；频率为 Hz、时间为秒、夹爪力为 mN，图像时差为 ms。"""
    available_camera_serials: tuple[str, ...]
    third_person_serial: str
    wrist_serial: str
    master_can: str
    follower_can: str
    collection_hz: float = 20.0
    collection_task: str = "主从采集"
    collection_output_root: Path = DEFAULT_DATA_ROOT
    collection_repo_id: str = "local/piper_master_slave"
    convert_after_collection: bool = False
    sync_speed: int = 10
    follow_speed: int = 30
    gripper_effort: int = 1000
    max_tracking_error_deg: float = 60.0
    tracking_error_seconds: float = 2.0
    feedback_timeout_seconds: float = 0.30
    openpi_root: Path = PROJECT_ROOT.parent / "github/openpi"
    camera_backend: str = "auto"
    camera_width: int = 640
    camera_height: int = 480
    camera_fps: float | None = None
    camera_warmup_frames: int = 120
    jpeg_quality: int = 95
    max_camera_age_ms: float = 150.0
    dataset_paths: tuple[str, ...] = ()
    control_mode: str = "master_slave"
    can_firmware: dict[str, str] = field(default_factory=dict)
    mixed_initial_joints_deg: tuple[float, ...] = ()

    def follower_interfaces(self) -> tuple[str, ...]:
        """按控制模式返回从臂接口；单臂模式同时纳入内核发现的 CAN 接口并去重。"""
        if self.control_mode == "master_slave":
            return (self.follower_can,)
        from robot_workbench.hardware.can_interfaces import discover_can_interfaces
        return tuple(dict.fromkeys((self.follower_can, self.master_can,
                                   *(item["name"] for item in discover_can_interfaces()))))

    def validate(self) -> None:
        """校验角色、固件档位、相机参数和存储路径；无效配置抛出 ConfigError。"""
        if self.control_mode not in {"master_slave", "follower"}:
            raise ConfigError("控制模式只能选择主从控制模式或从臂模式")
        serials = tuple(serial.strip() for serial in self.available_camera_serials)
        if not serials or any(not serial for serial in serials):
            raise ConfigError("available_serials 至少需要一个有效相机序列号")
        if len(set(serials)) != len(serials):
            raise ConfigError("available_serials 不能包含重复序列号")
        unsupported = [
            serial for serial in serials if not (serial.isdigit() or serial.startswith("CC1"))
        ]
        if unsupported:
            raise ConfigError(f"不支持的相机序列号格式：{', '.join(unsupported)}")
        if self.third_person_serial not in serials or self.wrist_serial not in serials:
            raise ConfigError("第三视角和腕部相机必须来自 available_serials")
        if self.third_person_serial == self.wrist_serial:
            raise ConfigError("第三视角和腕部不能使用同一台相机")
        if not self.master_can.strip() or not self.follower_can.strip():
            raise ConfigError("主臂和从臂 CAN 接口不能为空")
        if any(char.isspace() for char in self.master_can + self.follower_can):
            raise ConfigError("CAN 接口名称不能包含空白字符")
        if self.master_can == self.follower_can:
            raise ConfigError("主臂和从臂不能使用同一个 CAN 接口")
        if not isinstance(self.can_firmware, dict) or any(
            not isinstance(channel, str) or not channel or any(char.isspace() for char in channel)
            or not isinstance(profile, str) or profile not in {"default", "v183", "v188", "v189"}
            for channel, profile in self.can_firmware.items()
        ):
            raise ConfigError("CAN 固件配置必须是接口名到 default、v183、v188 或 v189 的映射")
        if (not isinstance(self.mixed_initial_joints_deg, tuple) or
            len(self.mixed_initial_joints_deg) not in (0, 6) or any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in self.mixed_initial_joints_deg)):
            raise ConfigError('混合推理初始姿态必须为六个有限角度值')
        if self.collection_hz not in COLLECTION_HZ_OPTIONS:
            raise ConfigError("采集频率只能选择 10、15、20 或 30 Hz")
        if not 1 <= self.sync_speed <= 100 or not 1 <= self.follow_speed <= 100:
            raise ConfigError("同步和跟随速度必须在 1..100")
        if not 0 <= self.gripper_effort <= 3000:
            raise ConfigError("夹爪夹持力（mN）必须在 0..3000")
        if self.max_tracking_error_deg <= 0 or self.tracking_error_seconds <= 0:
            raise ConfigError("跟踪误差阈值和持续时间必须大于 0")
        if self.feedback_timeout_seconds <= 0:
            raise ConfigError("反馈超时时间必须大于 0")
        if not self.openpi_root.expanduser().is_absolute():
            raise ConfigError("OpenPI 目录必须是绝对路径")
        if self.camera_backend not in {"auto", "realsense", "dabai"}:
            raise ConfigError("相机后端只能是 auto、realsense 或 dabai")
        allowed_resolutions = collection_resolution_options(
            self.third_person_serial, self.wrist_serial
        )
        if (self.camera_width, self.camera_height) not in allowed_resolutions:
            choices = "、".join(f"{width}×{height}" for width, height in allowed_resolutions)
            raise ConfigError(f"当前相机组合的分辨率只能选择：{choices}")
        allowed_fps = collection_camera_fps_options(
            self.third_person_serial, self.wrist_serial
        )
        if self.camera_fps not in allowed_fps:
            choices = "、".join("自动" if value is None else f"{value:g}" for value in allowed_fps)
            raise ConfigError(f"当前相机组合的相机帧率只能选择：{choices}")
        default_fps = min(
            20.0 if serial.startswith("CC1") else 30.0
            for serial in (self.third_person_serial, self.wrist_serial)
        )
        effective_camera_fps = self.camera_fps or default_fps
        if self.collection_hz > effective_camera_fps:
            raise ConfigError("采集频率不能高于相机帧率，否则会产生重复图像")
        if self.camera_warmup_frames not in COLLECTION_WARMUP_OPTIONS:
            raise ConfigError("相机预热只能选择 15、60、120 或 180 帧")
        if self.jpeg_quality not in COLLECTION_JPEG_QUALITY_OPTIONS:
            raise ConfigError("JPEG 质量只能选择 85、90 或 95")
        if self.max_camera_age_ms <= 0:
            raise ConfigError("最大图像时差必须大于 0")
        if not self.collection_task.strip() or "\n" in self.collection_task:
            raise ConfigError("任务描述不能为空或包含换行")
        repo_parts = self.collection_repo_id.split("/")
        if len(repo_parts) != 2 or any(not part.strip() for part in repo_parts):
            raise ConfigError("数据集名称必须为 owner/dataset")
        if not self.collection_output_root.expanduser().is_absolute():
            raise ConfigError("数据保存目录必须是绝对路径")
        if len(set(self.dataset_paths)) != len(self.dataset_paths):
            raise ConfigError("数据集路径不能重复")
        if any(not Path(path).expanduser().is_absolute() or "\n" in path
               for path in self.dataset_paths):
            raise ConfigError("数据集路径必须是无换行的绝对路径")


def collection_camera_fps_options(
    third_person_serial: str, wrist_serial: str
) -> tuple[float | None, ...]:
    """根据双相机序列号确定可选帧率（Hz）；None 表示自动选择。"""
    backends = {
        "dabai" if serial.startswith("CC1") else "realsense"
        for serial in (third_person_serial, wrist_serial)
    }
    if backends == {"realsense"}:
        return (None, 15.0, 30.0)
    if backends == {"dabai"}:
        return (None, 20.0)
    return (None,)


def collection_resolution_options(
    third_person_serial: str, wrist_serial: str
) -> tuple[tuple[int, int], ...]:
    """根据双相机类型返回共同支持的图像分辨率（像素）。"""
    if all(
        serial.isdigit() for serial in (third_person_serial, wrist_serial)
    ):
        return COLLECTION_RESOLUTION_OPTIONS
    return (COLLECTION_RESOLUTION_OPTIONS[0],)


def config_path() -> Path:
    """优先解析 PIPER_CONFIG_PATH 指定的配置路径，否则使用项目默认路径。"""
    override = os.environ.get("PIPER_CONFIG_PATH")
    return Path(override).expanduser().resolve() if override else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> PiperConfig:
    """只读加载指定或默认 INI 配置并完整校验；缺失或无效时抛出 ConfigError。"""
    selected = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    if not selected.is_file():
        raise ConfigError(f"找不到配置文件：{selected}")
    try:
        with selected.open(encoding="utf-8") as file_handle:
            parser.read_file(file_handle)
        serials = tuple(
            serial.strip()
            for serial in parser.get("cameras", "available_serials").split(",")
        )
        result = PiperConfig(
            available_camera_serials=serials,
            third_person_serial=parser.get("cameras", "third_person_serial").strip(),
            wrist_serial=parser.get("cameras", "wrist_serial").strip(),
            master_can=parser.get("arms", "master_can").strip(),
            follower_can=parser.get("arms", "follower_can").strip(),
            control_mode=parser.get("arms", "control_mode", fallback="master_slave").strip(),
            can_firmware=json.loads(parser.get("arms", "firmware", fallback="{}")),
            mixed_initial_joints_deg=tuple(json.loads(parser.get("arms", "mixed_initial_joints_deg", fallback="[]"))),
            collection_hz=parser.getfloat("collection", "hz"),
            collection_task=parser.get("collection", "task").strip(),
            collection_output_root=Path(
                parser.get("collection", "output_root").strip()
            ).expanduser(),
            collection_repo_id=parser.get("collection", "repo_id").strip(),
            convert_after_collection=parser.getboolean(
                "collection", "convert_after_collection"
            ),
            sync_speed=parser.getint("collection", "sync_speed"),
            follow_speed=parser.getint("collection", "follow_speed"),
            gripper_effort=parser.getint("collection", "gripper_effort"),
            max_tracking_error_deg=parser.getfloat(
                "collection", "max_tracking_error_deg"
            ),
            tracking_error_seconds=parser.getfloat(
                "collection", "tracking_error_seconds"
            ),
            feedback_timeout_seconds=parser.getfloat(
                "collection", "feedback_timeout_seconds"
            ),
            openpi_root=Path(parser.get("collection", "openpi_root").strip()).expanduser(),
            camera_backend=parser.get("collection", "camera_backend").strip(),
            camera_width=parser.getint("collection", "camera_width"),
            camera_height=parser.getint("collection", "camera_height"),
            camera_fps=(
                None
                if parser.get("collection", "camera_fps").strip().lower() == "auto"
                else parser.getfloat("collection", "camera_fps")
            ),
            camera_warmup_frames=parser.getint(
                "collection", "camera_warmup_frames"
            ),
            jpeg_quality=parser.getint("collection", "jpeg_quality"),
            max_camera_age_ms=parser.getfloat("collection", "max_camera_age_ms"),
            dataset_paths=tuple(json.loads(parser.get("analysis", "dataset_paths"))),
        )
    except (configparser.Error, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"配置文件格式错误：{exc}") from exc
    result.validate()
    return result


def default_lerobot_dataset_path(config: PiperConfig | None = None) -> Path:
    """优先使用环境指定的数据集路径，否则按配置生成采用夹持力语义的导出目录。"""
    override = os.environ.get("LEROBOT_DATASET_ROOT")
    if override:
        return Path(override).expanduser()
    selected = config or load_config()
    return (
        selected.collection_output_root.expanduser()
        / "lerobot_v21"
        / Path(pyagx_repo_id(selected.collection_repo_id))
    )


def save_config(config: PiperConfig, path: Path | None = None) -> None:
    """校验配置后保留原有 INI 注释，以临时文件原子替换目标配置；会写入磁盘。"""
    config.validate()
    selected = path or config_path()
    replacements = {
        ("cameras", "available_serials"): ", ".join(config.available_camera_serials),
        ("cameras", "third_person_serial"): config.third_person_serial,
        ("cameras", "wrist_serial"): config.wrist_serial,
        ("arms", "master_can"): config.master_can,
        ("arms", "follower_can"): config.follower_can,
        ("arms", "control_mode"): config.control_mode,
        ("arms", "firmware"): json.dumps(config.can_firmware, ensure_ascii=False, separators=(",", ":")),
        ("arms", "mixed_initial_joints_deg"): json.dumps(list(config.mixed_initial_joints_deg)),
        ("collection", "hz"): f"{config.collection_hz:g}",
        ("collection", "task"): config.collection_task.strip(),
        ("collection", "output_root"): str(config.collection_output_root.expanduser()),
        ("collection", "repo_id"): config.collection_repo_id.strip(),
        ("collection", "convert_after_collection"): (
            "yes" if config.convert_after_collection else "no"
        ),
        ("collection", "sync_speed"): str(config.sync_speed),
        ("collection", "follow_speed"): str(config.follow_speed),
        ("collection", "gripper_effort"): str(config.gripper_effort),
        ("collection", "max_tracking_error_deg"): f"{config.max_tracking_error_deg:g}",
        ("collection", "tracking_error_seconds"): f"{config.tracking_error_seconds:g}",
        ("collection", "feedback_timeout_seconds"): f"{config.feedback_timeout_seconds:g}",
        ("collection", "openpi_root"): str(config.openpi_root.expanduser()),
        ("collection", "camera_backend"): config.camera_backend,
        ("collection", "camera_width"): str(config.camera_width),
        ("collection", "camera_height"): str(config.camera_height),
        ("collection", "camera_fps"): (
            "auto" if config.camera_fps is None else f"{config.camera_fps:g}"
        ),
        ("collection", "camera_warmup_frames"): str(
            config.camera_warmup_frames
        ),
        ("collection", "jpeg_quality"): str(config.jpeg_quality),
        ("collection", "max_camera_age_ms"): f"{config.max_camera_age_ms:g}",
        ("analysis", "dataset_paths"): json.dumps(
            list(config.dataset_paths), ensure_ascii=False, separators=(",", ":")
        ),
    }
    with _WRITE_LOCK:
        lines = selected.read_text(encoding="utf-8").splitlines(keepends=True)
        section = ""
        found: set[tuple[str, str]] = set()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip().lower()
                continue
            match = _KEY_LINE.match(line.rstrip("\r\n"))
            if match is None:
                continue
            key = match.group("key").lower()
            identity = (section, key)
            if identity in replacements:
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                lines[index] = (
                    f"{match.group('indent')}{key} = {replacements[identity]}{newline}"
                )
                found.add(identity)
        missing = set(replacements) - found
        for key in ("control_mode", "firmware", "mixed_initial_joints_deg"):
            if ("arms", key) in missing:
                index = next(index for index, line in enumerate(lines) if line.strip().lower() == "[arms]")
                lines.insert(index + 1, f"{key} = {replacements['arms', key]}\n")
                missing.remove(("arms", key))
        if missing:
            names = ", ".join(f"[{section}] {key}" for section, key in sorted(missing))
            raise ConfigError(f"配置文件缺少必要字段，无法安全更新：{names}")
        temporary = selected.with_suffix(f"{selected.suffix}.tmp")
        temporary.write_text("".join(lines), encoding="utf-8")
        os.replace(temporary, selected)
