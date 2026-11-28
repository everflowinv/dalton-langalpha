#!/usr/bin/env python3
"""Read-only MCP stdio transport over the canonical research data dispatcher.

Uses newline-delimited JSON-RPC per MCP stdio. No package installation,
arbitrary command, URL, credential input, network listener or shell expansion.
"""
import json
import base64
import math
import os
import sys
import time
from pathlib import Path

import data_gateway

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


ROOT, HOME_ROOT = _resolve_research_roots()
STRING_FIELDS = ('operation', 'ticker', 'query', 'series', 'concept', 'item', 'document',
                 'form', 'dataset', 'start', 'end', 'as_of', 'vintage')


def tool_catalog():
    reg = data_gateway.catalog()
    tools = [{"name": "catalog", "description": "研究数据目录、来源口径、实际执行与交接路径。分类可省略。",
              "inputSchema": {"type": "object", "properties": {
                  "category": {"type": "string", "enum": list(reg['categories'])}}, "additionalProperties": False}}]
    for category, label in reg['categories'].items():
        routes = [r['id'] for r in reg['routes'] if r['category'] == category]
        properties = {"route": {"type": "string", "enum": routes},
                      "mode": {"type": "string", "enum": ["fetch", "plan"], "default": "fetch"}}
        properties.update({key: {"type": "string"} for key in STRING_FIELDS})
        properties.update({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 3},
                           "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "default": 180},
                           "params": {"type": "object", "description": "SEC：attachments 用 accession/document_type/max_chars；financials 用 statement；search-text 用 window/max_matches；read-item 用 max_chars。estimates：shares_bn/tax。AlphaEngine：meeting_scope=all|institution|personal；空 query 可 sort=newest；list_recording_tasks 用 page/states，list_filters 用 filter_type/offset；Guidepoint：query必填；size 1–30、keywords 1–10项（OR）、recency_bias 1–100整数；日期为UTC；内部库支持 media(all|text|images)、company/sector/tag、indexed_since（wiki 按入库时间筛，YYYY-MM-DD 或 UTC ISO）、sender、layer(raw|digest|all；联搜 all 只给 sales-notes 加摘要)、sources、search_mode(hybrid|keyword)、no_decay；read 用 document（含 image:<id>），文本分页用 offset/max_chars。"}})
        tools.append({"name": category, "description": label +
                      "。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。",
                      "inputSchema": {"type": "object", "properties": properties,
                                      "required": ["route"], "additionalProperties": False}})
    for tool in tools:
        tool['annotations'] = {"readOnlyHint": True, "destructiveHint": False,
                               "idempotentHint": True, "openWorldHint": tool['name'] != 'catalog'}
    return tools


def call_tool(name, args):
    definition = next((t for t in tool_catalog() if t['name'] == name), None)
    if definition is None or not isinstance(args, dict):
        raise ValueError('未知工具或参数格式错误')
    schema = definition['inputSchema']
    if set(args) - set(schema['properties']):
        raise ValueError('存在未支持的参数')
    for key in schema.get('required', []):
        if key not in args:
            raise ValueError(f'缺少 {key}')
    for key, value in args.items():
        spec = schema['properties'][key]
        expected = {'string': str, 'integer': int, 'object': dict}[spec['type']]
        if type(value) is not expected:
            raise ValueError(f'{key} 类型错误')
        if 'enum' in spec and value not in spec['enum']:
            # Accept explicitly supplied legacy routes from cached clients,
            # without publishing them in tools/list or catalog.
            compatible=key=='route' and any(r.get('hidden') and r['id']==value and r['category']==name
                for r in data_gateway.catalog(include_hidden=True)['routes'])
            if not compatible:raise ValueError(f'{key} 不在当前分类的可选值中')
        if 'minimum' in spec and not spec['minimum'] <= value <= spec['maximum']:
            raise ValueError(f'{key} 超出范围')
    if name == 'catalog':
        reg = data_gateway.catalog()
        return {"ok": True, **reg, "routes": [r for r in reg['routes']
                if not args.get('category') or r['category'] == args['category']]}
    req_timeout = float(args.get('timeout', 180))
    raw_deadline_ms = os.environ.get('DALTON_REQUEST_DEADLINE_EPOCH_MS', '').strip()
    eff_timeout_s = req_timeout
    if raw_deadline_ms:
        try:
            deadline_rem_s = max(0.1, (int(raw_deadline_ms) / 1000.0) - time.time())
            eff_timeout_s = min(req_timeout, deadline_rem_s)
        except ValueError:
            eff_timeout_s = req_timeout
    command = [sys.executable, str(ROOT/'scripts/data_gateway.py'), args.get('mode', 'fetch'), args['route']]
    for key in STRING_FIELDS + ('limit',):
        if key in args:
            # --key=value keeps a query starting with '-' a value, never an option.
            command.append('--' + key.replace('_', '-') + '=' + str(args[key]))
    if 'timeout' in args or raw_deadline_ms:
        cli_timeout = max(1, min(600, int(math.ceil(eff_timeout_s))))
        command.append('--timeout=' + str(cli_timeout))
    if 'params' in args:
        command.append('--params-json=' + json.dumps(args['params'], ensure_ascii=False))
    result = data_gateway.execute(command, eff_timeout_s)
    if 'data' in result:
        return result['data']
    return result


def serve():
    for line in sys.stdin:
        request_id = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('JSON-RPC request must be an object')
            request_id = message.get('id')
            method = message.get('method')
            if request_id is None:
                continue
            params = message.get('params') or {}
            if method == 'initialize':
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "researchdata", "version": "1.5.0"}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {"tools": tool_catalog()}
            elif method == 'tools/call':
                try:
                    payload = call_tool(params.get('name'), params.get('arguments', {}))
                except (ValueError, OSError) as exc:
                    payload = {"ok": False, "status": "error", "error": str(exc),
                               "hint": "先调用 catalog，再核对分类、路由和参数。"}
                result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                          "isError": payload.get('ok') is False}
                doc=payload.get('data',{}).get('document',{})
                if payload.get('ok') and doc.get('media_type')=='image':
                    preview=Path(doc['preview_path']).resolve()
                    if not preview.is_relative_to((HOME_ROOT/'state/research-images/assets').resolve()):
                        raise ValueError('invalid image asset path')
                    data=preview.read_bytes()
                    if len(data)>5*1024*1024:raise ValueError('image preview too large')
                    result['content'].append({'type':'image','mimeType':'image/jpeg','data':base64.b64encode(data).decode()})
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": -32601, "message": "Method not supported"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False), flush=True)
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "error": {"code": -32600, "message": "Invalid request"}}), flush=True)


if __name__ == '__main__':
    serve()
