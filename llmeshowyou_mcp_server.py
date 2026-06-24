#!/usr/bin/env python3
"""
llmeshowyou_mcp_server — Ephemeral map tool for MCP-compatible LLMs.
Generates fresh file maps on-demand so the LLM always sees current source
structure with zero staleness/drift. Designed for opencode, Claude Code,
and any MCP-compatible agent.

Usage (stdio, default):
  python llmeshowyou_mcp_server.py

Usage (HTTP):
  python llmeshowyou_mcp_server.py --transport http --port 8080

Configure in opencode.json:
  {
    "mcpServers": {
      "llmeshowyou": {
        "command": "python",
        "args": ["path/to/llmeshowyou_mcp_server.py"]
      }
    }
  }
"""

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from llmeshowyou import analyze_file, render_map, read_source, map_path_for, find_source_files, is_stale
    ENGINE_OK = True
except ImportError as e:
    ENGINE_OK = False
    ENGINE_ERR = str(e)


def handle_request(method: str, params: dict) -> dict:
    if method == 'tools/list':
        return {
            'tools': [
                {
                    'name': 'map_file',
                    'description': 'Generate a fresh structural map of a source file. Returns class/function signatures, imports, constants, section markers, and TODOs. Always call this instead of reading stale .map.md files.',
                    'inputSchema': {
                        'type': 'object',
                        'properties': {
                            'path': {
                                'type': 'string',
                                'description': 'Path to the source file'
                            }
                        },
                        'required': ['path']
                    }
                },
                {
                    'name': 'read_source',
                    'description': 'Read specific lines from a source file. Use this after map_file to read the exact section you need by line range.',
                    'inputSchema': {
                        'type': 'object',
                        'properties': {
                            'path': {
                                'type': 'string',
                                'description': 'Path to the source file'
                            },
                            'start_line': {
                                'type': 'integer',
                                'description': 'First line number (1-indexed)'
                            },
                            'end_line': {
                                'type': 'integer',
                                'description': 'Last line number (inclusive)'
                            }
                        },
                        'required': ['path', 'start_line', 'end_line']
                    }
                },
                {
                    'name': 'map_folder',
                    'description': 'Map all supported source files in a folder. Returns an index of file->map relationships.',
                    'inputSchema': {
                        'type': 'object',
                        'properties': {
                            'path': {
                                'type': 'string',
                                'description': 'Path to the folder'
                            },
                            'recursive': {
                                'type': 'boolean',
                                'description': 'Search subdirectories',
                                'default': True
                            }
                        },
                        'required': ['path']
                    }
                },
                {
                    'name': 'check_stale',
                    'description': 'Check if a .map.md file is stale using SHA-256 hashing. Returns true if the source has changed since the map was generated. Use this first: if a map exists and is not stale, read it directly from disk (cheap). Only call map_file if the map is stale or missing.',
                    'inputSchema': {
                        'type': 'object',
                        'properties': {
                            'source': {
                                'type': 'string',
                                'description': 'Path to the source file (e.g. foo.py)'
                            }
                        },
                        'required': ['source']
                    }
                },
            ]
        }

    if method == 'tools/call':
        name = params.get('name', '')
        args = params.get('arguments', {})

        if not ENGINE_OK:
            return {
                'isError': True,
                'content': [{
                    'type': 'text',
                    'text': f'llmeshowyou engine not available: {ENGINE_ERR}'
                }]
            }

        if name == 'map_file':
            path = args.get('path', '')
            return _do_map_file(path)

        if name == 'read_source':
            path = args.get('path', '')
            start = args.get('start_line', 1)
            end = args.get('end_line', start)
            return _do_read_source(path, start, end)

        if name == 'map_folder':
            path = args.get('path', '')
            recursive = args.get('recursive', True)
            return _do_map_folder(path, recursive)

        if name == 'check_stale':
            source = args.get('source', '')
            return _do_check_stale(source)

        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Unknown tool: {name}'}]
        }

    return {
        'isError': True,
        'content': [{'type': 'text', 'text': f'Unknown method: {method}'}]
    }


