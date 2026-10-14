import logging
import os
import re
import secrets
import smtplib
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import EmailOTP, PhoneOTP

logger = logging.getLogger(__name__)


class OTPError(ValueError):
    pass


class OTPDeliveryError(RuntimeError):
    pass


def normalize_mobile(value: str) -> str:
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 10 and digits[0] in '6789':
        digits = '91' + digits
    elif len(digits) == 12 and digits.startswith('91'):
        pass
    else:
        raise OTPError('Enter a valid 10-digit Indian mobile number.')
    return '+' + digits


def local_mobile_number(value: str) -> str:
    return normalize_mobile(value)[3:]


def _deliver_sms(mobile: str, code: str) -> str | None:
    account_sid = os.getenv('TWILIO_ACCOUNT_SID', '').strip()
    auth_token = os.getenv('TWILIO_AUTH_TOKEN', '').strip()
    from_number = os.getenv('TWILIO_FROM_NUMBER', '').strip()
    if not (account_sid and auth_token and from_number):
        if settings.DEBUG:
            logger.warning('OTP SMS provider is not configured; using local development delivery.')
            if os.getenv('OTP_RETURN_CODE_IN_DEBUG', 'True').strip().lower() in {'1', 'true', 'yes'}:
                return code
            return None
        raise OTPDeliveryError('SMS delivery is not configured. Contact the administrator.')

    endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    message = f'Your KhelMP verification code is {code}. It expires in 5 minutes.'
    try:
        response = requests.post(
            endpoint,
            auth=(account_sid, auth_token),
            data={'From': from_number, 'To': mobile, 'Body': message},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        logger.warning('OTP SMS delivery failed for a masked destination.')
        raise OTPDeliveryError('We could not send the OTP. Please try again shortly.') from error
    return None


def issue_phone_otp(mobile: str, purpose: str) -> str | None:
    if purpose not in PhoneOTP.Purpose.values:
        raise OTPError('Invalid OTP purpose.')
    mobile = normalize_mobile(mobile)
    now = timezone.now()
    code = f'{secrets.randbelow(1_000_000):06d}'
    challenge = PhoneOTP.objects.create(
        mobile=mobile,
        purpose=purpose,
        code_hash=make_password(code),
        expires_at=now + timedelta(minutes=5),
    )
    try:
        development_code = _deliver_sms(mobile, code)
    except OTPDeliveryError:
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        raise
    return development_code


def verify_phone_otp(mobile: str, code: str, purpose: str) -> bool:
    try:
        mobile = normalize_mobile(mobile)
    except OTPError:
        return False
    if not re.fullmatch(r'\d{6}', code or '') or purpose not in PhoneOTP.Purpose.values:
        return False

    now = timezone.now()
    with transaction.atomic():
        challenge = (
            PhoneOTP.objects.select_for_update()
            .filter(
                mobile=mobile,
                purpose=purpose,
                consumed_at__isnull=True,
                expires_at__gt=now,
                attempts__lt=5,
            )
            .first()
        )
        if challenge is None:
            return False
        challenge.attempts += 1
        challenge.save(update_fields=('attempts',))
        if not check_password(code, challenge.code_hash):
            return False
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        return True


def issue_email_otp(email: str) -> None:
    if not all((settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD, settings.DEFAULT_FROM_EMAIL)):
        raise OTPDeliveryError('Email OTP is not configured. Contact the administrator.')

    now = timezone.now()
    code = f'{secrets.randbelow(1_000_000):06d}'
    challenge = EmailOTP.objects.create(
        email=email,
        code_hash=make_password(code),
        expires_at=now + timedelta(minutes=5),
    )
    try:
        sent_count = send_mail(
            'Your KhelMP login code',
            f'Your KhelMP login code is {code}. It expires in 5 minutes.',
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
        )
        if sent_count != 1:
            raise OTPDeliveryError('We could not send the login code. Please try again shortly.')
    except (OSError, smtplib.SMTPException) as error:
        logger.warning('Email OTP delivery failed for a masked destination.')
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        raise OTPDeliveryError('We could not send the login code. Please try again shortly.') from error
    except OTPDeliveryError:
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        raise


def verify_email_otp(email: str, code: str) -> bool:
    if not re.fullmatch(r'\d{6}', code or ''):
        return False

    now = timezone.now()
    with transaction.atomic():
        challenge = (
            EmailOTP.objects.select_for_update()
            .filter(
                email=email,
                consumed_at__isnull=True,
                expires_at__gt=now,
                attempts__lt=5,
            )
            .first()
        )
        if challenge is None:
            return False
        challenge.attempts += 1
        challenge.save(update_fields=('attempts',))
        if not check_password(code, challenge.code_hash):
            return False
        challenge.consumed_at = now
        challenge.save(update_fields=('consumed_at',))
        return True