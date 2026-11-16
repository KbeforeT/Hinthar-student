"""
Idempotent management command to bootstrap an admin user from Clerk.

Usage:
    python manage.py bootstrap_admin --email admin@school.org
    python manage.py bootstrap_admin --clerk-id user_2abc123...
    BOOTSTRAP_ADMIN_EMAIL=admin@school.org python manage.py bootstrap_admin
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from people.clerk_backend import (
    ClerkBackendError,
    find_clerk_user_by_email,
    get_clerk_user,
)
from people.clerk_users import identity_from_clerk_user, upsert_from_clerk


class Command(BaseCommand):
    help = (
        'Look up an existing Clerk user by --clerk-id or --email, upsert their '
        'local Django User row, and promote them to admin (role=admin, is_staff, is_superuser).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--clerk-id',
            dest='clerk_id',
            default='',
            help='Clerk user ID (e.g. user_2abc...). Falls back to BOOTSTRAP_ADMIN_CLERK_ID.',
        )
        parser.add_argument(
            '--email',
            dest='email',
            default='',
            help='Primary or secondary email in Clerk. Falls back to BOOTSTRAP_ADMIN_EMAIL.',
        )
        parser.add_argument(
            '--no-superuser',
            action='store_true',
            default=False,
            help='Set role=admin and is_staff=True without granting is_superuser=True.',
        )

    def handle(self, *args, **options):
        cli_clerk_id = (options.get('clerk_id') or '').strip()
        cli_email = (options.get('email') or '').strip()

        if cli_clerk_id and cli_email:
            raise CommandError('Provide either --clerk-id or --email, not both.')

        if cli_clerk_id:
            clerk_id = cli_clerk_id
            email = ''
        elif cli_email:
            clerk_id = ''
            email = cli_email
        else:
            clerk_id = os.getenv('BOOTSTRAP_ADMIN_CLERK_ID', '').strip()
            email = os.getenv('BOOTSTRAP_ADMIN_EMAIL', '').strip()
            if clerk_id and email:
                raise CommandError(
                    'Set either BOOTSTRAP_ADMIN_CLERK_ID or BOOTSTRAP_ADMIN_EMAIL, not both.'
                )

        if not clerk_id and not email:
            raise CommandError(
                'Provide --clerk-id or --email (or set BOOTSTRAP_ADMIN_CLERK_ID / BOOTSTRAP_ADMIN_EMAIL).'
            )

        try:
            if clerk_id:
                clerk_data = get_clerk_user(clerk_id)
                if not clerk_data:
                    raise CommandError(f"Clerk user '{clerk_id}' was not found.")
            else:
                clerk_data = find_clerk_user_by_email(email)
                if not clerk_data:
                    raise CommandError(f"No Clerk user found with email '{email}'.")
        except ClerkBackendError as exc:
            raise CommandError(str(exc)) from exc

        resolved_id, resolved_email, resolved_username = identity_from_clerk_user(clerk_data)
        if not resolved_id:
            raise CommandError('Clerk user payload is missing an id.')

        grant_superuser = not bool(options.get('no_superuser'))

        with transaction.atomic():
            user, created = upsert_from_clerk(
                resolved_id,
                email=resolved_email,
                username=resolved_username,
            )
            update_fields = []
            desired = {
                'role': 'admin',
                'is_active': True,
                'is_staff': True,
                'is_superuser': grant_superuser,
            }
            for field, value in desired.items():
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    update_fields.append(field)
            if update_fields:
                user.save(update_fields=update_fields)

        action = 'Created' if created else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'{action} admin user: username={user.username} email={user.email or "-"} '
                f'clerk_id={user.clerk_id} role={user.role} '
                f'is_staff={user.is_staff} is_superuser={user.is_superuser}'
            )
        )
