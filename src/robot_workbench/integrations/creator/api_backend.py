"""复用 Creator 的 OpenAI 兼容 HTTP 适配器，处理图片输入、JSON 响应和请求错误。"""
import base64
import io
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from PIL import Image

from . import model_catalog
from .model_catalog import endpoint
from .image_batch_types import ImageCapacityError, ImageBatchIncomplete, validate_outputs, save_png


class APIError(ValueError):
    """表示模型 API 的请求或响应内容不满足接口约定。"""
    pass


class APIConfigurationError(APIError):
    """表示调用模型前缺少有效地址、模型或必要认证信息。"""
    pass


def capacity_error(response):
    """只提取已知容量错误类型和正整数上限，避免传播远端响应正文。"""
    codes = {'context_length_exceeded': 'tokens', 'max_context_length_exceeded': 'tokens',
             'token_limit_exceeded': 'tokens', 'request_too_large': 'bytes',
             'payload_too_large': 'bytes', 'too_many_images': 'images',
             'image_limit_exceeded': 'images', 'too_many_output_images': 'outputs',
             'output_image_limit_exceeded': 'outputs'}
    if response.status_code not in {400, 413, 422}:
        return None
    detail = {}
    try:
        value = response.json()
        if isinstance(value, dict) and isinstance(value.get('error'), dict):
            detail = value['error']
    except ValueError:
        pass
    kind = codes.get(detail.get('code')) if isinstance(detail.get('code'), str) else None
    if not kind and response.status_code != 413:
        return None
    kind = kind or 'bytes'
    keys = {'tokens': ('max_tokens', 'context_length', 'limit'), 'bytes': ('max_bytes', 'limit'),
            'images': ('max_images', 'limit'), 'outputs': ('max_output_images', 'limit')}[kind]
    limit = next((detail[key] for key in keys if isinstance(detail.get(key), int)
                  and not isinstance(detail[key], bool) and detail[key] > 0), None)
    return ImageCapacityError(f'模型 API 请求超过容量（HTTP {response.status_code}，{kind}）', kind=kind, limit=limit)


def configuration_status(config):
    """检查端点、代理、模型和 OpenAI 必需密钥，返回是否可调用及原因。"""
    try:
        url = endpoint(config.get('base_url', ''))
    except ValueError:
        return {'configured': False, 'ready': False, 'reason': '请先在设置中配置有效的 API 地址'}
    try:
        model_catalog.proxy_url(config.get('proxy', ''))
    except model_catalog.ConfigurationError as error:
        return {'configured': False, 'ready': False, 'reason': str(error)}
    reason = ''
    if not config.get('model', '').strip():
        reason = '请先在设置中配置模型名称'
    elif urlsplit(url).hostname == 'api.openai.com' and not config.get('api_key', '').strip():
        reason = '请先在设置中配置 OpenAI API Key'
    return {'configured': not reason, 'ready': not reason, 'reason': reason}


def settings(private=False):
    """读取统一配置；非私有调用隐藏 API Key，仅返回密钥存在标记。"""
    from .engine import load_persistent_llm_settings
    config = load_persistent_llm_settings()
    if private:
        return config
    return {key: value for key,value in config.items() if key != 'api_key'} | {
        'has_api_key': bool(config.get('api_key')), **configuration_status(config)}


def models():
    """使用统一配置查询模型列表，并转换为接口层错误。"""
    config = settings(True)
    try:
        return model_catalog.list_models(config['base_url'], config.get('api_key', ''), config['timeout'], proxy=config.get('proxy', ''))
    except model_catalog.ConfigurationError as error:
        raise APIConfigurationError(str(error)) from None
    except ValueError as error:
        raise APIError(str(error)) from None


