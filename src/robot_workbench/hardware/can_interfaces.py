"""读取 Linux 内核登记的 CAN 接口及 USB 适配器信息。"""
from pathlib import Path


def _optional_text(path: Path) -> str | None:
    """读取并清理可选属性文本；属性缺失或内容为空时返回 None。"""
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def adapter_identity(path: Path) -> dict[str, str | None]:
    """沿设备路径查找最近的 USB 设备，返回适配器型号和序列号。"""
    for parent in path.resolve().parents:
        if (parent / 'idVendor').is_file():
            # 仅取最近 USB 设备的身份，避免误用主机控制器的序列号。
            return {'adapter_model': _optional_text(parent / 'product'),
                    'adapter_serial': _optional_text(parent / 'serial')}
    return {'adapter_model': None, 'adapter_serial': None}


def discover_can_interfaces(root: Path = Path('/sys/class/net')) -> list[dict[str, object]]:
    """只读扫描内核网络接口，返回 CAN 接口名称、启用状态及适配器身份。"""
    interfaces = []
    for path in sorted(root.iterdir()):
        try:
            # 使用 ARPHRD_CAN 类型识别接口，兼容重命名或未启用的 CAN 接口。
            if (path / 'type').read_text().strip() != '280':
                continue
            interfaces.append({
                'name': path.name,
                'up': bool(int((path / 'flags').read_text().strip(), 16) & 1),
                **adapter_identity(path),
            })
        except FileNotFoundError:
            # 扫描期间适配器可能被拔出，跳过已消失的接口。
            continue
    return interfaces
