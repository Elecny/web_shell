import asyncio
import hmac
import os
import pty
import shutil
import struct
import termios
import fcntl
import signal
import pwd
from pathlib import Path
from aiohttp import web


AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', 'admin')
SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key').encode()
PROTECTED_ROUTES = {'/', '/ws', '/api/files', '/api/files/read', '/api/files/write', '/api/files/mkdir', '/api/files/delete', '/api/files/rename'}

COOKIE_NAME = 'session'

HERE = Path(__file__).parent

DEFAULT_SHELL = os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'


def _resolve_shell(request):
    shell = request.query.get('shell', DEFAULT_SHELL)
    return shell, os.path.basename(shell)


async def shell_handler(request):
    ws = web.WebSocketResponse(max_msg_size=65536)
    await ws.prepare(request)

    shell_path, shell_name = _resolve_shell(request)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(shell_path, [shell_name])
        os._exit(1)

    _set_size(fd, 24, 80)
    loop = asyncio.get_running_loop()

    async def reader():
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
            except (ConnectionResetError, ConnectionError, OSError):
                break

    async def writer():
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                text = msg.data
                if text.startswith('RESIZE:'):
                    try:
                        _, rows, cols = text.split(':')
                        _set_size(fd, int(rows), int(cols))
                    except (ValueError, TypeError):
                        pass
                else:
                    await loop.run_in_executor(None, os.write, fd, text.encode('utf-8'))
            elif msg.type == web.WSMsgType.BINARY:
                await loop.run_in_executor(None, os.write, fd, msg.data)
            elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break

    await asyncio.gather(reader(), writer())

    try:
        os.close(fd)
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
    except OSError:
        pass

    return ws


def _set_size(fd, rows, cols):
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def _make_session_cookie():
    val = 'authenticated'
    sig = hmac.new(SECRET_KEY, val.encode(), 'sha256').hexdigest()
    return f'{val}.{sig}'


def _check_session(request):
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False
    try:
        val, sig = cookie.split('.', 1)
        expected = hmac.new(SECRET_KEY, val.encode(), 'sha256').hexdigest()
        return hmac.compare_digest(sig, expected) and val == 'authenticated'
    except (ValueError, AttributeError):
        return False


async def login_handler(request):
    if request.method == 'GET':
        return web.FileResponse(HERE / 'static' / 'login.html')
    data = await request.post()
    password = data.get('password', '')
    if password == AUTH_PASSWORD:
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


def index_handler(request):
    return web.FileResponse(HERE / 'static' / 'index.html')


def style_handler(request):
    return web.FileResponse(HERE / 'static' / 'style.css')


def explorer_js_handler(request):
    return web.FileResponse(HERE / 'static' / 'file_explorer.js')


def _safe_path(path_str):
    p = Path(path_str).resolve()
    return p


async def list_files(request):
    path_str = request.query.get('path', str(HERE))
    p = _safe_path(path_str)
    try:
        entries = []
        with os.scandir(p) as it:
            dirs = []
            files = []
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    name = entry.name
                    if is_dir:
                        dirs.append((name, entry.path))
                    else:
                        st = entry.stat()
                        files.append((name, entry.path, st.st_size, st.st_mtime))
                except OSError:
                    continue
            dirs.sort(key=lambda e: e[0].lower())
            files.sort(key=lambda e: e[0].lower())
            for name, path in dirs:
                entries.append({
                    'name': name,
                    'path': path,
                    'is_dir': True,
                    'size': 0,
                    'mtime': 0,
                })
            for name, path, size, mtime in files:
                entries.append({
                    'name': name,
                    'path': path,
                    'is_dir': False,
                    'size': size,
                    'mtime': mtime,
                })
        return web.json_response({
            'path': str(p),
            'parent': str(p.parent) if p.parent != p else None,
            'entries': entries,
        })
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def read_file(request):
    path_str = request.query.get('path', '')
    p = _safe_path(path_str)
    try:
        if not p.is_file():
            return web.json_response({'error': 'Not a file'}, status=400)
        content = p.read_bytes()
        return web.Response(body=content, content_type='application/octet-stream')
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def write_file(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    path_str = data.get('path', '')
    content = data.get('content', '')
    p = _safe_path(path_str)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
        return web.json_response({'ok': True, 'path': str(p.resolve())})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


async def make_directory(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'Invalid JSON'}, status=400)
    path_str = data.get('path', '')
    p = _safe_path(path_str)
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
    p = _safe_path(path_str)
    try:
        if p.is_dir():
            shutil.rmtree(p)
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
    old_path = _safe_path(data.get('path', ''))
    new_path = _safe_path(data.get('newPath', ''))
    try:
        old_path.rename(new_path)
        return web.json_response({'ok': True, 'path': str(new_path.resolve())})
    except OSError as e:
        return web.json_response({'error': str(e)}, status=500)


app = web.Application(middlewares=[auth_middleware])
app.router.add_get('/ws', shell_handler)
app.router.add_get('/', index_handler)
app.router.add_get('/style.css', style_handler)
app.router.add_get('/file_explorer.js', explorer_js_handler)
app.router.add_route('GET', '/login', login_handler)
app.router.add_route('POST', '/login', login_handler)
app.router.add_get('/logout', logout_handler)

# File explorer API
app.router.add_get('/api/files', list_files)
app.router.add_get('/api/files/read', read_file)
app.router.add_post('/api/files/write', write_file)
app.router.add_post('/api/files/mkdir', make_directory)
app.router.add_post('/api/files/delete', delete_path)
app.router.add_post('/api/files/rename', rename_path)


if __name__ == '__main__':
    print('Web Terminal running at http://localhost:8082')
    web.run_app(app, port=8082, print=None)