def send(config, body, operation, timeout=None, on_phase=None):
    """按秒级超时发送 JSON 请求；拒绝重定向，校验响应并避免暴露远端错误正文。"""
    headers = {'Content-Type': 'application/json'}
    if config.get('api_key', '').strip():
        headers['Authorization'] = 'Bearer ' + config['api_key'].strip()
    limit = min(timeout, config['timeout']) if timeout else config['timeout']
    with httpx.Client(timeout=httpx.Timeout(limit), follow_redirects=False, proxy=model_catalog.proxy_url(config.get('proxy', ''))) as client:
        options = {}
        if on_phase:
            names = {'send_request_body.complete': 'request_body_sent',
                     'receive_response_headers.complete': 'response_headers',
                     'receive_response_body.complete': 'response_body_received'}
            def trace(name, info):
                """将 HTTP 传输阶段映射为调用方可识别的进度事件。"""
                for suffix, phase in names.items():
                    if name.endswith(suffix):
                        on_phase(phase)
            options['extensions'] = {'trace': trace}
            on_phase('http_request_started')
        response = client.post(endpoint(config['base_url'], operation), json=body, headers=headers, **options)
    if response.status_code in {408, 504}:
        raise httpx.ReadTimeout('模型 API 请求超时')
    if not response.is_success:
        # 远端错误正文可能包含凭据和请求素材，因此不直接传播。
        capacity = capacity_error(response)
        if capacity:
            raise capacity
        raise APIError(f'模型 API 返回 HTTP {response.status_code}')
    try:
        value = response.json()
    except ValueError:
        raise APIError('模型 API 返回的不是 JSON') from None
    if not isinstance(value, dict):
        raise APIError('模型 API 返回的响应必须是对象')
    return value


