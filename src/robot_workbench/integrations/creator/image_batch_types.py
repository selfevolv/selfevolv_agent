"""保留接口所需的容量错误及图片输出校验依赖。"""
import io
import re
from pathlib import Path

from PIL import Image


class ImageCapacityError(ValueError):
    """携带已确认的接口容量类型和正整数上限。"""
    def __init__(self, message='图片请求超过接口容量', *, kind='tokens', limit=None):
        """初始化配置及内部状态，不启动设备或网络请求。"""
        super().__init__(message)
        self.kind = kind
        self.limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else None


class ImageBatchIncomplete(ValueError):
    """携带批量图片响应不完整时的已完成编号。"""
    def __init__(self, message='图片批量响应不完整', *, completed=()):
        """初始化配置及内部状态，不启动设备或网络请求。"""
        super().__init__(message)
        self.completed = tuple(completed)


def validate_outputs(outputs):
    """校验图片任务编号、尺寸和互不重复的目标路径。"""
    outputs = list(outputs)
    allowed = {'1152x1536', '1920x1080', '2048x1024', '1888x800', '1024x1024'}
    if not outputs or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                          or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['id'])
                          or item.get('image_size') not in allowed for item in outputs):
        raise ValueError('批量图片编号或尺寸无效')
    if len({item['id'] for item in outputs}) != len(outputs):
        raise ValueError('批量图片编号重复')
    paths = [Path(item['path']).resolve() for item in outputs]
    if len(set(paths)) != len(paths) or any(not path.parent.is_dir() for path in paths):
        raise ValueError('批量图片输出目录无效或路径重复')
    return outputs


def save_png(data, output):
    """校验原始 PNG 和长宽比，再以临时文件替换输出，失败时清理临时文件。"""
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    width, height = map(int, output['image_size'].split('x'))
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or abs(image.width / image.height - width / height) > .005:
            raise ValueError('批量图片 ' + output['id'] + ' 的 PNG 格式或比例不符')
    path = Path(output['path'])
    pending = path.with_suffix(path.suffix + '.pending')
    try:
        pending.write_bytes(data)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
