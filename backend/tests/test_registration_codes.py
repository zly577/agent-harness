"""Isolated registration API tests; no real mail or business database."""
import unittest
from datetime import timedelta
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.api.routes.auth import router
from app.core.config import settings
from app.db.models import User, RegistrationCode
from app.db.session import get_db


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        User.__table__.create(self.engine)
        RegistrationCode.__table__.create(self.engine)
        self.sessions = sessionmaker(self.engine)
        app = FastAPI()
        app.include_router(router)
        def db():
            with self.sessions() as session:
                yield session
        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        self.codes = {}
        self.mail = patch('app.api.routes.auth.send_registration_code', side_effect=lambda email, code: self.codes.update({email: code})).start()
        patch.object(settings, 'allow_registration', True).start()
        patch.object(settings, 'registration_email_verification', True).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.client.close)

    def send(self, email='person@example.com'):
        return self.client.post('/api/v1/auth/register/code', json={'email': email})

    def register(self, code=None, email='person@example.com'):
        return self.client.post('/api/v1/auth/register', json={'email': email, 'password': 'StrongPassword123', 'verification_code': code or self.codes.get(email, '123456')})

    def age(self, minutes):
        with self.sessions() as db:
            item = db.get(RegistrationCode, 'person@example.com')
            item.sent_at -= timedelta(minutes=minutes)
            item.expires_at -= timedelta(minutes=minutes)
            db.commit()

    def test_flow_normalization_single_use(self):
        result = self.send(' Person@Example.com ')
        self.assertEqual(result.status_code, 200)
        code = self.codes['person@example.com']
        self.assertNotIn(code, result.text)
        with self.sessions() as db:
            self.assertNotEqual(db.get(RegistrationCode, 'person@example.com').code_hash, code)
        self.assertEqual(self.register(code, 'PERSON@example.com').status_code, 201)
        with self.sessions() as db:
            self.assertIsNone(db.get(RegistrationCode, 'person@example.com'))
        self.assertEqual(self.register().status_code, 409)
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.client.post('/api/v1/auth/login', json={'email': 'person@example.com', 'password': 'StrongPassword123'}).status_code, 200)

    def test_missing_wrong_expired_attempt_limit(self):
        self.assertEqual(self.register().status_code, 400)
        self.send()
        wrong = '000000' if self.codes['person@example.com'] != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.register(wrong).status_code, 400)
        self.assertEqual(self.register().status_code, 429)
        self.age(11)
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.register().status_code, 201)

    def test_cooldown_resend(self):
        self.send()
        old = self.codes['person@example.com']
        self.assertEqual(self.send('PERSON@example.com').status_code, 429)
        self.assertEqual(self.mail.call_count, 1)
        self.age(2)
        with patch('app.api.routes.auth.secrets.randbelow', return_value=(int(old) + 1) % 1000000):
            self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.register(old).status_code, 400)
        self.assertEqual(self.register().status_code, 201)

    def test_mail_failure_rollback(self):
        self.mail.side_effect = RuntimeError('secret smtp error')
        result = self.send()
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('secret', result.text)
        with self.sessions() as db:
            self.assertIsNone(db.get(RegistrationCode, 'person@example.com'))
        self.mail.side_effect = lambda email, code: self.codes.update({email: code})
        self.assertEqual(self.send().status_code, 200)

    def test_optional_verification_direct_registration(self):
        with patch.object(settings, 'registration_email_verification', False):
            config = self.client.get('/api/v1/auth/registration-config').json()
            self.assertFalse(config['email_verification_required'])
            self.assertEqual(self.send().status_code, 400)
            result = self.client.post('/api/v1/auth/register', json={'email': 'direct@example.com', 'password': 'StrongPassword123'})
            self.assertEqual(result.status_code, 201)
            self.mail.assert_not_called()
            login = self.client.post('/api/v1/auth/login', json={'email': 'direct@example.com', 'password': 'StrongPassword123'})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(self.client.post('/api/v1/auth/register', json={'email': 'direct@example.com', 'password': 'StrongPassword123'}).status_code, 409)

    def test_validation_email_binding_disabled(self):
        for email in ['admin@local', 'a b@example.com', 'a@example.com\r\nBcc:x@y.com']:
            self.assertEqual(self.send(email).status_code, 422)
        self.send()
        self.assertEqual(self.register(email='other@example.com').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/auth/register', json={'email': 'x@example.com', 'password': 'StrongPassword123'}).status_code, 422)
        with patch.object(settings, 'allow_registration', False):
            self.assertEqual(self.send().status_code, 403)
            self.assertEqual(self.register().status_code, 403)


if __name__ == '__main__':
    unittest.main()
