"""仅提取 Creator 的配置读取与 JSON 解析辅助逻辑，不加载媒体编辑应用。"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

APP_DIR = Path(os.environ.get('CREATOR_ROOT', Path(__file__).resolve().parents[1])).resolve()


CONFIG_ROOT = APP_DIR / '.novafetch'


LLM_SETTINGS_PATH = CONFIG_ROOT / 'llm_settings.json'


ENV_LLM_BASE_URL = os.getenv('NOVAFETCH_LLM_BASE_URL', 'https://api.openai.com/v1')


ENV_LLM_API_KEY = os.getenv('NOVAFETCH_LLM_API_KEY', '')


ENV_LLM_MODEL = os.getenv('NOVAFETCH_LLM_MODEL', '')


ENV_LLM_TIMEOUT = int(os.getenv('NOVAFETCH_LLM_TIMEOUT', '180'))


def load_persistent_llm_settings() -> dict:
    """读取统一 API 配置，保留已保存值和环境变量默认值；不写入配置。"""
    defaults = {'base_url': ENV_LLM_BASE_URL, 'model': ENV_LLM_MODEL, 'api_key': ENV_LLM_API_KEY,
                'image_model': 'gpt-image-2', 'timeout': ENV_LLM_TIMEOUT, 'proxy': '',
                'primary_provider': 'custom_api', 'fallback_provider': 'codex'}
    try:
        payload = json.loads(LLM_SETTINGS_PATH.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return defaults
        return {key: (int(payload.get(key, default) or default) if key == 'timeout' else
                      str(payload.get(key, default)).strip()) for key,default in defaults.items()}
    except (OSError, ValueError, TypeError):
        return defaults


def clean_text(text) -> str:
    """将可选文本转为去除首尾空白的字符串。"""
    if text is None:
        return ''
    return str(text).strip()


def extract_json_object(text: str) -> dict:
    """从模型文本中解析 JSON 对象，兼容代码围栏或外围说明文字。"""
    content = clean_text(text)
    if content.startswith('```'):
        content = re.sub('^```(?:json)?\\s*', '', content, flags=re.IGNORECASE)
        content = re.sub('\\s*```$', '', content)
    try:
        value = json.loads(content)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = content.find('{')
    end = content.rfind('}')
    if start >= 0 and end > start:
        value = json.loads(content[start:end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError('大模型没有返回可解析的 JSON 对象。')
