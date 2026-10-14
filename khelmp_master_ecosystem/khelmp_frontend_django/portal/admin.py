from django.contrib import admin
from django.contrib import messages
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
import hashlib
import logging
import secrets
import smtplib

from .models import AccountProfile, Athlete, AthletePost, AthleteResult, SelectorApplication, SelectorInvite

logger = logging.getLogger(__name__)


@admin.register(Athlete)
class AthleteAdmin(admin.ModelAdmin):
    list_display = ('registration_number', 'full_name', 'sport_category', 'district', 'registered_at')
    search_fields = ('full_name', 'mobile_number', 'district', 'sport_category')
    list_filter = ('sport_category', 'district')


@admin.register(AthleteResult)
class AthleteResultAdmin(admin.ModelAdmin):
    list_display = ('athlete', 'event_name', 'result', 'high_score', 'event_date', 'entered_by')
    search_fields = ('athlete__full_name', 'event_name', 'result', 'high_score')
    list_filter = ('event_date', 'athlete__sport_category')


admin.site.register(AthletePost)


@admin.register(SelectorApplication)
class SelectorApplicationAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'organization', 'position', 'status', 'submitted_at')
    list_filter = ('status', 'submitted_at')
    search_fields = ('full_name', 'email', 'organization')
    readonly_fields = tuple(field.name for field in SelectorApplication._meta.fields)
    actions = ('approve_and_issue_invites', 'reject_applications')

    @admin.action(description='Approve selected applications and issue unique codes')
    def approve_and_issue_invites(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, 'Only a superuser can issue selector invitations.', messages.ERROR)
            return
        if not all((settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD, settings.DEFAULT_FROM_EMAIL)):
            self.message_user(
                request,
                'Gmail delivery is not configured. Set EMAIL_HOST_USER, EMAIL_HOST_PASSWORD and DEFAULT_FROM_EMAIL in .env first.',
                messages.ERROR,
            )
            return

        issued_count = 0
        for application in queryset.filter(status=SelectorApplication.Status.PENDING):
            raw_code = secrets.token_urlsafe(32)
            code_hash = hashlib.sha256(raw_code.encode('utf-8')).hexdigest()
            signup_url = request.build_absolute_uri(f"{reverse('register')}?role=selector")
            try:
                with transaction.atomic():
                    invite = SelectorInvite.objects.create(
                        email=application.email.strip().lower(),
                        code_hash=code_hash,
                        application=application,
                        issued_by=request.user,
                    )
                    email_sent = send_mail(
                        subject='Your KhelMP selector access code',
                        message=(
                            f"Hello {application.full_name},\n\n"
                            'Your selector access application has been approved.\n\n'
                            f'One-time selector code: {raw_code}\n'
                            f'Register here: {signup_url}\n\n'
                            'Use the same email address that received this message. This code expires in 7 days '
                            'and works once only. Keep it private. If you did not request selector access, '
                            'contact the KhelMP administrator and do not use this code.\n'
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[application.email],
                        fail_silently=False,
                    )
                    if email_sent != 1:
                        raise smtplib.SMTPException('Email backend did not confirm delivery.')
                    application.status = SelectorApplication.Status.APPROVED
                    application.reviewed_at = timezone.now()
                    application.reviewed_by = request.user
                    application.save(update_fields=('status', 'reviewed_at', 'reviewed_by'))
                self.message_user(
                    request,
                    f'Approved {application.email}; one-time code emailed successfully. It expires in 7 days.',
                    messages.SUCCESS,
                )
                issued_count += 1
            except Exception:
                logger.exception('Could not email a selector invitation for application %s', application.pk)
                self.message_user(
                    request,
                    f'Could not send the selector email to {application.email}. The application remains pending; check SMTP settings and retry.',
                    messages.ERROR,
                )

        if not issued_count:
            self.message_user(request, 'No pending applications were selected.', messages.WARNING)

    @admin.action(description='Reject selected applications')
    def reject_applications(self, request, queryset):
        updated = queryset.filter(status=SelectorApplication.Status.PENDING).update(
            status=SelectorApplication.Status.REJECTED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(request, f'{updated} application(s) rejected.', messages.SUCCESS)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SelectorInvite)
class SelectorInviteAdmin(admin.ModelAdmin):
    list_display = ('email', 'application', 'created_at', 'expires_at', 'redeemed_at', 'revoked_at', 'issued_by')
    list_filter = ('created_at', 'expires_at', 'redeemed_at', 'revoked_at')
    search_fields = ('email', 'application__full_name')
    readonly_fields = tuple(field.name for field in SelectorInvite._meta.fields)
    actions = ('revoke_unused_invites',)

    @admin.action(description='Revoke selected unused selector codes')
    def revoke_unused_invites(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, 'Only a superuser can revoke selector invitations.', messages.ERROR)
            return
        updated = queryset.filter(redeemed_at__isnull=True, revoked_at__isnull=True).update(
            revoked_at=timezone.now(),
        )
        self.message_user(request, f'{updated} unused invitation(s) revoked.', messages.SUCCESS)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountProfile)
class AccountProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role')
    readonly_fields = ('user', 'role')

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
