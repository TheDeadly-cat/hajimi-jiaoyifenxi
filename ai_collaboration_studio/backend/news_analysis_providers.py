"""Fixed, single-request news analysis adapters. No config/environment/key loading."""
from __future__ import annotations

import copy
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .execution_boundary import build_text_provider_request

DOUBAO_MODEL = "doubao-seed-2-1-pro-260915"
MAX_OUTPUT_TOKENS = 900
REQUEST_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 16000
CNY_PER_USD = 10
RATE_VERSION = "2026-10-25_peak_budget_v1"
BUDGET_ESTIMATE_NOTICE = (
    "按请求UTF-8字节数加256估计输入上界，另预留900个输出token；"
    "DeepSeek按美元峰值费率及10元/美元预算参数折算。"
    "该参数不是实时汇率，预约不是供应商实付账单，也不是供应商账单硬上限。"
)
AVAILABLE_MODELS = (
    {"provider":"doubao","model":DOUBAO_MODEL,"label":"豆包 Seed 2.1 Pro"},
    {"provider":"deepseek","model":"deepseek-flash","label":"DeepSeek Flash"},
    {"provider":"deepseek","model":"deepseek-v4-pro","label":"DeepSeek V4 Pro"},
)
ENDPOINTS = {"doubao":"https://ark.cn-beijing.volces.com/api/v3/responses",
             "deepseek":"https://api.deepseek.com/responses"}
# Decimal strings are peak USD/million-token rates, not observed bills.
DEEPSEEK_RATES_USD = {"deepseek-flash":("0.3","1.2"), "deepseek-v4-pro":("1.32","3.96")}


def select_route(provider="doubao", model=None):
    if not isinstance(provider, str) or provider not in ENDPOINTS:
        raise ValueError("invalid_analysis_provider")
    selected = model if model is not None else DOUBAO_MODEL if provider == "doubao" else "deepseek-flash"
    if not isinstance(selected, str) or not any(option['provider']==provider and option['model']==selected for option in AVAILABLE_MODELS):
        raise ValueError("invalid_analysis_model")
    return provider, selected


def build_body(provider, model, text, instructions, text_format):
    provider, model = select_route(provider, model)
    body = {"model":model,"instructions":instructions,"input":text,"max_output_tokens":MAX_OUTPUT_TOKENS}
    if provider == 'doubao':
        body['thinking'] = {"type":"disabled"}
    else:
        # Responses defaults to thinking enabled; explicitly disable it so the
        # fixed output reservation never silently turns into a thinking budget.
        body['reasoning'] = {"effort":"none"}
    body['store'] = False
    body['text'] = {"format":copy.deepcopy(text_format)}
    return body


def encode_body(body):
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')


def reservation_for(provider, model, body):
    provider, model = select_route(provider, model)
    size = len(encode_body(body))
    inputs = size + 256
    if provider == 'doubao':
        # Frozen existing per-token reservation rates in micro-CNY.
        input_micro, output_micro = Decimal(6), Decimal(30)
        rates = {"input_cny_per_million":"6","output_cny_per_million":"30"}
    else:
        input_usd, output_usd = DEEPSEEK_RATES_USD[model]
        input_micro, output_micro = Decimal(input_usd) * CNY_PER_USD, Decimal(output_usd) * CNY_PER_USD
        rates = {"input_usd_per_million":input_usd,"output_usd_per_million":output_usd,
                 "cny_per_usd_parameter":CNY_PER_USD}
    reserved = int((inputs * input_micro + MAX_OUTPUT_TOKENS * output_micro).to_integral_value(rounding=ROUND_CEILING))
    return reserved, {"rate_version":RATE_VERSION,"request_utf8_bytes":size,
        "input_token_upper_estimate":inputs,"output_tokens_reserved":MAX_OUTPUT_TOKENS,
        "rounding":"ceil_micro_cny","reserved_micro_cny":reserved,**rates,
        "supplier_bill_verified":False,"notice":BUDGET_ESTIMATE_NOTICE}


@dataclass(frozen=True)
class NewsProviderResponse:
    ok: bool
    provider: str
    model: str
    content: str = ""
    error_code: str = ""
    requested_model: str = ""
    reported_model: str | None = None


class NoNewsProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NewsModelMismatch(ValueError):
    """Returned identity cannot be bound to the requested model family."""