def run(prompt, images=(), *, output_image=None, search=False, on_output=None, on_usage=None,
        timeout=None, on_sources=None, config=None, system_prompt='', image_size='1152x1536', on_phase=None):
    """发送文本与本地图片并解析 JSON；显式 config 可避免读取 Creator 配置，支持原有可选检索或图片输出分支。"""
    config = settings(True) if config is None else config
    status = configuration_status(config)
    if not status['configured']:
        raise APIConfigurationError(status['reason'])
    if output_image and image_size not in {'1152x1536', '1920x1080', '2048x1024', '1888x800', '1024x1024'}:
        raise ValueError('图片尺寸无效')
    tools = bool(search or output_image)
    if on_phase:
        on_phase('preparing_request')
    content = [{'type': 'input_text' if tools else 'text', 'text': prompt}]
    for path in images:
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or 'image/png'
        data = f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode()
        content.append({'type': 'input_image', 'image_url': data} if tools else
                       {'type': 'image_url', 'image_url': {'url': data}})
    if tools:
        body = {'model': config['model'], 'input': [{'role': 'user', 'content': content}], 'store': False}
        if output_image:
            # 原生生成尺寸须为 16 的倍数，原工作流随后再导出 1080p。
            native_size = '2048x1152' if image_size == '1920x1080' else image_size
            body.update(tools=[{'type':'image_generation', 'model':config['image_model'], 'size':native_size, 'output_format':'png'}],
                        tool_choice={'type':'image_generation'})
        else:
            body.update(tools=[{'type':'web_search'}],
                        tool_choice='required', include=['web_search_call.action.sources'])
    else:
        messages = [{'role':'system','content':(system_prompt + '\n\n' if system_prompt else '') + 'Return a JSON object.'}]
        messages.append({'role':'user','content':content if images else prompt})
        body = {'model':config['model'], 'messages':messages, 'response_format':{'type':'json_object'}}
    request_config = {**config, 'timeout': timeout} if output_image and timeout is not None else config
    if on_phase:
        on_phase('request_prepared')
    try:
        value = send(request_config, body, 'responses' if tools else 'chat/completions', timeout,
                     **({'on_phase': on_phase} if on_phase else {}))
    except ImageCapacityError as error:
        # 容量元数据仅由批量协调器消费，此处保留原有异常行为。
        raise APIError(str(error)) from None
    sources = []
    try:
        if not tools:
            choices = value.get('choices')
            if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict) or not isinstance(choices[0].get('message'),dict):
                raise APIError('模型 API 未返回有效 choices')
            text = choices[0]['message'].get('content')
            if isinstance(text,list):
                text = ''.join(part.get('text','') for part in text if isinstance(part,dict))
        else:
            if value.get('status') != 'completed':
                raise APIError('模型 API 未完成生成')
            outputs = value.get('output')
            if not isinstance(outputs,list) or any(not isinstance(item,dict) for item in outputs):
                raise APIError('模型 API 返回的输出列表无效')
            if output_image:
                encoded = next(item['result'] for item in outputs if item.get('type') == 'image_generation_call' and item.get('result'))
                data = base64.b64decode(encoded,validate=True)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    ratio = {'1152x1536': .75, '1920x1080': 16 / 9, '2048x1024': 2, '1888x800': 2.36, '1024x1024': 1}[image_size]
                    if image.format != 'PNG' or abs(image.width/image.height - ratio) > .005:
                        raise APIError('模型 API 图片必须为 ' + {.75: '3:4', 16 / 9: '16:9', 2: '2:1'}.get(ratio, image_size) + ' PNG')
            else:
                messages = [item for item in outputs if item.get('type') == 'message']
                if any(not isinstance(item.get('content'),list) or any(not isinstance(part,dict) for part in item['content']) for item in messages):
                    raise APIError('模型 API 返回的消息内容无效')
                text = ''.join(part.get('text','') for item in messages for part in item['content'] if part.get('type') == 'output_text')
                for item in outputs:
                    if item.get('type') == 'web_search_call':
                        action = item.get('action')
                        if not isinstance(action,dict):
                            raise APIError('模型 API 返回的检索来源无效')
                        found = action.get('sources', [])
                        if not isinstance(found,list) or any(not isinstance(source,dict) for source in found):
                            raise APIError('模型 API 返回的检索来源无效')
                        if action.get('type') in {'open_page', 'find_in_page'}:
                            found = [*found, {'url': action.get('url')}]
                        sources.extend(source for source in found if isinstance(source.get('url'),str) and source['url'].startswith(('https://','http://')))
                if not sources:
                    raise APIError('模型 API 未返回真实检索来源')
        if not output_image:
            from .engine import extract_json_object
            if not isinstance(text,str):
                raise APIError('模型 API 未返回有效文本')
            result = extract_json_object(text)
    except (KeyError,StopIteration,TypeError,ValueError,OSError) as error:
        if isinstance(error,APIError):
            raise
        raise APIError('模型 API 返回的结果无效') from None
    if on_usage and isinstance(value.get('usage'),dict):
        on_usage(value['usage'])
    if on_sources and sources:
        on_sources(sources)
    if output_image:
        target = Path(output_image)
        temporary = target.with_suffix(target.suffix + '.pending')
        temporary.write_bytes(data)
        temporary.replace(target)
        return None
    if on_output:
        on_output(text)
    return result


