from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from people.clerk_backend import ClerkBackendError, find_clerk_user_by_email
from people.clerk_users import upsert_from_clerk

User = get_user_model()


def _clerk_payload(clerk_id, *, email='admin@hinthar.org', username='adminuser'):
    return {
        'id': clerk_id,
        'username': username,
        'primary_email_address_id': 'idn_1',
        'email_addresses': [{'id': 'idn_1', 'email_address': email}],
    }


@override_settings(CLERK_SECRET_KEY='sk_test')
class FindClerkUserByEmailTests(TestCase):
    def _mock_response(self, payload, *, ok=True, status_code=200):
        resp = MagicMock()
        resp.ok = ok
        resp.status_code = status_code
        resp.json.return_value = payload
        resp.text = ''
        return resp

    def test_returns_single_case_insensitive_match(self):
        payload = [_clerk_payload('user_1', email='Admin@Hinthar.org')]
        with patch('people.clerk_backend.requests.get', return_value=self._mock_response(payload)):
            result = find_clerk_user_by_email('admin@hinthar.org')
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 'user_1')

    def test_returns_none_when_no_match(self):
        with patch('people.clerk_backend.requests.get', return_value=self._mock_response([])):
            result = find_clerk_user_by_email('missing@hinthar.org')
        self.assertIsNone(result)

    def test_raises_on_multiple_matches(self):
        payload = [
            _clerk_payload('user_1', email='dup@hinthar.org'),
            _clerk_payload('user_2', email='dup@hinthar.org'),
        ]
        with patch('people.clerk_backend.requests.get', return_value=self._mock_response(payload)):
            with self.assertRaises(ClerkBackendError):
                find_clerk_user_by_email('dup@hinthar.org')


@override_settings(CLERK_SECRET_KEY='sk_test')
class BootstrapAdminCommandTests(TestCase):
    def test_bootstrap_new_user_by_clerk_id(self):
        out = StringIO()
        with patch(
            'people.management.commands.bootstrap_admin.get_clerk_user',
            return_value=_clerk_payload('user_admin_1', email='owner@hinthar.org', username='owner'),
        ):
            call_command('bootstrap_admin', clerk_id='user_admin_1', stdout=out)

        user = User.objects.get(clerk_id='user_admin_1')
        self.assertEqual(user.role, 'admin')
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.email, 'owner@hinthar.org')
        self.assertIn('Created admin user', out.getvalue())

    def test_bootstrap_promotes_existing_inactive_pending_user_by_email_and_is_idempotent(self):
        user, _ = upsert_from_clerk('user_existing', email='lead@hinthar.org', username='lead')
        user.is_active = False
        user.save(update_fields=['is_active'])

        payload = _clerk_payload('user_existing', email='lead@hinthar.org', username='lead')
        with patch(
            'people.management.commands.bootstrap_admin.find_clerk_user_by_email',
            return_value=payload,
        ):
            out1 = StringIO()
            call_command('bootstrap_admin', email='lead@hinthar.org', stdout=out1)
            out2 = StringIO()
            call_command('bootstrap_admin', email='lead@hinthar.org', stdout=out2)

        user.refresh_from_db()
        self.assertEqual(User.objects.filter(clerk_id='user_existing').count(), 1)
        self.assertEqual(user.role, 'admin')
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertIn('Updated admin user', out1.getvalue())
        self.assertIn('Updated admin user', out2.getvalue())

    def test_no_superuser_flag(self):
        with patch(
            'people.management.commands.bootstrap_admin.get_clerk_user',
            return_value=_clerk_payload('user_staff_admin'),
        ):
            call_command('bootstrap_admin', clerk_id='user_staff_admin', no_superuser=True, stdout=StringIO())

        user = User.objects.get(clerk_id='user_staff_admin')
        self.assertEqual(user.role, 'admin')
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_env_fallback_and_cli_precedence(self):
        with patch.dict('os.environ', {'BOOTSTRAP_ADMIN_EMAIL': 'env@hinthar.org'}):
            with patch(
                'people.management.commands.bootstrap_admin.find_clerk_user_by_email',
                return_value=_clerk_payload('user_env', email='env@hinthar.org'),
            ) as mock_find:
                call_command('bootstrap_admin', stdout=StringIO())
                mock_find.assert_called_once_with('env@hinthar.org')

            with patch(
                'people.management.commands.bootstrap_admin.get_clerk_user',
                return_value=_clerk_payload('user_cli'),
            ) as mock_get:
                call_command('bootstrap_admin', clerk_id='user_cli', stdout=StringIO())
                mock_get.assert_called_once_with('user_cli')

    def test_validation_and_not_found_errors(self):
        with patch.dict('os.environ', {}, clear=False):
            with self.assertRaises(CommandError):
                call_command('bootstrap_admin')

        with self.assertRaises(CommandError):
            call_command('bootstrap_admin', clerk_id='user_1', email='a@b.com')

        with patch('people.management.commands.bootstrap_admin.get_clerk_user', return_value=None):
            with self.assertRaises(CommandError):
                call_command('bootstrap_admin', clerk_id='user_missing')

        with patch(
            'people.management.commands.bootstrap_admin.find_clerk_user_by_email',
            side_effect=ClerkBackendError('Clerk HTTP 500'),
        ):
            with self.assertRaises(CommandError):
                call_command('bootstrap_admin', email='err@hinthar.org')
