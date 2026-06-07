import importlib
import os
import shutil
import stat
import sys
import tempfile
import unittest


# Test-only credentials.
TEST_PASSWORD = 'pass1234'  # nosec B105
TEST_INVITE_TOKEN = 'invite-secret'  # nosec B105


class HBExtraSecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='hbextra-test-')
        os.environ['HBEXTRA_DATA_DIR'] = self.tmpdir
        sys.modules.pop('hbextra', None)
        self.hbextra = importlib.import_module('hbextra')
        self.hbextra.init_db()
        self.client = self.hbextra.app.test_client()

    def tearDown(self):
        sys.modules.pop('hbextra', None)
        os.environ.pop('HBEXTRA_DATA_DIR', None)
        os.environ.pop('HBEXTRA_REGISTRATION_TOKEN', None)
        shutil.rmtree(self.tmpdir)

    def register(self, username='alice', password=TEST_PASSWORD, token=None):
        payload = {'username': username, 'password': password}
        if token is not None:
            payload['registration_token'] = token
        return self.client.post('/hbextra/api/register', json=payload)

    def csrf_token(self):
        return self.client.get('/hbextra/api/me').json['csrf_token']

    def test_second_registration_rejected_without_token(self):
        first = self.register('alice')
        self.assertEqual(first.status_code, 200)
        second = self.hbextra.app.test_client().post(
            '/hbextra/api/register',
            json={'username': 'bob', 'password': TEST_PASSWORD},
        )
        self.assertEqual(second.status_code, 403)
        self.assertFalse(second.json['ok'])

    def test_second_registration_allowed_with_configured_token(self):
        os.environ['HBEXTRA_REGISTRATION_TOKEN'] = TEST_INVITE_TOKEN
        first = self.register('alice')
        self.assertEqual(first.status_code, 200)
        second = self.hbextra.app.test_client().post(
            '/hbextra/api/register',
            json={
                'username': 'bob',
                'password': TEST_PASSWORD,
                'registration_token': TEST_INVITE_TOKEN,
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json['ok'])

    def test_import_rejects_scriptable_url_and_bad_counts(self):
        self.register('alice')
        csrf = self.csrf_token()
        bad_payloads = [
            {
                'entries': [{
                    'url': "https://example.com/x');alert(1);//",
                    'title': 'bad',
                    'date': '2026-06-07T00:00:00+00:00',
                    'count': 1,
                    'cats': [],
                    'tags': [{'tag': 'safe', 'count': 1}],
                    'tags_loaded': 1,
                }]
            },
            {
                'entries': [{
                    'url': 'https://example.com/article',
                    'title': 'bad',
                    'date': '2026-06-07T00:00:00+00:00',
                    'count': '<img src=x onerror=alert(1)>',
                    'cats': [],
                    'tags': [{'tag': 'safe', 'count': 1}],
                    'tags_loaded': 1,
                }]
            },
            {
                'entries': [{
                    'url': 'https://example.com/article',
                    'title': 'bad',
                    'date': '2026-06-07T00:00:00+00:00',
                    'count': 1,
                    'cats': [],
                    'tags': [{'tag': 'safe', 'count': 'bad'}],
                    'tags_loaded': 1,
                }]
            },
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                res = self.client.post(
                    '/hbextra/api/import',
                    json=payload,
                    headers={'X-CSRF-Token': csrf},
                )
                self.assertEqual(res.status_code, 400)
                self.assertFalse(res.json['ok'])

    def test_import_accepts_valid_entry(self):
        self.register('alice')
        csrf = self.csrf_token()
        payload = {
            'entries': [{
                'url': 'https://example.com/article',
                'title': 'Example',
                'date': '2026-06-07T00:00:00+00:00',
                'count': 3,
                'cats': ['it'],
                'tags': [{'tag': 'security', 'count': 2}],
                'tags_loaded': 1,
            }],
            'memberships': [{'url': 'https://example.com/article', 'mode': 'new', 'cat': ''}],
            'user_stars': ['https://example.com/article'],
        }
        res = self.client.post('/hbextra/api/import', json=payload, headers={'X-CSRF-Token': csrf})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json['imported'], 1)
        entries = self.client.get('/hbextra/api/entries?mode=new&cat=&page=0&per_page=10')
        self.assertEqual(entries.status_code, 200)
        self.assertEqual(entries.json['entries'][0]['count'], 3)

    def test_proxy_response_has_sandbox_csp(self):
        class FakeResponse:
            headers = {'Content-Type': 'text/html'}

            def __init__(self):
                self.body = b'<html><body><script>window.evil=1</script></body></html>'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                if not self.body:
                    return b''
                if size is None or size < 0:
                    size = len(self.body)
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        class FakeOpener:
            def open(self, req, timeout=0):
                return FakeResponse()

        self.register('alice')
        self.hbextra._no_redirect_opener = FakeOpener()
        res = self.client.get('/hbextra/api/proxy?url=https://example.com/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('sandbox', res.headers.get('Content-Security-Policy', ''))
        self.assertIn('X-Content-Type-Options', res.headers)

    def test_normal_responses_have_security_headers(self):
        self.register('alice')
        res = self.client.get('/hbextra/api/me')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(res.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertIn('default-src', res.headers.get('Content-Security-Policy', ''))

    def test_secret_and_db_permissions_are_private(self):
        self.register('alice')
        secret_mode = stat.S_IMODE(os.stat(os.path.join(self.tmpdir, '.secret_key')).st_mode)
        db_mode = stat.S_IMODE(os.stat(os.path.join(self.tmpdir, 'hbextra.db')).st_mode)
        self.assertEqual(secret_mode, 0o600)
        self.assertEqual(db_mode, 0o600)

    def test_malicious_xml_is_rejected_without_crashing(self):
        xml = """<?xml version="1.0"?>
<!DOCTYPE root [
<!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<rss><channel><item><title>&xxe;</title></item></channel></rss>"""
        self.assertEqual(self.hbextra.parse_rss(xml), [])


if __name__ == '__main__':
    unittest.main()