def _do_map_file(path: str) -> dict:
    src = Path(path).resolve()
    if not src.exists():
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'File not found: {src}'}]
        }
    if not src.is_file():
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Not a file: {src}'}]
        }
    try:
        text, lines = read_source(src)
        fm = analyze_file(src, text, lines)
        map_text = render_map(fm)
        return {
            'content': [{'type': 'text', 'text': f'# Map: {src.name}\n\n{map_text}'}]
        }
    except Exception as e:
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Error mapping {src}: {e}\n{traceback.format_exc()}'}]
        }


def _do_read_source(path: str, start: int, end: int) -> dict:
    src = Path(path).resolve()
    if not src.exists():
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'File not found: {src}'}]
        }
    try:
        _, lines = read_source(src)
        total = len(lines)
        s = max(1, start)
        e = min(total, end)
        if s > total:
            return {
                'isError': True,
                'content': [{'type': 'text', 'text': f'File has {total} lines, requested starting at {s}'}]
            }
        excerpt = lines[s - 1:e]
        result = f'# {src.name} lines {s}-{e} of {total}\n\n'
        for i, line in enumerate(excerpt, s):
            result += f'{i}:{line}'
        if not result.endswith('\n'):
            result += '\n'
        return {
            'content': [{'type': 'text', 'text': result}]
        }
    except Exception as e:
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Error reading {src}: {e}'}]
        }


def _do_map_folder(path: str, recursive: bool) -> dict:
    folder = Path(path).resolve()
    if not folder.is_dir():
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Folder not found: {folder}'}]
        }
    try:
        from llmeshowyou import SUPPORTED_EXTENSIONS
        files = find_source_files([str(folder)], recursive=recursive)
        results = []
        for fp in files:
            try:
                text, lines = read_source(fp)
                fm = analyze_file(fp, text, lines)
                rel = fp.relative_to(folder)
                results.append({
                    'file': str(rel.as_posix()),
                    'total_lines': fm.total_lines,
                    'symbols': fm.total_symbols,
                    'classes': len(fm.classes),
                    'functions': len(fm.functions),
                    'imports': len(fm.imports),
                })
            except Exception:
                results.append({
                    'file': fp.name,
                    'error': 'parse failed'
                })
        return {
            'content': [{
                'type': 'text',
                'text': f'# Folder: {folder.name} ({len(results)} files)\n\n' +
                        json.dumps(results, indent=2)
            }]
        }
    except Exception as e:
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Error mapping folder: {e}'}]
        }


def _do_check_stale(source: str) -> dict:
    src = Path(source).resolve()
    if not src.exists():
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Source file not found: {src}'}]
        }
    map_path = src.parent / (src.name + '.map.md')
    if not map_path.exists():
        return {
            'content': [{'type': 'text', 'text': json.dumps({
                'stale': True,
                'reason': 'map_not_found',
                'map_path': str(map_path)
            })}]
        }
    try:
        stale = is_stale(src, map_path)
        return {
            'content': [{'type': 'text', 'text': json.dumps({
                'stale': stale,
                'reason': 'sha_mismatch' if stale else 'fresh',
                'source': src.name,
                'map_path': map_path.name
            })}]
        }
    except Exception as e:
        return {
            'isError': True,
            'content': [{'type': 'text', 'text': f'Error checking staleness: {e}'}]
        }


def serve_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get('id', 0)
        method = req.get('method', '')
        params = req.get('params', {})
        try:
            result = handle_request(method, params)
            resp = {'jsonrpc': '2.0', 'id': rid, 'result': result}
        except Exception as e:
            resp = {
                'jsonrpc': '2.0', 'id': rid,
                'error': {'code': -32603, 'message': str(e)}
            }
        sys.stdout.write(json.dumps(resp) + '\n')
        sys.stdout.flush()


def serve_http(port: int):
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class MCPHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            try:
                req = json.loads(body)
                rid = req.get('id', 0)
                method = req.get('method', '')
                params = req.get('params', {})
                result = handle_request(method, params)
                resp = {'jsonrpc': '2.0', 'id': rid, 'result': result}
            except Exception as e:
                resp = {
                    'jsonrpc': '2.0', 'id': 0,
                    'error': {'code': -32603, 'message': str(e)}
                }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode('utf-8'))

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(('127.0.0.1', port), MCPHandler)
    print(f'llmeshowyou MCP server -> http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='llmeshowyou MCP tool server')
    parser.add_argument('--transport', choices=['stdio', 'http'], default='stdio')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    if args.transport == 'http':
        serve_http(args.port)
    else:
        serve_stdio()