def safe_reported_model(value, provider=None, requested_model=None):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        return None
    families = {
        ('doubao',DOUBAO_MODEL):r'doubao-seed-2-1-pro-[0-9]{6,8}',
        ('deepseek','deepseek-flash'):r'deepseek-(?:flash|v4(?:\.[0-9]+)?-flash)(?:-[0-9]{4,8})?',
        ('deepseek','deepseek-v4-pro'):r'deepseek-v4(?:\.[0-9]+)?-pro(?:-[0-9]{4,8})?',
    }
    family = families.get((provider,requested_model))
    return value if family and re.fullmatch(family,value.casefold()) else None


def _response_content(payload, provider, requested_model):
    if (not isinstance(payload, dict) or payload.get('status') != 'completed'
            or payload.get('error') is not None or payload.get('incomplete_details') not in (None, {})
            or payload.get('refusal') is not None):
        raise ValueError('invalid_provider_completion')
    reported = safe_reported_model(payload.get('model'),provider,requested_model)
    if reported is None:
        raise NewsModelMismatch('model_mismatch')
    output = payload.get('output')
    if not isinstance(output, list) or not output:
        raise ValueError('missing_provider_output')
    chunks = []
    for message in output:
        if (not isinstance(message, dict) or message.get('type') != 'message'
                or message.get('role') != 'assistant' or message.get('status', 'completed') != 'completed'):
            raise ValueError('unexpected_provider_output')
        content = message.get('content')
        if not isinstance(content, list):
            raise ValueError('invalid_provider_content')
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                raise ValueError('provider_refusal_or_invalid_content')
            chunks.append(part['text'])
    text = ''.join(chunks)
    if not text.strip():
        raise ValueError('empty_provider_output')
    return text, reported


def generate_response(provider, model, api_key, body):
    """Exactly one POST, no redirects/retries/tools, and bounded response bytes."""
    provider, model = select_route(provider, model)
    endpoint = ENDPOINTS[provider]
    allowed = {'model','instructions','input','max_output_tokens','store','text',
               'thinking' if provider == 'doubao' else 'reasoning'}
    expected_mode = {'type':'disabled'} if provider == 'doubao' else {'effort':'none'}
    if (set(body) != allowed or body.get('model') != model or body.get('max_output_tokens') != MAX_OUTPUT_TOKENS
            or not isinstance(body.get('input'),str) or not isinstance(body.get('instructions'),str)
            or body.get('store') is not False
            or body.get('thinking' if provider == 'doubao' else 'reasoning') != expected_mode
            or not isinstance(body.get('text'), dict)
            or not isinstance(body['text'].get('format'), dict)
            or body['text']['format'].get('type') != 'json_schema'
            or body['text']['format'].get('strict') is not True):
        raise ValueError('invalid_news_provider_request')
    if not isinstance(api_key, str) or not api_key or any(not 32 < ord(char) < 127 for char in api_key):
        raise ValueError('invalid_analysis_configuration')
    encoded = encode_body(body)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError('news_provider_request_too_large')
    request = build_text_provider_request(endpoint.removesuffix('/responses'), 'responses', body, headers={
        'Authorization':'Bearer ' + api_key,'Content-Type':'application/json','User-Agent':'AICollaborationStudio/0.2'})
    # Reservation, bound comparison plans and transport must use the same bytes.
    if request.full_url != endpoint or request.data != encoded:
        raise ValueError('news_provider_request_encoding_mismatch')
    # Preserve the standard proxy behavior used by the existing Doubao adapter.
    # Configuration modules and implicit credential discovery are never imported.
    opener = urllib.request.build_opener(NoNewsProviderRedirect())
    reported = None
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.geturl() != endpoint or response.status != 200:
                raise ValueError('unexpected_provider_endpoint_or_status')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('provider_response_too_large')
            payload = json.loads(raw.decode('utf-8'))
            reported = safe_reported_model(payload.get('model'),provider,model) if isinstance(payload, dict) else None
            content, reported = _response_content(payload, provider,model)
        return NewsProviderResponse(True, provider, reported or model, content, requested_model=model, reported_model=reported)
    except NewsModelMismatch:
        return NewsProviderResponse(False, provider, model, error_code='model_mismatch', requested_model=model)
    except urllib.error.HTTPError as exc:
        exc.close()  # Never read or expose a provider error body or key.
        return NewsProviderResponse(False, provider, model, error_code='http_error', requested_model=model)
    except Exception:
        return NewsProviderResponse(False, provider, model, error_code='invalid_or_unconfirmed_response', requested_model=model, reported_model=reported)
