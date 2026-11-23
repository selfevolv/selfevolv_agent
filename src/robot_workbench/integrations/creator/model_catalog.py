"""复用 Creator 的端点与代理校验、模型列表查询。"""
import json
import logging
import re
from time import sleep
from urllib.parse import urlsplit, urlunsplit

import httpx


logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """表示 API 地址、代理或认证配置无效。"""
    pass


def endpoint(base_url, operation='chat/completions'):
    """校验基础 URL 并拼接目标 API 路径，兼容已有端点后缀。"""
    url = urlsplit(base_url.strip())
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment or any(char.isspace() for char in url.netloc):
        raise ValueError('请输入有效的大模型 API 地址')
    url.port  # 请求前先校验显式端口是否合法。
    path = url.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path and url.hostname == 'api.openai.com':
        path = '/v1'
    return urlunsplit((url.scheme, url.netloc, path + '/' + operation, '', ''))


def proxy_url(value=''):
    """校验无认证和附加路径的 HTTP 或 HTTPS 代理地址；空值表示不配置代理。"""
    if not value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                or url.path not in {'', '/'} or url.query or url.fragment
                or any(char.isspace() for char in value)):
            raise ValueError()
        url.port
    except ValueError:
        raise ConfigurationError('请输入有效的 HTTP 或 HTTPS 代理地址，不支持账号密码、路径或查询参数') from None
    return value


def list_models(base_url, key, timeout=30, proxy=''):
    """请求模型列表并校验响应；仅对首次连接类错误重试一次，返回去重的模型名称。"""
    url = endpoint(base_url, 'models')
    key = key.strip()
    if urlsplit(url).hostname == 'api.openai.com' and not key:
        raise ConfigurationError('请先保存 OpenAI API Key')
    if not key.isascii() or any(char.isspace() for char in key):
        raise ConfigurationError('API Key 格式无效')
    try:
        with httpx.Client(timeout=httpx.Timeout(min(timeout, 30)), follow_redirects=False, proxy=proxy_url(proxy)) as client:
            for attempt in range(2):
                try:
                    response = client.get(url, headers={'Authorization': 'Bearer ' + key} if key else {})
                    break
                except httpx.HTTPError as error:
                    logger.warning('model_list transport_failure type=%s attempt=%s', type(error).__name__, attempt + 1)
                    if attempt or not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
                        raise
                    sleep(.25)
    except httpx.TimeoutException:
        raise ValueError('获取模型列表超时，请重试') from None
    except httpx.HTTPError:
        raise ValueError('无法连接模型 API，请稍后更新模型列表') from None
    if not response.is_success:
        logger.warning('model_list http_failure status=%s attempts=%s', response.status_code, attempt + 1)
        detail = {}
        try:
            error = response.json().get('error')
            if isinstance(error, dict):
                detail = {name: error[name] for name in ('type', 'code')
                          if isinstance(error.get(name), str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', error[name]) and error[name] != key}
        except (ValueError, AttributeError):
            pass
        raise ValueError(f'获取模型列表失败：HTTP {response.status_code}' + (' ' + json.dumps(detail) if detail else ''))
    try:
        value = response.json()
    except ValueError:
        raise ValueError('模型列表接口返回的不是 JSON') from None
    if not isinstance(value, dict) or not isinstance(value.get('data'), list) or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip()
            for item in value['data']):
        raise ValueError('模型列表接口返回的数据格式无效')
    return sorted({item['id'].strip() for item in value['data']})
