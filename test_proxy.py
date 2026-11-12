import unittest
from aiohttp import web, ClientSession
from yarl import URL
import proxy


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hits = []
        self.home = '<a id="stop">Stop My Server</a>'
        self.home_redirect = None
        self.workspace_redirect = None
        self.app = web.Application()
        self.app.router.add_route('*', '/{path:.*}', self.handle)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.origin = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def handle(self, request):
        self.hits.append(request.path)
        if request.path == '/hub/home':
            if self.home_redirect:
                raise web.HTTPFound(self.home_redirect)
            return web.Response(text=self.home, content_type='text/html')
        if request.path.startswith('/user/'):
            if self.workspace_redirect:
                raise web.HTTPFound(self.workspace_redirect)
            return web.Response(text='workspace ready')
        self.fail('Unexpected request: ' + request.path)

    async def test_login_redirect_is_not_followed(self):
        self.home_redirect = '/hub/login?next=/hub/home'
        async with ClientSession() as session:
            with self.assertRaisesRegex(proxy.PortalError, 'authentication'):
                await proxy.check_workspace(session, self.origin)
        self.assertEqual(self.hits, ['/hub/home'])

    async def test_stopped_workspace_does_not_try_to_spawn(self):
        self.home = '<a id="start">Start My Server</a>'
        async with ClientSession() as session:
            with self.assertRaisesRegex(proxy.PortalError, 'stopped'):
                await proxy.check_workspace(session, self.origin)
        self.assertEqual(self.hits, ['/hub/home'])

    async def test_workspace_oauth_login_reports_authentication(self):
        self.workspace_redirect = '/hub/login'
        async with ClientSession() as session:
            with self.assertRaisesRegex(proxy.PortalError, 'authentication'):
                await proxy.check_workspace(session, self.origin)
        self.assertNotIn('/hub/login', self.hits)

    async def test_workspace_spawn_redirect_never_allocates(self):
        self.workspace_redirect = '/hub/spawn'
        async with ClientSession() as session:
            with self.assertRaisesRegex(proxy.PortalError, 'stopped'):
                await proxy.check_workspace(session, self.origin)
        self.assertNotIn('/hub/spawn', self.hits)

    async def test_ready_workspace_preserves_encoded_username(self):
        async with ClientSession() as session:
            await proxy.check_workspace(session, self.origin)
        self.assertEqual(len(self.hits), 2)

    async def test_cookie_paths_do_not_overwrite_each_other(self):
        cookies = [
            {'name': '_xsrf', 'value': 'hub-test-value', 'domain': 'dub.aupcloud.io', 'path': '/hub/', 'secure': True},
            {'name': '_xsrf', 'value': 'user-test-value', 'domain': 'dub.aupcloud.io', 'path': '/user/github%3Ariverdave/', 'secure': True},
        ]
        jar = proxy.cookie_jar(cookies)
        self.assertEqual(jar.filter_cookies(URL('https://dub.aupcloud.io/hub/home'))['_xsrf'].value, 'hub-test-value')
        self.assertEqual(jar.filter_cookies(URL('https://dub.aupcloud.io/user/github%3Ariverdave/', encoded=True))['_xsrf'].value, 'user-test-value')

    async def test_browser_signed_cookie_is_not_double_quoted(self):
        jar = proxy.cookie_jar([{'name': 'login-test', 'value': '"2|signed-test"', 'domain': 'dub.aupcloud.io', 'path': '/hub/'}])
        cookie = jar.filter_cookies(URL('https://dub.aupcloud.io/hub/home'))['login-test']
        self.assertEqual(cookie.value, '2|signed-test')
        self.assertEqual(cookie.coded_value, '"2|signed-test"')


if __name__ == '__main__':
    unittest.main()
