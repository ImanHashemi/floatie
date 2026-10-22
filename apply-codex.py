#!/usr/bin/env python3
"""Apply workspace MCPs and guidance to Codex in the guest (Python 3.11+).

Managed blocks preserve unrelated settings. Conflicting unmanaged MCP names fail
before writing. HTTP header credentials resolve at connection time. Generated
configuration is mode 0600; STDIO environment values expand in the guest.
"""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import tomllib

START = '# floatie:codex:start'
END = '# floatie:codex:end'


def unmanaged(text):
    if START not in text and END not in text:
        return text
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError('invalid Floatie managed block')
    before, rest = text.split(START)
    _, after = rest.split(END)
    return before + after.lstrip('\n')


def expand(value):
    def replace(match):
        name = match[1]
        if not os.environ.get(name):
            raise ValueError(f'missing environment variable: {name}')
        return os.environ[name]
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', replace, value)


def literal(value):
    # JSON strings, arrays and inline objects are rendered as TOML explicitly.
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return '[' + ', '.join(literal(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(literal(k) + ' = ' + literal(v) for k, v in value.items()) + '}'
    raise ValueError('expected a string, list or mapping')


def render(source, original):
    base = unmanaged(original)
    parsed = tomllib.loads(base)
    defaults = ''
    if 'project_doc_fallback_filenames' not in parsed:
        defaults = 'project_doc_fallback_filenames = ["CLAUDE.md"]\n'
    servers = source.get('mcpServers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcpServers must be an object')
    blocks = []
    for name, server in servers.items():
        if name in parsed.get('mcp_servers', {}):
            raise ValueError(f'unmanaged Codex MCP already exists: {name}')
        transport = server.get('type', 'stdio')
        cfg = {}
        if transport == 'http':
            cfg['url'] = expand(server['url'])
            headers = server.get('headers', {})
            bearer = re.fullmatch(r'Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}', headers.get('Authorization', ''))
            if bearer:
                cfg['bearer_token_env_var'] = bearer[1]
                headers = {k: v for k, v in headers.items() if k != 'Authorization'}
            if headers:
                env_headers = {}
                static_headers = {}
                dynamic_headers = {}
                for key, value in headers.items():
                    match = re.fullmatch(r'(?:(.*) )?\$\{([A-Za-z_][A-Za-z0-9_]*)\}', value)
                    if match:
                        prefix, variable = match.groups()
                        if prefix:
                            dynamic_headers[key] = value
                        else:
                            env_headers[key] = variable
                    elif re.search(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', value):
                        dynamic_headers[key] = value
                    else:
                        static_headers[key] = value
                if dynamic_headers:
                    # Resolve prefixed headers (e.g. Basic ${TOKEN}) only when
                    # Codex connects. Never persist the expanded secret in TOML.
                    helper = (
                        'import json,os,re; '
                        f'headers={dynamic_headers!r}; '
                        'print(json.dumps({k:re.sub(r"\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}",'
                        'lambda m:os.environ[m[1]],v) for k,v in headers.items()}))'
                    )
                    cfg['http_headers_helper'] = 'python3 -c ' + shlex.quote(helper)
                if env_headers:
                    cfg['env_http_headers'] = env_headers
                if static_headers:
                    cfg['http_headers'] = static_headers
        elif transport == 'stdio':
            cfg['command'] = expand(server['command'])
            cfg['args'] = [expand(v) for v in server.get('args', [])]
            if server.get('env'):
                cfg['env'] = {k: expand(v) for k, v in server['env'].items()}
        else:
            raise ValueError(f'{name}: Codex needs stdio or streamable HTTP; migrate the {transport} endpoint')
        blocks.append('[mcp_servers.' + literal(name) + ']\n' + ''.join(k + ' = ' + literal(v) + '\n' for k, v in cfg.items()))
    # Root settings precede existing tables; managed server tables go at the end.
    result = defaults + base.rstrip() + '\n\n' + START + '\n' + '\n'.join(blocks) + END + '\n'
    tomllib.loads(result)
    return result


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    workspace = Path(sys.argv[1])
    target = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    config = target / 'config.toml'
    source = json.loads((workspace / '.mcp.json').read_text()) if (workspace / '.mcp.json').exists() else {}
    result = render(source, config.read_text() if config.exists() else '')
    guidance = workspace / 'AGENTS.md'
    if not guidance.exists():
        guidance = workspace / 'CLAUDE.md'
    agents = target / 'AGENTS.md'
    old = unmanaged(agents.read_text() if agents.exists() else '')
    instructions = old.rstrip() + '\n\n' + START + '\n'
    instructions += guidance.read_text() if guidance.exists() else ''
    instructions += '\n' + END + '\n'
    # Validate everything before replacing either file. Never touch auth.json.
    write_private(config, result)
    write_private(agents, instructions)
    print(f'==> Codex configured: {len(source.get("mcpServers", {}))} MCP servers; workspace guidance at user scope')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        # Do not print TOML parse errors: they may quote a credential-bearing line.
        if isinstance(error, tomllib.TOMLDecodeError):
            sys.exit('error: invalid Codex TOML; existing configuration preserved')
        sys.exit(f'error: {error}')
