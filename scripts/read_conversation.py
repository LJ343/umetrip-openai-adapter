#!/usr/bin/env python3
"""Read private traces locally on the server; this exposes no HTTP log endpoint."""
import argparse
import json
from pathlib import Path
import re

parser = argparse.ArgumentParser(description='查看中转完整问答记录（需服务用户或 sudo 权限）')
parser.add_argument('--directory', default='/var/log/openai-proxy/conversations')
parser.add_argument('--request-id', help='32 位十六进制请求 ID')
parser.add_argument('--list', action='store_true', help='列出最近 20 个请求')
parser.add_argument('--raw', action='store_true', help='输出完整 JSONL，包括逐条时间戳')
args = parser.parse_args()
directory = Path(args.directory)
if args.request_id and not re.fullmatch('[a-f0-9]{32}', args.request_id):
    parser.error('request-id 必须为 32 位十六进制字符串')
files = sorted(directory.glob('*' + (args.request_id or '') + '.jsonl'))
if not files:
    parser.exit(1, '没有找到会话记录\n')
if args.list:
    for path in files[-20:]:
        print(path.name)
    raise SystemExit(0)
path = files[-1]
if args.raw:
    print(path.read_text(), end='')
    raise SystemExit(0)
rows = [json.loads(line) for line in path.read_text().splitlines()]
print('文件：', path.name)
for stage, title in [('platform_request', '平台传入请求'), ('upstream_request', '实际发给上游的请求')]:
    for row in rows:
        if row['stage'] == stage:
            print('\n' + title + '：')
            print(json.dumps(row, ensure_ascii=False, indent=2))
print('\n上游原始 SSE（换行规范化，包含过滤前的卡片与事件）：')
for row in rows:
    if row['stage'] == 'upstream_sse_line':
        print(row['text'])
    elif row['stage'] in {'upstream_error_body', 'upstream_connection_error'}:
        print(json.dumps(row, ensure_ascii=False))
print('\n中转实际发给平台的 HTTP 正文（含等待提示、保活与错误）：')
for row in rows:
    if row['stage'] == 'platform_response_body':
        print(row['text'], end='')
print('\n\n记录状态：')
for row in rows:
    if row['stage'] in {'capture_truncated', 'platform_request_omitted', 'client_disconnect', 'request_exception', 'request_end'}:
        print(json.dumps(row, ensure_ascii=False))
