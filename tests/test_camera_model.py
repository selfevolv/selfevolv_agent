"""离线验证相机帧契约、双图 OpenAI 请求及错误处理，不访问真实网络或机械臂。"""
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
from PIL import Image

from robot_workbench.hardware.cameras import CameraError, ThreadedColorCamera, create_camera
from robot_workbench.hardware.cameras import rig
from robot_workbench.integrations.creator import api_backend as api


HTTP_CLIENT = httpx.Client


class FakeCamera(ThreadedColorCamera):
    """用递增图像模拟线程化相机，不打开设备。"""

    def __init__(self):
        """初始化小尺寸测试流及资源释放标记。"""
        super().__init__('test', 'test', 8, 6, 30, 2)
        self.closed = False

    def probe_info(self):
        """返回模拟设备身份。"""
        return {'serial': self.serial}

    def _open(self):
        """提供模拟 BGR 流配置。"""
        return {'format': 'BGR8'}

    def _read_color(self):
        """生成固定内容并让出线程执行时间。"""
        time.sleep(.002)
        return np.full((6, 8, 3), 73, dtype=np.uint8)

    def _close(self):
        """记录后端已释放。"""
        self.closed = True


class CameraTests(unittest.TestCase):
    """验证相机工厂、线程采流和双相机失败清理。"""

    def test_factory_uses_configured_serials(self):
        """验证 RealSense 序列号映射、默认帧率和未知序列号拒绝。"""
        camera = create_camera('wrist', '263022072493')
        self.assertEqual(camera.serial, '263022072493')
        self.assertEqual(camera.fps, 30)
        with self.assertRaises(CameraError):
            create_camera('wrist', 'unknown')

    def test_capture_copies_frames_and_closes(self):
        """验证预热后帧的时间、序号、独立副本和资源释放。"""
        camera = FakeCamera()
        with camera:
            first = camera.snapshot()
            first.image.fill(0)
            time.sleep(.02)
            second = camera.snapshot()
            self.assertTrue(np.all(second.image == 73))
            self.assertGreater(second.sequence, first.sequence)
            self.assertGreater(second.timestamp_ns, first.timestamp_ns)
        self.assertTrue(camera.closed)

    def test_dual_camera_failure_closes_both(self):
        """一路启动失败时，双相机管理器仍释放两路已创建资源。"""
        first, second = FakeCamera(), FakeCamera()
        with patch.object(rig, 'create_camera', side_effect=[first, second]):
            cameras = rig.DualCameraRig('1', '2')
        with patch.object(second, 'wait_ready', side_effect=CameraError('test')):
            with self.assertRaises(CameraError):
                cameras.start()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)


class ModelInterfaceTests(unittest.TestCase):
    """用真实 HTTP 序列化与假传输验证 Creator 的双图模型调用。"""

    def setUp(self):
        """准备仅用于假传输的显式模型配置。"""
        self.config = dict(base_url='https://api.openai.com/v1', api_key='test-only',
                           model='test-model', timeout=10, proxy='')

    def client(self, handler):
        """替换 HTTP 传输层，保留真实请求构造和 JSON 解析。"""
        return patch.object(api.httpx, 'Client', side_effect=lambda **kwargs:
                            HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs))

    def test_two_images_and_state_reach_model(self):
        """验证两张图的字节、提示词和状态实际进入请求，并解析 JSON 模型回复。"""
        seen = []

        def handler(request):
            """校验完整双图请求，并返回模拟的结构化模型决定。"""
            body = json.loads(request.content)
            seen.append(body)
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/chat/completions')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-only')
            self.assertEqual(body['model'], 'test-model')
            self.assertIn('只观察', body['messages'][0]['content'])
            content = body['messages'][1]['content']
            self.assertEqual(content[0]['text'], '状态：六轴位置均已提供。')
            self.assertEqual(len(content), 3)
            for part, original in zip(content[1:], images):
                encoded = part['image_url']['url'].split(',', 1)[1]
                self.assertEqual(base64.b64decode(encoded), original.read_bytes())
            return httpx.Response(200, json={'choices': [{'message': {
                'content': '{"decision":"observe","motion":false}'}}]})

        with tempfile.TemporaryDirectory() as temporary:
            images = [Path(temporary) / (role + '.png') for role in ('third', 'wrist')]
            for path in images:
                Image.new('RGB', (8, 6), (40, 80, 120)).save(path)
            with self.client(handler), patch.object(api, 'settings', side_effect=AssertionError('不得隐式读密钥')):
                result = api.run('状态：六轴位置均已提供。', images,
                                 config=self.config, system_prompt='只观察，不执行运动。')
        self.assertEqual(result, {'decision': 'observe', 'motion': False})
        self.assertEqual(len(seen), 1)

    def test_remote_error_body_is_not_exposed(self):
        """接口失败时不将远端正文中的请求素材或密钥带入异常。"""
        with self.client(lambda request: httpx.Response(401, text='private request material')):
            with self.assertRaises(api.APIError) as error:
                api.run('test', config=self.config)
        self.assertIn('401', str(error.exception))
        self.assertNotIn('private', str(error.exception))

    def test_invalid_response_is_rejected(self):
        """拒绝无 choices 和非 JSON 的模型回复，不伪造可执行结果。"""
        for payload in ({}, {'choices': [{'message': {'content': 'not json'}}]}):
            with self.subTest(payload=payload), self.client(lambda request: httpx.Response(200, json=payload)):
                with self.assertRaises(api.APIError):
                    api.run('test', config=self.config)

    def test_missing_configuration_does_not_send(self):
        """缺少模型名称时在网络调用前拒绝请求。"""
        with patch.object(api.httpx, 'Client') as client:
            with self.assertRaises(api.APIConfigurationError):
                api.run('test', config={**self.config, 'model': ''})
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
