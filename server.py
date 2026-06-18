import asyncio
import gzip
import hashlib
import hmac
import mimetypes
import os
import pty
import shutil
import struct
import tempfile
import termios
import fcntl
import signal
import pwd
from pathlib import Path
from aiohttp import web


AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', 'admin')
SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key').encode()
PROTECTED_ROUTES = {'/', '/ws', '/api/rootpath', '/api/files', '/api/files/read', '/api/files/write', '/api/files/mkdir', '/api/files/delete', '/api/files/rename'}

COOKIE_NAME = 'session'

HERE = Path(__file__).parent

_SESSION_COOKIE = None
_EXPECTED_SIG = None

DEFAULT_SHELL = os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'


_ALLOWED_SHELLS = frozenset({'/bin/bash', '/bin/sh', '/bin/zsh', '/bin/fish', '/usr/bin/bash', '/usr/bin/sh', '/usr/bin/zsh', '/usr/bin/fish'})


def _resolve_shell(request):
    shell = request.query.get('shell', DEFAULT_SHELL)
    if shell not in _ALLOWED_SHELLS:
        shell = DEFAULT_SHELL
    return shell, os.path.basename(shell)


async def shell_handler(request):
    ws = web.WebSocketResponse(max_msg_size=1024 * 1024)
    await ws.prepare(request)

    shell_path, shell_name = _resolve_shell(request)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(shell_path, [shell_name])
        os._exit(1)

    _set_size(fd, 24, 80)
    loop = asyncio.get_running_loop()

    async def reader():
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except (ConnectionResetError, ConnectionError, OSError):
            pass

    async def writer():
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    text = msg.data
                    if text.startswith('RESIZE:'):
                        try:
                            _, rows, cols = text.split(':')
                            _set_size(fd, int(rows), int(cols))
                        except (ValueError, IndexError, TypeError):
                            pass
                    else:
                        await loop.run_in_executor(None, os.write, fd, text.encode('utf-8'))
                elif msg.type == web.WSMsgType.BINARY:
                    await loop.run_in_executor(None, os.write, fd, msg.data)
                elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
        except (ConnectionResetError, ConnectionError, OSError):
            pass

    reader_task = asyncio.ensure_future(reader())
    writer_task = asyncio.ensure_future(writer())

    done, pending = await asyncio.wait(
        [reader_task, writer_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass

    return ws


def _set_size(fd, rows, cols):
    rows = max(1, min(rows, 1000))
    cols = max(1, min(cols, 1000))
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def _make_session_cookie():
    global _SESSION_COOKIE
    if _SESSION_COOKIE is None:
        val = 'authenticated'
        sig = hmac.new(SECRET_KEY, val.encode(), 'sha256').hexdigest()
        _SESSION_COOKIE = f'{val}.{sig}'
    return _SESSION_COOKIE


def _check_session(request):
    global _EXPECTED_SIG
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False
    try:
        val, sig = cookie.split('.', 1)
        if _EXPECTED_SIG is None:
            _EXPECTED_SIG = hmac.new(SECRET_KEY, b'authenticated', 'sha256').hexdigest()
        sig_ok = hmac.compare_digest(sig, _EXPECTED_SIG)
        val_ok = hmac.compare_digest(val, 'authenticated')
        return sig_ok and val_ok
    except (ValueError, AttributeError):
        return False


async def login_handler(request):
    if request.method == 'GET':
        return web.FileResponse(HERE / 'static' / 'login.html')
    data = await request.post()
    password = data.get('password', '')
    if hmac.compare_digest(password, AUTH_PASSWORD):
        resp = web.HTTPFound('/')
        resp.set_cookie(COOKIE_NAME, _make_session_cookie(), httponly=True, samesite='Lax', max_age=86400)
        raise resp
    raise web.HTTPFound('/login')


async def logout_handler(request):
    resp = web.HTTPFound('/login')
    resp.del_cookie(COOKIE_NAME)
    raise resp


@web.middleware
async def auth_middleware(request, handler):
    if request.path in PROTECTED_ROUTES and not _check_session(request):
        raise web.HTTPFound('/login')
    return await handler(request)


_gzip_cache = {}
_GZIP_CACHE_MAX = 64

@web.middleware
async def gzip_middleware(request, handler):
    resp = await handler(request)
    if isinstance(resp, web.Response) and resp.body and 'gzip' in request.headers.get('Accept-Encoding', ''):
        body = resp.body
        body_len = len(body)
        if body_len > 512:
            cache_key = hashlib.md5(body, usedforsecurity=False).digest()
            cached = _gzip_cache.get(cache_key)
            if cached is None:
                compressed = gzip.compress(body, compresslevel=5)
                if len(_gzip_cache) >= _GZIP_CACHE_MAX:
                    _gzip_cache.clear()
                _gzip_cache[cache_key] = compressed
            else:
                compressed = cached
            resp.body = compressed
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Content-Length'] = str(len(compressed))
    return resp


def _file_response_with_cache(path, cache_max_age=3600):
    resp = web.FileResponse(path)
    resp.headers['Cache-Control'] = f'public, max-age={cache_max_age}'
    return resp


def index_handler(request):
    return _file_response_with_cache(HERE / 'static' / 'index.html', cache_max_age=0)


def style_handler(request):
    return _file_response_with_cache(HERE / 'static' / 'style.css')


def explorer_js_handler(request):
    return _file_response_with_cache(HERE / 'static' / 'file_explorer.js')


def _safe_path(path_str):
    p = Path(path_str).resolve()
    if not (p == HERE or str(p).startswith(str(HERE) + '/')):
        raise ValueError('Path outside root')
    return p


async def list_files(request):
    path_str = request.query.get('path', str(HERE))
    try:
        p = _safe_path(path_str)
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        dirs = []
        files = []
        with os.scandir(p) as it:
            for entry in it:
                try:
                    name = entry.name
                    if entry.is_dir():
                        dirs.append({'name': name, 'path': entry.path, 'is_dir': True, 'size': 0, 'mtime': 0})
                    else:
                        st = entry.stat(follow_symlinks=False)
                        files.append({'name': name, 'path': entry.path, 'is_dir': False, 'size': st.st_size, 'mtime': st.st_mtime})
                except OSError:
                    continue
        dirs.sort(key=lambda e: e['name'].lower())
        files.sort(key=lambda e: e['name'].lower())
        return web.json_response({
            'path': str(p),
            'parent': str(p.parent) if p.parent != p else None,
            'entries': dirs + files,
        })
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def read_file(request):
    path_str = request.query.get('path', '')
    try:
        p = _safe_path(path_str)
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        if not p.is_file():
            return web.json_response({'error': 'Not a file'}, status=400)
        st = p.stat()
        if st.st_size > 5 * 1024 * 1024:
            return web.json_response({'error': 'File too large (>5MB)'}, status=413)
        content = p.read_bytes()
        content_type, _ = mimetypes.guess_type(str(p))
        if content_type is None:
            content_type = 'application/octet-stream'
        return web.Response(body=content, content_type=content_type)
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def write_file(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    path_str = data.get('path', '')
    content = data.get('content', '')
    try:
        p = _safe_path(path_str)
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=p.parent, suffix='.tmp')
        try:
            if isinstance(content, str):
                os.write(fd, content.encode('utf-8'))
            else:
                os.write(fd, content)
            os.close(fd)
            os.replace(tmp_path, p)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return web.json_response({'ok': True, 'path': str(p.resolve())})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def make_directory(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    path_str = data.get('path', '')
    try:
        p = _safe_path(path_str)
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return web.json_response({'ok': True, 'path': str(p.resolve())})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def delete_path(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    path_str = data.get('path', '')
    try:
        p = _safe_path(path_str)
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        if p.is_dir():
            shutil.rmtree(p, onerror=lambda func, path, exc: None)
        else:
            p.unlink()
        return web.json_response({'ok': True})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def rename_path(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    try:
        old_path = _safe_path(data.get('path', ''))
        new_path = _safe_path(data.get('newPath', ''))
    except ValueError:
        return web.json_response({'error': 'Invalid path'}, status=400)
    try:
        old_path.rename(new_path)
        return web.json_response({'ok': True, 'path': str(new_path.resolve())})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def root_path_handler(request):
    return web.json_response({'rootPath': str(HERE)})


app = web.Application(middlewares=[auth_middleware, gzip_middleware])
app.router.add_get('/ws', shell_handler)
app.router.add_get('/', index_handler)
app.router.add_get('/style.css', style_handler)
app.router.add_get('/file_explorer.js', explorer_js_handler)
app.router.add_route('GET', '/login', login_handler)
app.router.add_route('POST', '/login', login_handler)
app.router.add_get('/logout', logout_handler)

# File explorer API
app.router.add_get('/api/rootpath', root_path_handler)
app.router.add_get('/api/files', list_files)
app.router.add_get('/api/files/read', read_file)
app.router.add_post('/api/files/write', write_file)
app.router.add_post('/api/files/mkdir', make_directory)
app.router.add_post('/api/files/delete', delete_path)
app.router.add_post('/api/files/rename', rename_path)


if __name__ == '__main__':
    print('Web Terminal running at http://localhost:8082')
    web.run_app(app, port=8082, print=None)
