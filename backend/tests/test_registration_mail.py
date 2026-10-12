"""SMTP transport, provider resolution and OAuth regression tests."""
import socketserver
import threading
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch

from app.core.config import settings
from app.core.registration_mail import send_registration_code, microsoft_access_token
from app.core.smtp_profiles import SMTP_PROFILES, resolve_smtp


class MailTests(unittest.TestCase):
    def setUp(self):
        patch.multiple(settings, smtp_provider="custom", smtp_host="localhost", smtp_port=587,
                       smtp_ssl=False, smtp_starttls=True, smtp_from="sender@example.com",
                       smtp_username="sender@example.com", smtp_password="test-only",
                       smtp_auth_mode="auto", smtp_oauth_client_id="test-client",
                       smtp_oauth_client_secret="", smtp_oauth_refresh_token="",
                       smtp_oauth_tenant="common").start()
        self.addCleanup(patch.stopall)

    def test_all_presets(self):
        for provider, expected in SMTP_PROFILES.items():
            with self.subTest(provider=provider), patch.object(settings, "smtp_provider", provider):
                profile, starttls, auth = resolve_smtp(settings)
                self.assertEqual(profile.host, expected.host)
                self.assertTrue(profile.ssl or starttls)
                self.assertEqual(auth, expected.auth)
        with patch.object(settings, "smtp_provider", "unknown"):
            self.assertRaises(ValueError, resolve_smtp, settings)

    def test_starttls_before_login_and_message(self):
        with patch("app.core.registration_mail.smtplib.SMTP") as transport:
            smtp = transport.return_value.__enter__.return_value
            send_registration_code("recipient@company.example", "123456")
            self.assertEqual([c[0] for c in smtp.method_calls], ['starttls', 'login', 'send_message'])
            msg = smtp.send_message.call_args.args[0]
            self.assertEqual(msg['To'], 'recipient@company.example')
            self.assertIn('123456', msg.get_content())

    def test_ssl_and_no_plaintext_credentials(self):
        with patch.object(settings, "smtp_provider", "qq"), patch("app.core.registration_mail.smtplib.SMTP_SSL") as transport:
            send_registration_code("person@163.com", "123456")
            self.assertEqual(transport.call_args.args, ('smtp.qq.com', 465))
            self.assertFalse(transport.return_value.__enter__.return_value.starttls.called)
        with patch.object(settings, 'smtp_starttls', False):
            self.assertRaises(ValueError, resolve_smtp, settings)

    def test_oauth_refresh_and_client_credentials(self):
        with patch('app.core.registration_mail.httpx.post', return_value=Mock(json=lambda: {'access_token': 'test-token'})) as post:
            with patch.object(settings, 'smtp_oauth_refresh_token', 'test-refresh'):
                self.assertEqual(microsoft_access_token(), 'test-token')
                self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'refresh_token')
            with patch.multiple(settings, smtp_oauth_tenant='example-tenant', smtp_oauth_client_secret='test-secret'):
                self.assertEqual(microsoft_access_token(), 'test-token')
                self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'client_credentials')
        with patch('app.core.registration_mail.httpx.post', side_effect=RuntimeError('sensitive-token')), patch.object(settings, 'smtp_oauth_refresh_token', 'test-refresh'):
            with self.assertRaises(RuntimeError) as raised:
                microsoft_access_token()
            self.assertNotIn('sensitive-token', str(raised.exception))

    def test_xoauth2_without_password_fallback(self):
        with patch.object(settings, 'smtp_provider', 'outlook'), patch('app.core.registration_mail.microsoft_access_token', return_value='test-token'), patch('app.core.registration_mail.smtplib.SMTP') as transport:
            smtp = transport.return_value.__enter__.return_value
            send_registration_code('person@qq.com', '123456')
            self.assertFalse(smtp.login.called)
            method, callback = smtp.auth.call_args.args
            self.assertEqual(method, 'XOAUTH2')
            self.assertIn('auth=Bearer test-token', callback())
            self.assertEqual(callback(b'error'), '')

    def test_real_local_smtp_delivery(self):
        messages = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 localhost test SMTP\r\n')
                while line := self.rfile.readline():
                    command = line.split(b' ', 1)[0].strip().upper()
                    if command == b'DATA':
                        self.wfile.write(b'354 send message\r\n')
                        data = []
                        while (line := self.rfile.readline()) not in (b'.\r\n', b''):
                            data.append(line)
                        messages.append(b''.join(data))
                    elif command == b'QUIT':
                        self.wfile.write(b'221 bye\r\n')
                        break
                    self.wfile.write(b'250 OK\r\n')
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.multiple(settings, smtp_host='127.0.0.1', smtp_port=server.server_address[1], smtp_starttls=False, smtp_username=''):
                    send_registration_code('person@163.com', '654321')
                msg = BytesParser(policy=policy.default).parsebytes(messages[0])
                self.assertEqual(msg['Subject'], 'Agent Harness 注册验证码')
                self.assertIn('654321', msg.get_content())
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
