import asyncio
import hmac
import os
import pty
import struct
import termios
import fcntl
import signal
import pwd
from pathlib import Path
from aiohttp import web


os.environ['AUTH_PASSWORD']='mypassword'
AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', 'admin')
SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key').encode()
PROTECTED_ROUTES = {'/', '/ws'}

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
    loop = asyncio.get_event_loop()

    async def reader():
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
            except ConnectionResetError:
                break

    async def writer():
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                text = msg.data
                if text.startswith('RESIZE:'):
                    _, rows, cols = text.split(':')
                    _set_size(fd, int(rows), int(cols))
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


app = web.Application(middlewares=[auth_middleware])
app.router.add_get('/ws', shell_handler)
app.router.add_get('/', index_handler)
app.router.add_route('GET', '/login', login_handler)
app.router.add_route('POST', '/login', login_handler)
app.router.add_get('/logout', logout_handler)


if __name__ == '__main__':
    print('Web Terminal running at http://localhost:8082')
    web.run_app(app, port=8082, print=None)