def run_images(prompt, images=(), *, outputs, config=None, timeout=None, on_usage=None,
               on_output=None, on_phase=None):
    """使用一次 Responses 请求生成多张独立图片，并校验任务编号与实际输出的关联。"""
    outputs = validate_outputs(outputs)
    config = settings(True) if config is None else config
    status = configuration_status(config)
    if not status['configured']:
        raise APIConfigurationError(status['reason'])
    if on_phase:
        on_phase('preparing_request')
    body = image_batch_request(prompt, images, outputs, config)
    request_config = {**config, 'timeout': timeout} if timeout is not None else config
    if on_phase:
        on_phase('request_prepared')
    value = send(request_config, body, 'responses', timeout, **({'on_phase': on_phase} if on_phase else {}))
    if on_usage and isinstance(value.get('usage'), dict):
        on_usage(value['usage'])
    returned = value.get('output')
    if not isinstance(returned, list) or any(not isinstance(item, dict) for item in returned):
        raise ImageBatchIncomplete('模型 API 返回的批量输出列表无效')
    calls = [item for item in returned if item.get('type') == 'image_generation_call']
    expected = {item['id']: item for item in outputs}
    bindings = []
    text = ''.join(part['text'] for item in returned if item.get('type') == 'message'
                   and isinstance(item.get('content'), list) for part in item['content']
                   if isinstance(part, dict) and part.get('type') == 'output_text' and isinstance(part.get('text'), str))
    if text:
        from .engine import extract_json_object
        try:
            result = extract_json_object(text)
            if isinstance(result, dict) and isinstance(result.get('images'), list):
                bindings = result['images']
        except ValueError:
            pass
    # 部分兼容网关会在各工具结果中返回显式客户端任务编号。
    bindings += [{'id': item['job_id'], 'call_id': item.get('id')} for item in calls
                 if isinstance(item.get('job_id'), str) and item['job_id'] in expected]
    for item in calls:
        revised = item.get('revised_prompt')
        markers = set(re.findall(r'IMAGE_JOB_ID=([A-Za-z0-9_-]+)', revised)) if isinstance(revised, str) else set()
        if len(markers) == 1 and markers <= expected.keys():
            bindings.append({'id': next(iter(markers)), 'call_id': item.get('id')})
    by_call = {}
    for item in calls:
        if isinstance(item.get('id'), str):
            by_call.setdefault(item['id'], []).append(item)
    completed, errors, candidates = [], [], {}
    for binding in bindings:
        if not isinstance(binding, dict):
            errors.append('图片关联记录无效')
            continue
        identifier, call_id = binding.get('id'), binding.get('call_id')
        if not isinstance(identifier, str) or identifier not in expected or not isinstance(call_id, str):
            errors.append('图片关联编号无效')
            continue
        candidates.setdefault(identifier, set()).add(call_id)
    for identifier, call_ids in candidates.items():
        if len(call_ids) != 1:
            errors.append('图片关联编号重复')
            continue
        call_id = next(iter(call_ids))
        if len(by_call.get(call_id, [])) != 1 or sum(call_id in values for values in candidates.values()) != 1:
            errors.append('图片工具调用关联重复或不存在')
            continue
        try:
            item = by_call[call_id][0]
            data = base64.b64decode(item['result'], validate=True)
            save_png(data, expected[identifier])
            completed.append(identifier)
        except (KeyError, TypeError, ValueError, OSError):
            errors.append('图片 ' + identifier + ' 的生成结果或比例无效')
    if on_output and text:
        on_output(text)
    if errors or len(completed) != len(outputs) or value.get('status') != 'completed':
        raise ImageBatchIncomplete('；'.join(errors) or '模型 API 未返回全部有明确编号的图片', completed=completed)
    return completed


def image_batch_request(prompt, images, outputs, config):
    """构造带显式任务编号、独立尺寸要求和输入图片的批量图片请求。"""
    manifest = [{'id': item['id'], 'size': item['image_size']} for item in outputs]
    instructions = ('\n本请求的全部独立输出如下：' + json.dumps(manifest, ensure_ascii=False)
                    + '\n在同一次响应内生成全部独立图片，禁止拼图。每张必须遵循其指定尺寸比例。'
                      '每次生成时在该图的提示词保留 IMAGE_JOB_ID=<id>。'
                      '生成后最终文本只返回 JSON {"images":[{"id":"请求中的编号","call_id":"对应image_generation_call的id"}]}，'
                      '准确关联真实输出，不得编造工具调用编号。')
    content = [{'type': 'input_text', 'text': prompt + instructions}]
    for path in images:
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or 'image/png'
        content.append({'type': 'input_image', 'image_url': f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode()})
    body = {'model': config['model'], 'input': [{'role': 'user', 'content': content}], 'store': False,
            'tools': [{'type': 'image_generation', 'model': config['image_model'], 'size': 'auto', 'output_format': 'png'}],
            'tool_choice': {'type': 'image_generation'}}
    return body
