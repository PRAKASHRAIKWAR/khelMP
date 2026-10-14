from io import BytesIO
import os
import re
import smtplib
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib import admin, messages
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.test.client import RequestFactory
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils import timezone
from PIL import Image
from unittest.mock import patch

from .admin import SelectorApplicationAdmin
from .models import AccountProfile, AdminBootstrapState, Athlete, AthletePost, AthleteResult, AIPerformanceClaim, EmailOTP, FriendMessage, PhoneOTP, PlayerConnection, PostComment, PostLike, SelectorApplication, SelectorInvite


User = get_user_model()


def png_upload(name='athlete.png'):
    output = BytesIO()
    Image.new('RGB', (8, 8), color='green').save(output, format='PNG')
    return SimpleUploadedFile(name, output.getvalue(), content_type='image/png')


class AthleteCommunityTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_settings = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_settings.enable()
        self.addCleanup(self.media_settings.disable)

        self.player = User.objects.create_user(username='community-player', password='Test-Only-Password-2026!')
        self.other_player = User.objects.create_user(username='community-other', password='Test-Only-Password-2026!')
        self.selector = User.objects.create_user(username='community-selector', password='Test-Only-Password-2026!')
        AccountProfile.objects.create(user=self.player, role=AccountProfile.Role.PLAYER)
        AccountProfile.objects.create(user=self.other_player, role=AccountProfile.Role.PLAYER)
        AccountProfile.objects.create(user=self.selector, role=AccountProfile.Role.SELECTOR)
        self.athlete = Athlete.objects.create(
            user=self.player,
            full_name='Community Player',
            mobile_number='9123456780',
            district='Bhopal',
            sport_category='Athletics',
        )
        self.other_athlete = Athlete.objects.create(
            user=self.other_player,
            full_name='Community Opponent',
            mobile_number='9123456781',
            district='Indore',
            sport_category='Football',
        )
        self.client.force_login(self.player)
        self.post = AthletePost.objects.create(
            author=self.other_player,
            media=png_upload('feed.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='Training session',
        )

    def tearDown(self):
        for athlete in Athlete.objects.filter(user__in=(self.player, self.other_player)):
            for field_name in ('passport_photo', 'id_proof_pdf', 'video_file'):
                field = getattr(athlete, field_name)
                if field:
                    field.delete(save=False)
        for post in AthletePost.objects.filter(author__in=(self.player, self.other_player)):
            if post.media:
                post.media.delete(save=False)

    def test_signed_in_players_can_see_community_posts(self):
        response = self.client.get('/feed/')
        self.assertContains(response, 'Training session')
        self.assertContains(response, 'Like')
        self.assertContains(response, 'Add a comment')

    def test_player_sees_recent_score_on_feed(self):
        AthleteResult.objects.create(
            athlete=self.athlete,
            event_name='Regional sprint final',
            result='First place',
            high_score='11.02 sec',
            event_date=timezone.localdate(),
            entered_by=self.selector,
        )
        response = self.client.get('/feed/')
        self.assertContains(response, 'Recent results')
        self.assertContains(response, '11.02 sec')
        self.assertContains(response, 'View all')

    def test_like_toggles_and_is_unique_per_user(self):
        url = f'/feed/post/{self.post.pk}/like/'
        self.client.post(url)
        self.assertEqual(PostLike.objects.filter(post=self.post, user=self.player).count(), 1)
        self.client.post(url)
        self.assertFalse(PostLike.objects.filter(post=self.post, user=self.player).exists())

    def test_comment_is_saved_for_post(self):
        response = self.client.post(
            f'/feed/post/{self.post.pk}/comment/',
            {'text': 'Great effort', 'return_to': '/feed/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PostComment.objects.filter(post=self.post, user=self.player, text='Great effort').exists())

    def test_players_can_delete_only_their_own_posts(self):
        own_post = AthletePost.objects.create(
            author=self.player,
            media=png_upload('own.png'),
            media_type=AthletePost.MediaType.PHOTO,
        )
        self.client.post(f'/feed/post/{own_post.pk}/delete/')
        self.assertFalse(AthletePost.objects.filter(pk=own_post.pk).exists())
        self.client.post(f'/feed/post/{self.post.pk}/delete/')
        self.assertTrue(AthletePost.objects.filter(pk=self.post.pk).exists())

    def test_players_only_see_their_own_results_and_selectors_see_all(self):
        AthleteResult.objects.create(
            athlete=self.other_athlete,
            event_name='District final',
            result='Gold',
            high_score='10.8 sec',
            event_date=timezone.localdate(),
            notes='selector internal note',
            entered_by=self.selector,
        )
        player_results = self.client.get('/records/')
        self.assertNotContains(player_results, 'District final')
        self.client.force_login(self.selector)
        selector_results = self.client.get('/records/')
        self.assertContains(selector_results, 'District final')
        self.assertContains(selector_results, 'selector internal note')

    def test_profile_photo_upload_checks_real_image_content(self):
        invalid_photo = SimpleUploadedFile('fake.png', b'not actually an image', content_type='image/png')
        profile_data = {
            'full_name': self.athlete.full_name,
            'district': self.athlete.district,
            'sport_category': self.athlete.sport_category,
        }
        response = self.client.post('/profile/', {**profile_data, 'passport_photo': invalid_photo})
        self.assertEqual(response.status_code, 302)
        self.athlete.refresh_from_db()
        self.assertFalse(self.athlete.passport_photo)

        response = self.client.post('/profile/', {**profile_data, 'passport_photo': png_upload()})
        self.assertEqual(response.status_code, 302)
        self.athlete.refresh_from_db()
        self.assertTrue(self.athlete.passport_photo)

    def test_selector_ai_insight_uses_selected_athlete_posts(self):
        athlete_post = AthletePost.objects.create(
            author=self.other_player,
            media=png_upload('other-athlete.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='State camp practice block',
        )
        selector_client = Client()
        selector_client.force_login(self.selector)
        with patch('portal.views.create_scout_insight', return_value='AI insight text') as mock_insight:
            response = selector_client.post('/ai/insight/', {'athlete_id': self.other_athlete.pk, 'return_to': '/feed/'})
        self.assertEqual(response.status_code, 302)
        context = mock_insight.call_args[0][0]
        self.assertIn('State camp practice block', context)
        self.assertIn(self.other_athlete.full_name, context)
        self.assertNotIn(str(self.selector.pk), context)
        self.assertTrue(AthletePost.objects.filter(pk=athlete_post.pk).exists())

    def test_private_identity_document_is_owner_and_selector_only(self):
        self.other_athlete.id_proof_pdf.save(
            'identity.pdf',
            SimpleUploadedFile('identity.pdf', b'%PDF-test', content_type='application/pdf'),
        )
        private_url = f'/secure-media/{self.other_athlete.id_proof_pdf.name}'
        self.client.logout()
        anonymous_response = self.client.get(private_url)
        self.assertEqual(anonymous_response.status_code, 302)
        anonymous_response.close()
        self.client.force_login(self.player)
        player_response = self.client.get(private_url)
        self.assertEqual(player_response.status_code, 404)
        player_response.close()
        self.client.force_login(self.selector)
        selector_response = self.client.get(private_url)
        self.assertEqual(selector_response.status_code, 200)
        selector_response.close()

    def test_player_search_and_friend_request(self):
        response = self.client.get('/players/', {'q': 'Community Opponent'})
        self.assertContains(response, 'Community Opponent')
        self.assertContains(response, 'Add friend')

        payload = {'action': 'send', 'target_id': self.other_player.pk, 'q': 'Community Opponent'}
        first = self.client.post('/players/', payload)
        second = self.client.post('/players/', payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(PlayerConnection.objects.count(), 1)
        self.assertEqual(PlayerConnection.objects.get().status, PlayerConnection.Status.PENDING)

    def test_receiver_can_accept_and_both_players_see_friend(self):
        connection = PlayerConnection.objects.create(
            player_one=min(self.player, self.other_player, key=lambda user: user.pk),
            player_two=max(self.player, self.other_player, key=lambda user: user.pk),
            requested_by=self.player,
        )
        receiver_client = Client()
        receiver_client.force_login(self.other_player)
        response = receiver_client.post('/players/', {'action': 'accept', 'connection_id': connection.pk})
        self.assertEqual(response.status_code, 302)
        connection.refresh_from_db()
        self.assertEqual(connection.status, PlayerConnection.Status.ACCEPTED)
        self.assertContains(receiver_client.get('/players/'), 'My friends')

    def test_selector_can_search_records_but_cannot_send_friend_requests(self):
        selector_client = Client()
        selector_client.force_login(self.selector)
        response = selector_client.get('/players/', {'q': 'Community Opponent'})
        self.assertContains(response, 'Community Opponent')
        self.assertContains(response, 'Open selector record')
        self.assertNotContains(response, 'Add friend')
        selector_client.post('/players/', {'action': 'send', 'target_id': self.other_player.pk})
        self.assertFalse(PlayerConnection.objects.exists())


class FriendChatTests(TestCase):
    def setUp(self):
        cache.clear()
        self.player = User.objects.create_user(username='chat-player', password='Test-Only-Password-2026!')
        self.friend = User.objects.create_user(username='chat-friend', password='Test-Only-Password-2026!')
        self.stranger = User.objects.create_user(username='chat-stranger', password='Test-Only-Password-2026!')
        for user in (self.player, self.friend, self.stranger):
            AccountProfile.objects.create(user=user, role=AccountProfile.Role.PLAYER)
        self.player_athlete = Athlete.objects.create(
            user=self.player,
            full_name='Chat Player',
            mobile_number='9234567890',
            district='Bhopal',
            sport_category='Hockey',
        )
        self.friend_athlete = Athlete.objects.create(
            user=self.friend,
            full_name='Chat Friend',
            mobile_number='9234567891',
            district='Indore',
            sport_category='Football',
        )
        self.connection = PlayerConnection.objects.create(
            player_one=min(self.player, self.friend, key=lambda user: user.pk),
            player_two=max(self.player, self.friend, key=lambda user: user.pk),
            requested_by=self.player,
            status=PlayerConnection.Status.ACCEPTED,
        )
        self.client.force_login(self.player)

    def test_accepted_friends_can_send_and_receive_messages(self):
        response = self.client.post(f'/chat/{self.friend.pk}/', {'text': '<script>alert(1)</script>'})
        self.assertEqual(response.status_code, 201)
        message = FriendMessage.objects.get()
        self.assertEqual(message.text, '<script>alert(1)</script>')
        self.assertIsNone(message.read_at)

        friend_client = Client()
        friend_client.force_login(self.friend)
        friend_thread = friend_client.get(f'/chat/{self.player.pk}/')
        self.assertEqual(friend_thread.status_code, 200)
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)

        thread = self.client.get(f'/chat/{self.friend.pk}/')
        self.assertContains(thread, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertContains(thread, 'id="chat-messages"')
        self.assertContains(thread, 'portal/chat.js')

        received = friend_client.post(f'/chat/{self.player.pk}/', {'text': 'Good work today.'})
        self.assertEqual(received.status_code, 201)
        updates = self.client.get(f'/chat/{self.friend.pk}/updates/', {'after': message.pk})
        self.assertEqual(updates.status_code, 200)
        self.assertEqual(updates.json()['messages'][0]['text'], 'Good work today.')
        self.assertFalse(updates.json()['messages'][0]['own'])
        incoming = FriendMessage.objects.get(sender=self.friend)
        self.assertIsNotNone(incoming.read_at)

    def test_non_friend_cannot_open_send_or_poll_chat(self):
        for response in (
            self.client.get(f'/chat/{self.stranger.pk}/'),
            self.client.post(f'/chat/{self.stranger.pk}/', {'text': 'Hello'}),
            self.client.get(f'/chat/{self.stranger.pk}/updates/'),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertFalse(FriendMessage.objects.exists())

    def test_friend_message_send_is_rate_limited(self):
        first = self.client.post(f'/chat/{self.friend.pk}/', {'text': 'First message'})
        second = self.client.post(f'/chat/{self.friend.pk}/', {'text': 'Second message'})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(FriendMessage.objects.count(), 1)

    def test_inbox_shows_friend_and_unread_count(self):
        FriendMessage.objects.create(sender=self.friend, recipient=self.player, text='See you at practice.')
        response = self.client.get('/chat/')
        self.assertContains(response, 'Chat Friend')
        self.assertContains(response, 'See you at practice.')
        self.assertContains(response, 'chat-unread')


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_HOST_USER='admin@gmail.com',
    EMAIL_HOST_PASSWORD='test-app-password',
    DEFAULT_FROM_EMAIL='KhelMP <admin@gmail.com>',
)
class SelectorInviteEmailTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='invite-mail-admin',
            email='admin@example.test',
            password='Test-Only-Password-2026!',
        )
        self.application = SelectorApplication.objects.create(
            full_name='Selector Email Test',
            email='approved-selector@example.test',
            organization='Test Association',
            position='District selector',
            reason='Test email workflow',
        )
        self.model_admin = SelectorApplicationAdmin(SelectorApplication, admin.site)

    def make_admin_request(self):
        request = RequestFactory().post('/admin/portal/selectorapplication/')
        request.user = self.superuser
        SessionMiddleware(lambda current_request: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def test_approval_emails_code_and_does_not_show_or_store_plaintext(self):
        from django.core import mail

        request = self.make_admin_request()
        self.model_admin.approve_and_issue_invites(
            request,
            SelectorApplication.objects.filter(pk=self.application.pk),
        )
        invite = SelectorInvite.objects.get(application=self.application)
        self.application.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.application.email])
        self.assertIn('one-time selector code', mail.outbox[0].body.lower())
        self.assertIn('/register/?role=selector', mail.outbox[0].body)
        self.assertEqual(self.application.status, SelectorApplication.Status.APPROVED)
        self.assertNotIn(invite.code_hash, mail.outbox[0].body)
        self.assertNotIn('selector code for', str(list(messages.get_messages(request))).lower())

    def test_smtp_failure_leaves_application_pending_and_creates_no_invite(self):
        import smtplib

        request = self.make_admin_request()
        with patch('portal.admin.send_mail', side_effect=smtplib.SMTPException('simulated mail failure')):
            self.model_admin.approve_and_issue_invites(
                request,
                SelectorApplication.objects.filter(pk=self.application.pk),
            )
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, SelectorApplication.Status.PENDING)
        self.assertFalse(SelectorInvite.objects.filter(application=self.application).exists())


    @override_settings(EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD='', DEFAULT_FROM_EMAIL='')
    def test_unconfigured_email_does_not_approve_or_issue_a_code(self):
        request = self.make_admin_request()
        self.model_admin.approve_and_issue_invites(
            request,
            SelectorApplication.objects.filter(pk=self.application.pk),
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, SelectorApplication.Status.PENDING)
        self.assertFalse(SelectorInvite.objects.filter(application=self.application).exists())


class LoginIdentifierTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='login-player',
            email='login-player@example.com',
            password='Test-Only-Password-2026!',
        )
        AccountProfile.objects.create(user=self.user, role=AccountProfile.Role.PLAYER)
        Athlete.objects.create(
            user=self.user,
            full_name='Login Player',
            mobile_number='9876543210',
            email=self.user.email,
            district='Bhopal',
            sport_category='Hockey',
        )

    def test_homepage_shows_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sign in')
        self.assertContains(response, 'Email / Mobile / Username')
        self.assertContains(response, 'Create an account')
        self.assertContains(response, 'Forgot password?')
        self.assertNotContains(response, 'Ask Gemini')

    def test_authenticated_homepage_redirects_to_member_feed(self):
        self.client.force_login(self.user)
        response = self.client.get('/')
        self.assertRedirects(response, '/feed/')

    def test_anonymous_visitors_are_redirected_from_member_pages(self):
        for path in ('/feed/', '/players/', '/search/', '/chat/', '/profile/', '/records/', '/dashboard/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/?next=', response['Location'])

    def test_anonymous_gemini_use_requires_sign_in(self):
        response = self.client.post('/ai/question/', {'question': 'How do I train safely?'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/?next=', response['Location'])

    def test_email_mobile_and_username_can_sign_in(self):
        for identifier in (self.user.email, '9876543210', self.user.username):
            self.client.logout()
            response = self.client.post('/login/', {
                'username': identifier,
                'password': 'Test-Only-Password-2026!',
            })
            self.assertRedirects(response, '/feed/')

    def test_profile_photo_appears_in_signed_in_header(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.user.athlete_profile.passport_photo.save('login-avatar.png', png_upload(), save=True)
            self.client.force_login(self.user)
            response = self.client.get('/feed/')
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="account-avatar"')
            self.assertContains(response, 'secure-media/athlete_photos/login-avatar.png')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_password_reset_link_updates_account_password(self):
        from django.core import mail
        from urllib.parse import urlparse

        response = self.client.post('/password-reset/', {'email': self.user.email})
        self.assertRedirects(response, '/password-reset/done/')
        self.assertEqual(len(mail.outbox), 1)
        reset_url = next(line for line in mail.outbox[0].body.splitlines() if '/password-reset/confirm/' in line)
        reset_path = urlparse(reset_url).path

        response = self.client.get(reset_path, follow=True)
        self.assertContains(response, 'Choose a new password')
        response = self.client.post(response.request['PATH_INFO'], {
            'new_password1': 'NewSecurePassword2026!',
            'new_password2': 'NewSecurePassword2026!',
        })
        self.assertRedirects(response, '/password-reset/complete/')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewSecurePassword2026!'))


class GoogleSearchVisibilityTests(TestCase):
    def test_public_homepage_has_search_metadata_and_verification_tag(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='example-verification-token'):
            response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="robots" content="index,follow"')
        self.assertContains(response, 'rel="canonical" href="http://testserver/"')
        self.assertContains(response, 'name="google-site-verification" content="example-verification-token"')
        self.assertContains(response, 'Madhya Pradesh Athlete Community')
        self.assertContains(response, 'selectors can discover athletes')

    def test_signup_page_is_not_listed_in_search_results(self):
        response = self.client.get('/register/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="robots" content="noindex,follow"')

    def test_sitemap_lists_only_the_public_homepage(self):
        response = self.client.get('/sitemap.xml')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<loc>https://testserver/</loc>')
        self.assertNotContains(response, '/register/')
        self.assertNotContains(response, '/feed/')

    def test_robots_txt_points_crawlers_to_sitemap_and_away_from_private_pages(self):
        response = self.client.get('/robots.txt')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertContains(response, 'Sitemap: http://testserver/sitemap.xml')
        self.assertContains(response, 'Disallow: /admin/')
        self.assertContains(response, 'Disallow: /secure-media/')


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_HOST_USER='sender@gmail.com',
    EMAIL_HOST_PASSWORD='test-app-password',
    DEFAULT_FROM_EMAIL='sender@gmail.com',
)
class EmailOTPTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()

    def create_user(self, email='email-player@example.com', active=True):
        return User.objects.create_user(
            username='email-otp-player',
            email=email,
            password='Test-Only-Password-2026!',
            is_active=active,
        )

    def request_code(self, email='email-player@example.com'):
        return self.client.post('/otp/email/send/', {'email': email})

    def test_registered_user_can_sign_in_with_hashed_one_time_email_otp(self):
        user = self.create_user()
        sent = self.request_code()
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)
        challenge = EmailOTP.objects.get(email=user.email)
        self.assertNotEqual(challenge.code_hash, code)
        self.assertNotIn(code, sent.content.decode())

        response = self.client.post('/login/', {
            'auth_method': 'email_otp',
            'email': user.email,
            'email_otp': code,
        })

        self.assertRedirects(response, '/feed/')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.consumed_at)

    def test_unknown_and_registered_emails_get_same_send_response(self):
        self.create_user()
        registered = self.request_code()
        cache.clear()
        unknown = self.request_code('unknown@example.com')

        self.assertEqual(registered.status_code, unknown.status_code)
        self.assertEqual(registered.json()['message'], unknown.json()['message'])
        self.assertEqual(len(mail.outbox), 1)

    def test_email_otp_send_is_rate_limited(self):
        self.create_user()
        first = self.request_code()
        second = self.request_code()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(mail.outbox), 1)

    def test_expired_email_otp_cannot_sign_in(self):
        user = self.create_user()
        self.request_code()
        challenge = EmailOTP.objects.get(email=user.email)
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=('expires_at',))
        code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)

        response = self.client.post('/login/', {
            'auth_method': 'email_otp',
            'email': user.email,
            'email_otp': code,
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_email_otp_allows_at_most_five_incorrect_attempts(self):
        user = self.create_user()
        self.request_code()
        challenge = EmailOTP.objects.get(email=user.email)
        valid_code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)
        for _ in range(5):
            self.client.post('/login/', {
                'auth_method': 'email_otp',
                'email': user.email,
                'email_otp': '000000' if valid_code != '000000' else '000001',
            })

        self.assertEqual(EmailOTP.objects.get(pk=challenge.pk).attempts, 5)
        response = self.client.post('/login/', {
            'auth_method': 'email_otp',
            'email': user.email,
            'email_otp': valid_code,
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_email_delivery_failure_invalidates_the_challenge(self):
        self.create_user()
        with patch('portal.otp.send_mail', side_effect=smtplib.SMTPException('SMTP unavailable')):
            response = self.request_code()

        self.assertEqual(response.status_code, 503)
        self.assertIsNotNone(EmailOTP.objects.get().consumed_at)


@override_settings(DEBUG=True)
class MobileOTPTests(TestCase):
    def setUp(self):
        cache.clear()

    def request_code(self, mobile, purpose):
        with patch.dict(os.environ, {
            'TWILIO_ACCOUNT_SID': '',
            'TWILIO_AUTH_TOKEN': '',
            'TWILIO_FROM_NUMBER': '',
            'OTP_RETURN_CODE_IN_DEBUG': 'True',
        }):
            return self.client.post('/otp/send/', {'mobile_number': mobile, 'purpose': purpose})

    def create_player(self):
        user = User.objects.create_user(
            username='otp-player',
            email='otp-player@example.com',
            password='Test-Only-Password-2026!',
        )
        AccountProfile.objects.create(
            user=user,
            role=AccountProfile.Role.PLAYER,
            mobile='+919876543210',
            mobile_verified=True,
        )
        Athlete.objects.create(
            user=user,
            full_name='OTP Player',
            mobile_number='9876543210',
            email=user.email,
            district='Bhopal',
            sport_category='Hockey',
        )
        return user

    def test_player_signup_requires_and_consumes_hashed_mobile_otp(self):
        sent = self.request_code('9876543210', PhoneOTP.Purpose.SIGNUP)
        self.assertEqual(sent.status_code, 200)
        code = sent.json()['development_code']
        challenge = PhoneOTP.objects.get(mobile='+919876543210')
        self.assertNotEqual(challenge.code_hash, code)

        response = self.client.post('/register/', {
            'role': 'player',
            'username': 'new-otp-player',
            'password1': 'OtpSignupPassword2026!',
            'password2': 'OtpSignupPassword2026!',
            'full_name': 'New OTP Player',
            'mobile_number': '9876543210',
            'mobile_otp': code,
            'district': 'Bhopal',
            'sport_category': 'Hockey',
        })

        self.assertRedirects(response, '/feed/')
        athlete = Athlete.objects.get(mobile_number='9876543210')
        profile = athlete.user.account_profile
        self.assertTrue(profile.mobile_verified)
        self.assertEqual(profile.mobile, '+919876543210')
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.consumed_at)

    def test_verified_mobile_can_sign_in_with_one_time_otp(self):
        user = self.create_player()
        sent = self.request_code('9876543210', PhoneOTP.Purpose.LOGIN)
        self.assertEqual(sent.status_code, 200)
        response = self.client.post('/login/', {
            'auth_method': 'otp',
            'mobile_number': '9876543210',
            'mobile_otp': sent.json()['development_code'],
        })
        self.assertRedirects(response, '/feed/')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_invalid_signup_otp_does_not_create_account(self):
        self.request_code('9876543211', PhoneOTP.Purpose.SIGNUP)
        response = self.client.post('/register/', {
            'role': 'player',
            'username': 'bad-otp-player',
            'password1': 'OtpSignupPassword2026!',
            'password2': 'OtpSignupPassword2026!',
            'full_name': 'Bad OTP Player',
            'mobile_number': '9876543211',
            'mobile_otp': '000000',
            'district': 'Bhopal',
            'sport_category': 'Hockey',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Athlete.objects.filter(mobile_number='9876543211').exists())
        self.assertEqual(PhoneOTP.objects.get(mobile='+919876543211').attempts, 1)

    def test_otp_resend_is_rate_limited(self):
        first = self.request_code('9876543212', PhoneOTP.Purpose.SIGNUP)
        second = self.request_code('9876543212', PhoneOTP.Purpose.SIGNUP)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_production_sms_uses_twilio_and_never_returns_code(self):
        with (
            override_settings(DEBUG=False),
            patch.dict(os.environ, {
                'TWILIO_ACCOUNT_SID': 'AC-test',
                'TWILIO_AUTH_TOKEN': 'test-token',
                'TWILIO_FROM_NUMBER': '+15551234567',
            }),
            patch('portal.otp.requests.post') as send_sms,
        ):
            response = self.client.post('/otp/send/', {
                'mobile_number': '9876543210',
                'purpose': PhoneOTP.Purpose.SIGNUP,
            })

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['development_code'])
        send_sms.assert_called_once()
        self.assertIn('+919876543210', send_sms.call_args.kwargs['data']['To'])

    def test_feed_post_has_shareable_link(self):
        user = self.create_player()
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            post = AthletePost.objects.create(
                author=user,
                media=png_upload('share.png'),
                media_type=AthletePost.MediaType.PHOTO,
                caption='Training update',
            )
            self.client.force_login(user)
            response = self.client.get('/feed/')
        self.assertContains(response, f'data-share-url="http://testserver/feed/#post-{post.pk}"')
        self.assertContains(response, 'Share this post')


class AIReviewAndSearchTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_settings = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_settings.enable()
        self.addCleanup(self.media_settings.disable)
        self.player = User.objects.create_user(username='score-player', password='Test-Only-Password-2026!')
        self.selector = User.objects.create_user(username='score-selector', password='Test-Only-Password-2026!')
        AccountProfile.objects.create(user=self.player, role=AccountProfile.Role.PLAYER)
        AccountProfile.objects.create(user=self.selector, role=AccountProfile.Role.SELECTOR)
        self.athlete = Athlete.objects.create(
            user=self.player,
            full_name='Score Player',
            mobile_number='9345678901',
            district='Bhopal',
            sport_category='Cricket',
        )
        self.client.force_login(self.player)

    def test_media_screening_and_score_claim_do_not_create_verified_result(self):
        with patch('portal.views.analyze_uploaded_media', return_value={
            'status': 'likely_synthetic',
            'confidence': 76,
            'note': 'Some visual inconsistencies were observed.',
        }) as analyze:
            response = self.client.post('/feed/', {
                'media': png_upload('innings.png'),
                'caption': '10 balls 40 runs',
            })

        self.assertEqual(response.status_code, 302)
        post = AthletePost.objects.get(author=self.player)
        self.assertEqual(post.media_ai_status, 'likely_synthetic')
        self.assertEqual(post.media_ai_confidence, 76)
        analyze.assert_called_once()
        claim = AIPerformanceClaim.objects.get(source_post=post)
        self.assertEqual((claim.runs, claim.balls), (40, 10))
        self.assertTrue(claim.is_high_score_candidate)
        self.assertEqual(claim.status, AIPerformanceClaim.Status.PENDING)
        self.assertFalse(AthleteResult.objects.filter(athlete=self.athlete).exists())

    def test_selector_approval_adds_result_and_best_score(self):
        post = AthletePost.objects.create(
            author=self.player,
            media=png_upload('claim.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='10 balls 40 runs',
        )
        claim = AIPerformanceClaim.objects.create(
            athlete=self.athlete,
            source_post=post,
            runs=40,
            balls=10,
            is_high_score_candidate=True,
        )
        self.client.force_login(self.selector)
        response = self.client.post('/records/', {
            'action': 'review_ai_claim',
            'claim_id': claim.pk,
            'decision': 'approve',
        })
        self.assertRedirects(response, '/records/')
        result = AthleteResult.objects.get(athlete=self.athlete)
        self.assertEqual(result.result, '40 runs off 10 balls')
        self.assertEqual(result.high_score, '40 runs')
        self.assertEqual(result.entered_by, self.selector)
        claim.refresh_from_db()
        self.assertEqual(claim.status, AIPerformanceClaim.Status.APPROVED)

    def test_lower_score_claim_is_not_flagged_as_new_high(self):
        AthleteResult.objects.create(
            athlete=self.athlete,
            event_name='Previous innings',
            result='65 runs off 40 balls',
            high_score='65 runs',
            event_date='2026-01-01',
        )
        with patch('portal.views.analyze_uploaded_media', return_value={
            'status': 'uncertain',
            'confidence': None,
            'note': 'Visual screening is inconclusive.',
        }):
            response = self.client.post('/feed/', {
                'media': png_upload('lower-score.png'),
                'caption': '10 balls 40 runs',
            })
        self.assertEqual(response.status_code, 302)
        claim = AIPerformanceClaim.objects.get()
        self.assertFalse(claim.is_high_score_candidate)
        self.assertEqual(claim.status, AIPerformanceClaim.Status.PENDING)

    def test_selector_sees_pending_claim_review_controls(self):
        post = AthletePost.objects.create(
            author=self.player,
            media=png_upload('pending.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='12 balls 50 runs',
        )
        AIPerformanceClaim.objects.create(athlete=self.athlete, source_post=post, runs=50, balls=12)
        self.client.force_login(self.selector)
        response = self.client.get('/records/')
        self.assertContains(response, 'AI-detected score claims')
        self.assertContains(response, 'Approve')
        self.assertContains(response, 'Reject')

    def test_player_cannot_approve_claim(self):
        post = AthletePost.objects.create(
            author=self.player,
            media=png_upload('claim.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='10 balls 40 runs',
        )
        claim = AIPerformanceClaim.objects.create(athlete=self.athlete, source_post=post, runs=40, balls=10)
        response = self.client.post('/records/', {
            'action': 'review_ai_claim',
            'claim_id': claim.pk,
            'decision': 'approve',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AthleteResult.objects.exists())
        claim.refresh_from_db()
        self.assertEqual(claim.status, AIPerformanceClaim.Status.PENDING)

    def test_global_search_finds_players_and_public_post_captions(self):
        post = AthletePost.objects.create(
            author=self.player,
            media=png_upload('search.png'),
            media_type=AthletePost.MediaType.PHOTO,
            caption='Bhopal cricket practice',
        )
        response = self.client.get('/search/', {'q': 'Bhopal'})
        self.assertContains(response, 'Score Player')
        self.assertContains(response, 'Bhopal cricket practice')
        self.assertContains(response, f'#post-{post.pk}')
        post_view = self.client.get(f'/feed/?post_id={post.pk}')
        self.assertContains(post_view, 'Bhopal cricket practice')
        self.assertEqual(len(post_view.context['posts']), 1)


@override_settings(ADMIN_BOOTSTRAP_TOKEN='test-bootstrap-token-at-least-thirty-two-characters')
class AdminBootstrapTests(TestCase):
    def test_setup_page_requires_configured_token(self):
        with override_settings(ADMIN_BOOTSTRAP_TOKEN=''):
            response = self.client.get('/admin/initial-setup/')
        self.assertEqual(response.status_code, 404)

    def test_setup_page_rejects_short_token_configuration(self):
        with override_settings(ADMIN_BOOTSTRAP_TOKEN='too-short'):
            response = self.client.get('/admin/initial-setup/')
        self.assertEqual(response.status_code, 404)

    def test_invalid_token_does_not_create_administrator(self):
        response = self.client.post('/admin/initial-setup/', {
            'username': 'bootstrap-admin',
            'email': 'bootstrap@example.com',
            'password1': 'New-Unique-Admin-Password-2026!x',
            'password2': 'New-Unique-Admin-Password-2026!x',
            'setup_token': 'incorrect-token',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='bootstrap-admin').exists())
        self.assertIsNone(AdminBootstrapState.objects.get(pk=1).completed_at)

    def test_weak_password_is_rejected(self):
        response = self.client.post('/admin/initial-setup/', {
            'username': 'bootstrap-admin',
            'email': 'bootstrap@example.com',
            'password1': 'password',
            'password2': 'password',
            'setup_token': 'test-bootstrap-token-at-least-thirty-two-characters',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='bootstrap-admin').exists())
        self.assertIsNone(AdminBootstrapState.objects.get(pk=1).completed_at)

    def test_valid_token_creates_first_superuser_and_consumes_setup(self):
        User.objects.create_user(
            username='existing-player',
            email='admin@example.com',
            password='Existing-Player-Password-2026!',
        )
        response = self.client.post('/admin/initial-setup/', {
            'username': 'bootstrap-admin',
            'email': 'admin@example.com',
            'password1': 'New-Unique-Admin-Password-2026!x',
            'password2': 'New-Unique-Admin-Password-2026!x',
            'setup_token': 'test-bootstrap-token-at-least-thirty-two-characters',
        })
        self.assertRedirects(response, '/admin/login/')
        admin_user = User.objects.get(username='bootstrap-admin')
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.check_password('New-Unique-Admin-Password-2026!x'))
        self.assertIsNotNone(AdminBootstrapState.objects.get(pk=1).completed_at)
        self.assertEqual(self.client.get('/admin/initial-setup/').status_code, 404)

    def test_existing_superuser_disables_setup(self):
        User.objects.create_superuser(
            username='already-admin',
            email='already-admin@example.com',
            password='Existing-Admin-Password-2026!',
        )
        self.assertEqual(self.client.get('/admin/initial-setup/').status_code, 404)


class AdminUserPanelTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='site-admin',
            email='site-admin@example.com',
            password='Test-Only-Admin-Password-2026!',
        )
        self.player = User.objects.create_user(
            username='panel-player',
            email='panel-player@example.com',
            password='Test-Only-Player-Password-2026!',
        )
        AccountProfile.objects.create(
            user=self.player,
            role=AccountProfile.Role.PLAYER,
            mobile='+919123456780',
            mobile_verified=True,
        )

    def test_dashboard_requires_a_superuser(self):
        response = self.client.get('/admin/users/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

        self.client.force_login(self.player)
        response = self.client.get('/admin/users/')
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_search_and_view_accounts(self):
        self.client.force_login(self.admin)

        response = self.client.get('/admin/users/', {'q': '9123456780'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Registered accounts')
        self.assertContains(response, 'panel-player')
        self.assertContains(response, '+919123456780')
        self.assertNotContains(response, 'site-admin@example.com')

    def test_superuser_can_suspend_and_reactivate_player_accounts(self):
        self.client.force_login(self.admin)

        suspended = self.client.post(f'/admin/users/{self.player.pk}/toggle-active/')
        self.assertRedirects(suspended, '/admin/users/')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_active)

        reactivated = self.client.post(f'/admin/users/{self.player.pk}/toggle-active/')
        self.assertRedirects(reactivated, '/admin/users/')
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_active)

    def test_superuser_cannot_change_staff_or_administrator_accounts_here(self):
        staff = User.objects.create_user(
            username='panel-staff',
            password='Test-Only-Staff-Password-2026!',
            is_staff=True,
        )
        other_admin = User.objects.create_superuser(
            username='panel-admin-two',
            email='panel-admin-two@example.com',
            password='Test-Only-Admin-Password-2026!',
        )
        self.client.force_login(self.admin)

        for user in (staff, other_admin):
            response = self.client.post(f'/admin/users/{user.pk}/toggle-active/')
            self.assertRedirects(response, '/admin/users/')
            user.refresh_from_db()
            self.assertTrue(user.is_active)

    def test_account_toggle_rejects_get_requests(self):
        self.client.force_login(self.admin)
        response = self.client.get(f'/admin/users/{self.player.pk}/toggle-active/')
        self.assertEqual(response.status_code, 405)

    def test_superuser_can_create_otp_verified_player_account(self):
        mobile = '+919123456789'
        PhoneOTP.objects.create(
            mobile=mobile,
            purpose=PhoneOTP.Purpose.SIGNUP,
            code_hash=make_password('123456'),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.client.force_login(self.admin)

        response = self.client.post('/admin/users/create/', {
            'account_role': AccountProfile.Role.PLAYER,
            'full_name': 'New Admin Player',
            'username': 'new-admin-player',
            'email': 'new-admin-player@example.com',
            'mobile_number': mobile,
            'mobile_otp': '123456',
            'district': 'Bhopal',
            'sport_category': 'Athletics',
            'password1': 'Safe-Admin-Created-Password-2026!',
            'password2': 'Safe-Admin-Created-Password-2026!',
        })

        self.assertRedirects(response, '/admin/users/')
        user = User.objects.get(username='new-admin-player')
        self.assertTrue(user.check_password('Safe-Admin-Created-Password-2026!'))
        profile = AccountProfile.objects.get(user=user)
        self.assertEqual(profile.role, AccountProfile.Role.PLAYER)
        self.assertTrue(profile.mobile_verified)
        self.assertEqual(user.athlete_profile.full_name, 'New Admin Player')
        self.assertEqual(user.athlete_profile.mobile_number, '9123456789')
        self.assertIsNotNone(PhoneOTP.objects.get(mobile=mobile).consumed_at)

    def test_superuser_can_create_otp_verified_selector_account(self):
        mobile = '+919123456789'
        PhoneOTP.objects.create(
            mobile=mobile,
            purpose=PhoneOTP.Purpose.SIGNUP,
            code_hash=make_password('654321'),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.client.force_login(self.admin)

        response = self.client.post('/admin/users/create/', {
            'account_role': AccountProfile.Role.SELECTOR,
            'full_name': 'New Selector',
            'username': 'new-selector',
            'email': 'new-selector@example.com',
            'mobile_number': mobile,
            'mobile_otp': '654321',
            'district': '',
            'sport_category': '',
            'password1': 'Safe-Selector-Password-2026!',
            'password2': 'Safe-Selector-Password-2026!',
        })

        self.assertRedirects(response, '/admin/users/')
        user = User.objects.get(username='new-selector')
        self.assertEqual(user.account_profile.role, AccountProfile.Role.SELECTOR)
        self.assertTrue(user.account_profile.mobile_verified)
        self.assertFalse(Athlete.objects.filter(user=user).exists())

    def test_account_creation_requires_superuser_and_verified_mobile_otp(self):
        data = {
            'account_role': AccountProfile.Role.PLAYER,
            'full_name': 'Unauthorized Player',
            'username': 'unauthorized-player',
            'email': 'unauthorized@example.com',
            'mobile_number': '+919123456789',
            'mobile_otp': '123456',
            'district': 'Bhopal',
            'sport_category': 'Athletics',
            'password1': 'Safe-Admin-Created-Password-2026!',
            'password2': 'Safe-Admin-Created-Password-2026!',
        }
        response = self.client.post('/admin/users/create/', data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(username='unauthorized-player').exists())

        self.client.force_login(self.admin)
        response = self.client.post('/admin/users/create/', data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'invalid, expired, or has already been used')
        self.assertFalse(User.objects.filter(username='unauthorized-player').exists())

    def test_public_login_and_signup_show_admin_access_button(self):
        login_response = self.client.get('/login/')
        self.assertContains(login_response, 'href="/admin/users/"')
        signup_response = self.client.get('/register/')
        self.assertContains(signup_response, 'href="/admin/users/"')

    def test_django_admin_home_shows_account_actions_to_superusers(self):
        self.client.force_login(self.admin)

        response = self.client.get('/admin/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Create account')
        self.assertContains(response, 'href="/admin/users/create/"')
        self.assertContains(response, 'Manage accounts')

    def test_django_admin_home_hides_account_actions_from_non_superuser_staff(self):
        staff = User.objects.create_user(
            username='limited-admin-staff',
            password='Limited-Staff-Password-2026!',
            is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get('/admin/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Create account')
        self.assertNotContains(response, 'Manage accounts')

    def test_superuser_can_log_into_django_admin_with_email(self):
        response = self.client.post('/admin/login/', {
            'username': self.admin.email,
            'password': 'Test-Only-Admin-Password-2026!',
            'next': '/admin/',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/admin/')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)

    def test_django_admin_login_prompts_for_username_or_admin_email(self):
        response = self.client.get('/admin/login/')

        self.assertContains(response, 'Username or administrator email:')

    def test_non_staff_cannot_use_email_to_log_into_django_admin(self):
        response = self.client.post('/admin/login/', {
            'username': self.player.email,
            'password': 'Test-Only-Player-Password-2026!',
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_ambiguous_staff_email_cannot_log_into_django_admin(self):
        User.objects.create_superuser(
            username='second-site-admin',
            email=self.admin.email.upper(),
            password='Test-Only-Admin-Password-2026!',
        )

        response = self.client.post('/admin/login/', {
            'username': self.admin.email,
            'password': 'Test-Only-Admin-Password-2026!',
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
