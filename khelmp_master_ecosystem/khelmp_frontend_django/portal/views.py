import hashlib
import logging
import re
from pathlib import Path
from datetime import date
from decimal import Decimal, InvalidOperation

from PIL import Image, UnidentifiedImageError
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.static import serve
from django.views.decorators.http import require_POST

from .ai import answer_sports_question, create_scout_insight
from .models import AccountProfile, AIPerformanceClaim, Athlete, AthletePost, AthleteResult, EmailOTP, FriendMessage, PhoneOTP, PlayerConnection, PostComment, PostLike, SelectorApplication, SelectorInvite
from .ai import analyze_uploaded_media, extract_cricket_score_claim
 
from .otp import OTPDeliveryError, OTPError, issue_email_otp, issue_phone_otp, local_mobile_number, normalize_mobile, verify_email_otp, verify_phone_otp

User = get_user_model()
logger = logging.getLogger(__name__)

MAX_POST_SIZE = 100 * 1024 * 1024
MAX_IMAGE_SIZE = 12 * 1024 * 1024
ALLOWED_POST_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
ALLOWED_POST_VIDEO_EXT = {'.mp4', '.mov', '.webm', '.avi', '.mkv'}


class ResilientPasswordResetForm(PasswordResetForm):
	def send_mail(self, *args, **kwargs):
		try:
			return super().send_mail(*args, **kwargs)
		except Exception:
			logger.exception('Password reset email could not be sent.')


def validated_media_type(uploaded_file, *, allow_video=True):
	if uploaded_file.size > MAX_POST_SIZE:
		return None, 'Media must be 100 MB or smaller.'
	extension = Path(uploaded_file.name).suffix.lower()
	if extension in ALLOWED_POST_IMAGE_EXT:
		if uploaded_file.size > MAX_IMAGE_SIZE:
			return None, 'Photos must be 12 MB or smaller.'
		try:
			with Image.open(uploaded_file) as image:
				image.verify()
				if image.format not in {'JPEG', 'PNG', 'WEBP'}:
					return None, 'Use a valid JPG, PNG or WEBP photo.'
		except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
			return None, 'The selected photo is not a valid image.'
		finally:
			uploaded_file.seek(0)
		return AthletePost.MediaType.PHOTO, ''
	if not allow_video or extension not in ALLOWED_POST_VIDEO_EXT:
		return None, 'Use a JPG, PNG, WEBP, MP4, MOV, WEBM, AVI or MKV file.'
	header = uploaded_file.read(16)
	uploaded_file.seek(0)
	if extension in {'.mp4', '.mov'} and (len(header) < 8 or header[4:8] != b'ftyp'):
		return None, 'The selected MP4/MOV file signature is invalid.'
	if extension in {'.webm', '.mkv'} and not header.startswith(b'\x1aE\xdf\xa3'):
		return None, 'The selected WEBM/MKV file signature is invalid.'
	if extension == '.avi' and not (header.startswith(b'RIFF') and header[8:12] == b'AVI '):
		return None, 'The selected AVI file signature is invalid.'
	return AthletePost.MediaType.VIDEO, ''


def optional_decimal(value, label, minimum, maximum):
	if not value:
		return None
	try:
		parsed = Decimal(value)
	except (InvalidOperation, TypeError):
		raise ValueError(f'{label} must be a number.') from None
	if not parsed.is_finite() or parsed < minimum or parsed > maximum:
		raise ValueError(f'{label} must be between {minimum} and {maximum}.')
	return parsed


def highest_recorded_cricket_score(athlete):
	recorded_values = AthleteResult.objects.filter(athlete=athlete).values_list('result', 'high_score')
	scores = [
		int(match.group(1))
		for values in recorded_values
		for value in values
		if (match := re.search(r'\b(\d{1,3})\b', value or ''))
	]
	return max(scores, default=0)


def account_role(user):
	if user.is_staff:
		return AccountProfile.Role.SELECTOR
	profile, _ = AccountProfile.objects.get_or_create(user=user)
	return profile.role


def login_user(request):
	if request.user.is_authenticated:
		return redirect('feed')
	if request.method == 'POST':
		if request.POST.get('auth_method') == 'email_otp':
			email = request.POST.get('email', '').strip().casefold()
			if email and verify_email_otp(email, request.POST.get('email_otp', '')):
				matching_users = User.objects.filter(email__iexact=email, is_active=True)
				user = matching_users.first() if matching_users.count() == 1 else None
				if user:
					login(request, user)
					account_role(user)
					return redirect('feed')
			messages.error(request, 'Email address or OTP is invalid, expired, or not linked to an active account.')
			return render(request, 'login.html', {'form': AuthenticationForm()})
		if request.POST.get('auth_method') == 'otp':
			mobile = request.POST.get('mobile_number', '')
			try:
				canonical_mobile = normalize_mobile(mobile)
			except OTPError:
				canonical_mobile = ''
			if canonical_mobile and verify_phone_otp(canonical_mobile, request.POST.get('mobile_otp', ''), PhoneOTP.Purpose.LOGIN):
				athlete = Athlete.objects.filter(mobile_number=local_mobile_number(canonical_mobile), user__isnull=False).select_related('user').first()
				profile = AccountProfile.objects.filter(mobile=canonical_mobile, mobile_verified=True).select_related('user').first()
				user = athlete.user if athlete else profile.user if profile else None
				if user and user.is_active:
					account_profile, _ = AccountProfile.objects.get_or_create(user=user)
					account_profile.mobile = canonical_mobile
					account_profile.mobile_verified = True
					account_profile.save(update_fields=('mobile', 'mobile_verified'))
					login(request, user)
					account_role(user)
					return redirect('feed')
			messages.error(request, 'Mobile number or OTP is invalid, expired, or not linked to an active account.')
			return render(request, 'login.html', {'form': AuthenticationForm()})
		form_data = request.POST.copy()
		identifier = form_data.get('username', '').strip()
		matched_user = None
		if '@' in identifier:
			matched_user = User.objects.filter(email__iexact=identifier).first()
		elif identifier.isdigit():
			matched_user = User.objects.filter(athlete_profile__mobile_number=identifier).first()
		if matched_user is not None:
			form_data['username'] = matched_user.get_username()
		form = AuthenticationForm(request, data=form_data)
		if form.is_valid():
			user = form.get_user()
			if user is not None:
				login(request, user)
				account_role(user)
				return redirect('feed')
	else:
		form = AuthenticationForm()
	return render(request, 'login.html', {'form': form})


@require_POST
def send_phone_otp(request):
	purpose = request.POST.get('purpose', '')
	try:
		mobile = normalize_mobile(request.POST.get('mobile_number', ''))
	except OTPError as error:
		return JsonResponse({'ok': False, 'message': str(error)}, status=400)

	local_mobile = local_mobile_number(mobile)
	if purpose == PhoneOTP.Purpose.SIGNUP:
		if Athlete.objects.filter(mobile_number=local_mobile).exists() or AccountProfile.objects.filter(mobile=mobile).exists():
			return JsonResponse({'ok': False, 'message': 'This mobile number is already linked to an account.'}, status=409)
	elif purpose == PhoneOTP.Purpose.LOGIN:
		account_exists = (
			Athlete.objects.filter(mobile_number=local_mobile, user__isnull=False, user__is_active=True).exists()
			or AccountProfile.objects.filter(mobile=mobile, mobile_verified=True, user__is_active=True).exists()
		)
		if not account_exists:
			return JsonResponse({'ok': True, 'message': 'If the number is registered, a login code will be sent.'})
	else:
		return JsonResponse({'ok': False, 'message': 'Choose a valid OTP purpose.'}, status=400)

	remote_address = request.META.get('REMOTE_ADDR', '')
	phone_key = 'phone-otp:' + hashlib.sha256(mobile.encode()).hexdigest()
	address_key = 'phone-otp-ip:' + hashlib.sha256(remote_address.encode()).hexdigest()
	if not cache.add(phone_key, True, timeout=60) or not cache.add(address_key, True, timeout=5):
		return JsonResponse({'ok': False, 'message': 'Please wait before requesting another OTP.'}, status=429)

	try:
		development_code = issue_phone_otp(mobile, purpose)
	except OTPDeliveryError as error:
		cache.delete(phone_key)
		cache.delete(address_key)
		return JsonResponse({'ok': False, 'message': str(error)}, status=503)
	return JsonResponse({
		'ok': True,
		'message': 'OTP sent. It expires in 5 minutes.',
		'development_code': development_code if settings.DEBUG else None,
	})


@require_POST
def send_email_otp(request):
	email = request.POST.get('email', '').strip().casefold()
	try:
		validate_email(email)
	except ValidationError:
		return JsonResponse({'ok': False, 'message': 'Enter a valid email address.'}, status=400)

	remote_address = request.META.get('REMOTE_ADDR', '')
	email_key = 'email-otp:' + hashlib.sha256(email.encode()).hexdigest()
	address_key = 'email-otp-ip:' + hashlib.sha256(remote_address.encode()).hexdigest()
	email_limited = not cache.add(email_key, True, timeout=60)
	address_limited = not cache.add(address_key, True, timeout=5)
	if email_limited or address_limited:
		if not email_limited:
			cache.delete(email_key)
		if not address_limited:
			cache.delete(address_key)
		return JsonResponse({'ok': False, 'message': 'Please wait before requesting another OTP.'}, status=429)

	matching_users = User.objects.filter(email__iexact=email, is_active=True)
	if matching_users.count() != 1:
		return JsonResponse({
			'ok': True,
			'message': 'If an active account exists for that email, a login code will be sent.',
		})

	try:
		issue_email_otp(email)
	except OTPDeliveryError as error:
		cache.delete(email_key)
		cache.delete(address_key)
		return JsonResponse({'ok': False, 'message': str(error)}, status=503)
	return JsonResponse({
		'ok': True,
		'message': 'If an active account exists for that email, a login code will be sent.',
	})


def register_account(request):
	if request.user.is_authenticated:
		return redirect('feed')
	role = request.POST.get('role', request.GET.get('role', AccountProfile.Role.PLAYER))
	if role not in AccountProfile.Role.values:
		role = AccountProfile.Role.PLAYER

	if request.method == 'POST':
		form = UserCreationForm(request.POST)
		full_name = request.POST.get('full_name', '').strip()
		mobile_number = request.POST.get('mobile_number', '').strip()
		canonical_mobile = ''
		local_mobile = ''
		district = request.POST.get('district', '').strip()
		sport_category = request.POST.get('sport_category', '').strip()
		invite_code = request.POST.get('invite_code', '')
		selector_email = request.POST.get('email', '').strip().lower()

		if role == AccountProfile.Role.PLAYER:
			if not full_name or not district or not sport_category:
				form.add_error(None, 'Player name, district and sport are required.')
			try:
				canonical_mobile = normalize_mobile(mobile_number)
				local_mobile = local_mobile_number(canonical_mobile)
			except OTPError as error:
				form.add_error(None, str(error))
			if local_mobile and Athlete.objects.filter(mobile_number=local_mobile).exists():
				form.add_error(None, 'This mobile number is already linked to an athlete profile.')
		else:
			try:
				canonical_mobile = normalize_mobile(mobile_number)
			except OTPError as error:
				form.add_error(None, str(error))
			if not selector_email:
				form.add_error(None, 'Use the same email address submitted with your selector application.')
			if not invite_code.strip():
				form.add_error(None, 'A selector invitation code is required.')
		if canonical_mobile and AccountProfile.objects.filter(mobile=canonical_mobile).exists():
			form.add_error(None, 'This mobile number is already linked to an account.')

		if form.is_valid():
			try:
				with transaction.atomic():
					invite = None
					if role == AccountProfile.Role.SELECTOR:
						code_hash = hashlib.sha256(invite_code.strip().encode('utf-8')).hexdigest()
						invite = SelectorInvite.objects.select_for_update().filter(
							code_hash=code_hash,
							email=selector_email,
							redeemed_at__isnull=True,
							revoked_at__isnull=True,
							expires_at__gt=timezone.now(),
						).first()
						if invite is None:
							form.add_error(None, 'This selector code is invalid, expired, already used, or belongs to another email.')
					if not form.errors and not verify_phone_otp(
						canonical_mobile,
						request.POST.get('mobile_otp', ''),
						PhoneOTP.Purpose.SIGNUP,
					):
						form.add_error(None, 'Verify this mobile number with a current OTP before creating your account.')

					if not form.errors:
						user = form.save(commit=False)
						if role == AccountProfile.Role.SELECTOR:
							user.email = selector_email
						user.save()
						AccountProfile.objects.create(
							user=user,
							role=role,
							mobile=canonical_mobile,
							mobile_verified=True,
						)
						if role == AccountProfile.Role.PLAYER:
							Athlete.objects.create(
								user=user,
								full_name=full_name,
								mobile_number=local_mobile,
								district=district,
								sport_category=sport_category,
							)
						else:
							invite.redeemed_at = timezone.now()
							invite.redeemed_by = user
							invite.save(update_fields=('redeemed_at', 'redeemed_by'))
						login(request, user)
						messages.success(request, 'Your account is ready. Welcome to the KhelMP community!')
						return redirect('feed')
			except IntegrityError:
				form.add_error(None, 'This username or selector invitation has already been used.')
	else:
		form = UserCreationForm()

	return render(request, 'register.html', {'form': form, 'role': role})


def apply_as_selector(request):
	if request.user.is_authenticated:
		return redirect('feed')
	if request.method == 'POST':
		full_name = request.POST.get('full_name', '').strip()
		email = request.POST.get('email', '').strip().lower()
		organization = request.POST.get('organization', '').strip()
		position = request.POST.get('position', '').strip()
		official_website = request.POST.get('official_website', '').strip()
		reason = request.POST.get('reason', '').strip()
		if not all((full_name, email, organization, position, reason)):
			messages.error(request, 'Complete the required application fields.')
		elif SelectorApplication.objects.filter(email__iexact=email, status=SelectorApplication.Status.PENDING).exists():
			messages.info(request, 'An application for this email is already awaiting review.')
			return redirect('login')
		else:
			SelectorApplication.objects.create(
				full_name=full_name[:160],
				email=email,
				organization=organization[:180],
				position=position[:120],
				official_website=official_website,
				reason=reason[:1200],
			)
			messages.success(request, 'Application received. An administrator will verify it before issuing a selector code.')
			return redirect('login')
	return render(request, 'selector_application.html')


def logout_user(request):
	logout(request)
	return redirect('login')


@login_required(login_url='login')
def feed(request):
	role = account_role(request.user)
	athlete = getattr(request.user, 'athlete_profile', None)
	if request.method == 'POST':
		if role != AccountProfile.Role.PLAYER or athlete is None:
			messages.error(request, 'Only player accounts can publish feed posts.')
			return redirect('feed')

		media = request.FILES.get('media')
		caption = request.POST.get('caption', '').strip()
		if media is None:
			messages.error(request, 'Choose a photo or video to publish.')
			return redirect('feed')

		media_type, validation_error = validated_media_type(media)
		if validation_error:
			messages.error(request, validation_error)
			return redirect('feed')
        
		post = AthletePost.objects.create(author=request.user, media=media, media_type=media_type, caption=caption)
		mime_types = {
			'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
			'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
			'.avi': 'video/x-msvideo', '.mkv': 'video/x-matroska',
		}
		mime_type = mime_types.get(Path(media.name).suffix.lower(), '')
		try:
			screening = analyze_uploaded_media(post.media.path, mime_type)
		except Exception:
			logger.exception('Post media screening could not start.')
			screening = {
				'status': 'unavailable',
				'confidence': None,
				'note': 'AI screening could not complete; manual review is recommended.',
			}
		post.media_ai_status = screening['status']
		post.media_ai_confidence = screening['confidence']
		post.media_ai_note = screening['note']
		post.save(update_fields=('media_ai_status', 'media_ai_confidence', 'media_ai_note'))

		claim_created = False
		if athlete.sport_category.strip().casefold() == 'cricket':
			current_best = highest_recorded_cricket_score(athlete)
			score_claim = extract_cricket_score_claim(caption, current_best)
			if score_claim:
				AIPerformanceClaim.objects.create(
					athlete=athlete,
					source_post=post,
					runs=score_claim['runs'],
					balls=score_claim['balls'],
					is_high_score_candidate=score_claim['is_high_score_candidate'],
				)
				claim_created = True

		if claim_created:
			messages.success(request, 'Your update is live. Its cricket score was sent to selectors as an unverified claim.')
		else:
			messages.success(request, 'Your update has been published.')
		return redirect('feed')

	user_like = PostLike.objects.filter(post=OuterRef('pk'), user=request.user)
	recent_comments = PostComment.objects.select_related('user').order_by('-created_at')[:3]
	posts = (
		AthletePost.objects.select_related('author', 'author__athlete_profile', 'ai_performance_claim')
		.annotate(like_count=Count('likes', distinct=True), comment_count=Count('comments', distinct=True))
		.annotate(has_liked=Exists(user_like))
		.prefetch_related(Prefetch('comments', queryset=recent_comments, to_attr='recent_comments'))
		.order_by('-created_at', '-pk')
	)
	post_id = request.GET.get('post_id', '')
	if post_id.isdigit():
		posts = posts.filter(pk=post_id)
	page = Paginator(posts, 20).get_page(request.GET.get('page'))
	return render(request, 'feed.html', {
		'posts': page.object_list,
		'page': page,
		'role': role,
		'athlete': athlete,
		'recent_results': athlete.results.all()[:3] if athlete else [],
		'can_post': role == AccountProfile.Role.PLAYER and athlete is not None,
		'ai_athletes': Athlete.objects.all().order_by('full_name') if role == AccountProfile.Role.SELECTOR else [],
	})


@login_required(login_url='login')
def like_post(request, post_id):
	if request.method != 'POST':
		return redirect('feed')
	post = get_object_or_404(AthletePost, pk=post_id)
	like = PostLike.objects.filter(post=post, user=request.user)
	if like.exists():
		like.delete()
	else:
		PostLike.objects.get_or_create(post=post, user=request.user)
	return redirect(request.POST.get('return_to') if url_has_allowed_host_and_scheme(request.POST.get('return_to', ''), allowed_hosts={request.get_host()}) else 'feed')


@login_required(login_url='login')
def comment_post(request, post_id):
	if request.method != 'POST':
		return redirect('feed')
	post = get_object_or_404(AthletePost, pk=post_id)
	text = request.POST.get('text', '').strip()
	if not text:
		messages.error(request, 'Write a comment before posting.')
		return redirect('feed')
	if len(text) > 500:
		messages.error(request, 'Comments are limited to 500 characters.')
		return redirect('feed')
	key = f'post-comment:{request.user.pk}'
	if not cache.add(key, True, timeout=4):
		messages.error(request, 'Please wait a few seconds before commenting again.')
		return redirect('feed')
	PostComment.objects.create(post=post, user=request.user, text=text)
	return redirect(request.POST.get('return_to') if url_has_allowed_host_and_scheme(request.POST.get('return_to', ''), allowed_hosts={request.get_host()}) else 'feed')


@login_required(login_url='login')
def delete_post(request, post_id):
	if request.method != 'POST':
		return redirect('feed')
	post = get_object_or_404(AthletePost, pk=post_id)
	if post.author_id != request.user.id and not request.user.is_superuser:
		messages.error(request, 'You can remove only your own post.')
		return redirect('feed')
	post.delete()
	messages.success(request, 'Your post was removed.')
	return redirect('feed')


@login_required(login_url='login')
def people(request):
	role = account_role(request.user)
	selector_mode = role == AccountProfile.Role.SELECTOR
	if request.method == 'POST':
		if selector_mode:
			messages.error(request, 'Selector accounts can search players but cannot send friend requests.')
			return redirect('people')
		action = request.POST.get('action', '')
		connection_id = request.POST.get('connection_id', '')
		query_text = request.POST.get('q', '').strip()[:80]
		if action == 'send':
			try:
				target_id = int(request.POST.get('target_id', ''))
			except ValueError:
				messages.error(request, 'Choose a valid player.')
				return redirect('people')
			target = get_object_or_404(
				User.objects.filter(account_profile__role=AccountProfile.Role.PLAYER, athlete_profile__isnull=False),
				pk=target_id,
			)
			if target.pk == request.user.pk:
				messages.error(request, 'You cannot send a friend request to yourself.')
				return redirect('people')
			first, second = sorted((request.user, target), key=lambda user: user.pk)
			connection, created = PlayerConnection.objects.get_or_create(
				player_one=first,
				player_two=second,
				defaults={'requested_by': request.user, 'status': PlayerConnection.Status.PENDING},
			)
			if created:
				messages.success(request, f'Friend request sent to {target.athlete_profile.full_name}.')
			elif connection.status == PlayerConnection.Status.ACCEPTED:
				messages.info(request, 'You are already friends with this player.')
			elif connection.status == PlayerConnection.Status.PENDING:
				messages.info(request, 'A friend request already exists for this player.')
			else:
				connection.status = PlayerConnection.Status.PENDING
				connection.requested_by = request.user
				connection.updated_at = timezone.now()
				connection.save(update_fields=('status', 'requested_by', 'updated_at'))
				messages.success(request, 'Friend request sent again.')
		elif action in {'accept', 'decline', 'remove'}:
			try:
				connection = PlayerConnection.objects.get(pk=connection_id)
			except (PlayerConnection.DoesNotExist, ValueError, TypeError):
				messages.error(request, 'That friend request is no longer available.')
				return redirect('people')
			is_participant = request.user.pk in (connection.player_one_id, connection.player_two_id)
			if not is_participant:
				return redirect('people')
			if action in {'accept', 'decline'}:
				if connection.status != PlayerConnection.Status.PENDING or connection.requested_by_id == request.user.pk:
					messages.error(request, 'Only an incoming pending request can be updated.')
					return redirect('people')
				connection.status = PlayerConnection.Status.ACCEPTED if action == 'accept' else PlayerConnection.Status.DECLINED
				connection.updated_at = timezone.now()
				connection.save(update_fields=('status', 'updated_at'))
				messages.success(request, 'Friend request accepted.' if action == 'accept' else 'Friend request declined.')
			elif connection.status == PlayerConnection.Status.ACCEPTED:
				connection.delete()
				messages.success(request, 'Player removed from your friends.')
		return redirect(f'/players/?q={query_text}' if query_text else 'people')

	search_query = request.GET.get('q', '').strip()[:80]
	players = User.objects.filter(
		account_profile__role=AccountProfile.Role.PLAYER,
		athlete_profile__isnull=False,
	).select_related('athlete_profile').order_by('athlete_profile__full_name')
	if search_query:
		players = players.filter(
			Q(athlete_profile__full_name__icontains=search_query)
			| Q(athlete_profile__sport_category__icontains=search_query)
			| Q(athlete_profile__district__icontains=search_query)
		)
	page = Paginator(players, 24).get_page(request.GET.get('page'))
	connections = PlayerConnection.objects.filter(
		Q(player_one=request.user) | Q(player_two=request.user)
	).select_related('player_one__athlete_profile', 'player_two__athlete_profile', 'requested_by')
	connection_by_user = {}
	for connection in connections:
		other_user = connection.player_two if connection.player_one_id == request.user.pk else connection.player_one
		if connection.status == PlayerConnection.Status.PENDING and connection.requested_by_id == request.user.pk:
			state = 'sent'
		elif connection.status == PlayerConnection.Status.PENDING:
			state = 'incoming'
		elif connection.status == PlayerConnection.Status.ACCEPTED:
			state = 'friend'
		else:
			state = 'none'
		connection_by_user[other_user.pk] = (state, connection)
	for player in page.object_list:
		player.connection_state, player.connection = connection_by_user.get(player.pk, ('none', None))
	incoming = [
		{'connection': connection, 'person': connection.requested_by}
		for connection in connections
		if connection.status == PlayerConnection.Status.PENDING and connection.requested_by_id != request.user.pk
	]
	friends = [
		{
			'connection': connection,
			'person': connection.player_two if connection.player_one_id == request.user.pk else connection.player_one,
		}
		for connection in connections if connection.status == PlayerConnection.Status.ACCEPTED
	]
	return render(request, 'people.html', {
		'people_page': page,
		'players': page.object_list,
		'search_query': search_query,
		'selector_mode': selector_mode,
		'incoming_requests': incoming if not selector_mode else [],
		'friends': friends if not selector_mode else [],
		'role': role,
		'ai_athletes': Athlete.objects.all().order_by('full_name') if selector_mode else [],
	})


@login_required(login_url='login')
def global_search(request):
	query = request.GET.get('q', '').strip()[:100]
	players = User.objects.none()
	posts = AthletePost.objects.none()
	results = AthleteResult.objects.none()
	if query:
		term = Q(athlete_profile__full_name__icontains=query) | Q(athlete_profile__sport_category__icontains=query) | Q(athlete_profile__district__icontains=query)
		players = User.objects.filter(
			account_profile__role=AccountProfile.Role.PLAYER,
			athlete_profile__isnull=False,
		).filter(term).select_related('athlete_profile')[:12]
		posts = AthletePost.objects.filter(
			Q(caption__icontains=query) | Q(author__athlete_profile__full_name__icontains=query)
		).select_related('author', 'author__athlete_profile').order_by('-created_at', '-pk')[:12]
		results = AthleteResult.objects.filter(
			Q(event_name__icontains=query) | Q(result__icontains=query) | Q(high_score__icontains=query)
		).select_related('athlete')
		if account_role(request.user) != AccountProfile.Role.SELECTOR:
			results = results.filter(athlete__user=request.user)
		results = results.order_by('-event_date', '-pk')[:12]
	return render(request, 'global_search.html', {
		'query': query,
		'players': players,
		'posts': posts,
		'results': results,
		'role': account_role(request.user),
	})


def _chat_friend(request, friend_id):
	if account_role(request.user) != AccountProfile.Role.PLAYER:
		raise Http404
	friend = get_object_or_404(
		User.objects.select_related('athlete_profile'),
		pk=friend_id,
		account_profile__role=AccountProfile.Role.PLAYER,
		athlete_profile__isnull=False,
	)
	if not PlayerConnection.objects.filter(
		Q(player_one=request.user, player_two=friend) | Q(player_one=friend, player_two=request.user),
		status=PlayerConnection.Status.ACCEPTED,
	).exists():
		raise Http404
	return friend


@login_required(login_url='login')
def chat_inbox(request):
	if account_role(request.user) != AccountProfile.Role.PLAYER:
		return redirect('people')
	connections = PlayerConnection.objects.filter(
		Q(player_one=request.user) | Q(player_two=request.user),
		status=PlayerConnection.Status.ACCEPTED,
	).select_related('player_one__athlete_profile', 'player_two__athlete_profile')
	conversations = []
	for connection in connections:
		friend = connection.player_two if connection.player_one_id == request.user.pk else connection.player_one
		thread_messages = FriendMessage.objects.filter(
			Q(sender=request.user, recipient=friend) | Q(sender=friend, recipient=request.user)
		)
		conversations.append({
			'friend': friend,
			'last_message': thread_messages.select_related('sender').order_by('-created_at', '-pk').first(),
			'unread_count': thread_messages.filter(sender=friend, recipient=request.user, read_at__isnull=True).count(),
			'updated_at': connection.updated_at,
		})
	conversations.sort(
		key=lambda item: item['last_message'].created_at if item['last_message'] else item['updated_at'],
		reverse=True,
	)
	return render(request, 'chat_inbox.html', {'conversations': conversations, 'role': AccountProfile.Role.PLAYER})


@login_required(login_url='login')
def chat_thread(request, friend_id):
	friend = _chat_friend(request, friend_id)
	if request.method == 'POST':
		text = request.POST.get('text', '').strip()
		if not text or len(text) > 2000:
			return JsonResponse({'ok': False, 'message': 'Messages must contain 1 to 2,000 characters.'}, status=400)
		throttle_key = f'friend-chat:{request.user.pk}:{friend.pk}'
		if not cache.add(throttle_key, True, timeout=1):
			return JsonResponse({'ok': False, 'message': 'Please wait a moment before sending another message.'}, status=429)
		message = FriendMessage.objects.create(sender=request.user, recipient=friend, text=text)
		return JsonResponse({
			'ok': True,
			'message': {
				'id': message.pk,
				'text': message.text,
				'own': True,
				'time': timezone.localtime(message.created_at).strftime('%b %d, %H:%M'),
			},
		}, status=201)

	FriendMessage.objects.filter(sender=friend, recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
	thread = list(
		FriendMessage.objects.filter(
			Q(sender=request.user, recipient=friend) | Q(sender=friend, recipient=request.user)
		).select_related('sender').order_by('-created_at', '-pk')[:100]
	)
	thread.reverse()
	return render(request, 'chat_thread.html', {
		'friend': friend,
		'messages_list': thread,
		'last_message_id': thread[-1].pk if thread else 0,
	})


@login_required(login_url='login')
def chat_updates(request, friend_id):
	friend = _chat_friend(request, friend_id)
	try:
		after_id = max(0, int(request.GET.get('after', '0')))
	except (TypeError, ValueError):
		after_id = 0
	new_messages = list(
		FriendMessage.objects.filter(
			Q(sender=request.user, recipient=friend) | Q(sender=friend, recipient=request.user),
			pk__gt=after_id,
		).select_related('sender').order_by('created_at', 'pk')[:100]
	)
	incoming_ids = [message.pk for message in new_messages if message.sender_id == friend.pk and message.read_at is None]
	if incoming_ids:
		FriendMessage.objects.filter(pk__in=incoming_ids, recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
	return JsonResponse({
		'messages': [
			{
				'id': message.pk,
				'text': message.text,
				'own': message.sender_id == request.user.pk,
				'time': timezone.localtime(message.created_at).strftime('%b %d, %H:%M'),
			}
			for message in new_messages
		],
	})


@login_required(login_url='login')
def player_profile(request):
	if account_role(request.user) != AccountProfile.Role.PLAYER:
		return redirect('records')
	athlete = get_object_or_404(Athlete, user=request.user)
	if request.method == 'POST':
		full_name = request.POST.get('full_name', '').strip()
		district = request.POST.get('district', '').strip()
		sport_category = request.POST.get('sport_category', '').strip()
		if not full_name or not district or not sport_category:
			messages.error(request, 'Display name, district and sport are required.')
			return redirect('player-profile')
		try:
			height_cm = optional_decimal(request.POST.get('height_cm', '').strip(), 'Height', Decimal('50'), Decimal('250'))
			weight_kg = optional_decimal(request.POST.get('weight_kg', '').strip(), 'Weight', Decimal('10'), Decimal('300'))
			years_of_experience = int(request.POST.get('years_of_experience') or 0)
			if not 0 <= years_of_experience <= 80:
				raise ValueError('Experience must be between 0 and 80 years.')
		except ValueError as error:
			messages.error(request, str(error))
			return redirect('player-profile')
		old_photo_name = athlete.passport_photo.name if athlete.passport_photo else ''
		photo = request.FILES.get('passport_photo')
		if photo:
			media_type, validation_error = validated_media_type(photo, allow_video=False)
			if validation_error:
				messages.error(request, validation_error)
				return redirect('player-profile')
			athlete.passport_photo = photo
		athlete.full_name = full_name[:160]
		athlete.district = district[:100]
		athlete.age_group = request.POST.get('age_group', '').strip()[:50]
		athlete.height_cm = height_cm
		athlete.weight_kg = weight_kg
		athlete.years_of_experience = years_of_experience
		athlete.sport_category = sport_category[:80]
		athlete.specialization = request.POST.get('specialization', '').strip()[:120]
		athlete.highest_achievement = request.POST.get('highest_achievement', '').strip()
		athlete.save()
		if old_photo_name and old_photo_name != athlete.passport_photo.name:
			athlete.passport_photo.storage.delete(old_photo_name)
		messages.success(request, 'Your player profile has been updated.')
		return redirect('player-profile')
	return render(request, 'player_profile.html', {'athlete': athlete})


@login_required(login_url='login')
def records(request):
	role = account_role(request.user)
	if role == AccountProfile.Role.SELECTOR:
		athletes = Athlete.objects.all().order_by('full_name')
		results = AthleteResult.objects.select_related('athlete', 'entered_by')
		if request.method == 'POST':
			if request.POST.get('action') == 'review_ai_claim':
				try:
					claim_id = int(request.POST.get('claim_id', ''))
				except ValueError:
					messages.error(request, 'Choose a valid score claim.')
					return redirect('records')
				with transaction.atomic():
					claim = AIPerformanceClaim.objects.select_for_update().select_related('athlete', 'source_post').filter(
						pk=claim_id,
						status=AIPerformanceClaim.Status.PENDING,
					).first()
					if claim is None:
						messages.error(request, 'That score claim is no longer awaiting review.')
						return redirect('records')
					decision = request.POST.get('decision')
					if decision == 'approve':
						current_best = highest_recorded_cricket_score(claim.athlete)
						is_new_high = claim.runs > current_best
						AthleteResult.objects.create(
							athlete=claim.athlete,
							event_name='Player-reported cricket innings',
							result=f'{claim.runs} runs off {claim.balls} balls',
							high_score=f'{claim.runs} runs' if is_new_high else '',
							event_date=timezone.localdate(claim.source_post.created_at),
							notes='Selector-approved claim from a player feed post.',
							entered_by=request.user,
						)
						claim.status = AIPerformanceClaim.Status.APPROVED
						feedback = 'Score claim approved and added to verified athlete records.'
					elif decision == 'reject':
						claim.status = AIPerformanceClaim.Status.REJECTED
						feedback = 'Score claim rejected.'
					else:
						messages.error(request, 'Choose approve or reject.')
						return redirect('records')
					claim.reviewed_at = timezone.now()
					claim.reviewed_by = request.user
					claim.save(update_fields=('status', 'reviewed_at', 'reviewed_by'))
				messages.success(request, feedback)
				return redirect('records')
			try:
				athlete = Athlete.objects.get(pk=request.POST.get('athlete_id'))
			except (Athlete.DoesNotExist, TypeError, ValueError):
				messages.error(request, 'Choose a valid athlete record.')
				return redirect('records')
			event_name = request.POST.get('event_name', '').strip()
			result_text = request.POST.get('result', '').strip()
			event_date = request.POST.get('event_date', '').strip()
			if not event_name or not result_text:
				messages.error(request, 'Event and result are required.')
				return redirect('records')
			try:
				parsed_date = date.fromisoformat(event_date)
			except ValueError:
				messages.error(request, 'Enter a valid event date.')
				return redirect('records')
			AthleteResult.objects.create(
				athlete=athlete,
				event_name=event_name[:160],
				result=result_text[:160],
				high_score=request.POST.get('high_score', '').strip()[:80],
				event_date=parsed_date,
				location=request.POST.get('location', '').strip()[:120],
				notes=request.POST.get('notes', '').strip(),
				entered_by=request.user,
			)
			messages.success(request, 'Athlete result added to selector records.')
			return redirect('records')
		pending_claims = AIPerformanceClaim.objects.filter(
			status=AIPerformanceClaim.Status.PENDING,
		).select_related('athlete', 'source_post', 'source_post__author').order_by('created_at')
	else:
		athlete = get_object_or_404(Athlete, user=request.user)
		athletes = [athlete]
		results = AthleteResult.objects.filter(athlete=athlete).select_related('athlete')
		pending_claims = []
	return render(request, 'records.html', {
		'athletes': athletes,
		'results': results,
		'is_selector': role == AccountProfile.Role.SELECTOR,
		'pending_claims': pending_claims,
		'role': role,
		'ai_athletes': Athlete.objects.all().order_by('full_name') if role == AccountProfile.Role.SELECTOR else [],
	})


@login_required(login_url='login')
def athlete_record(request, athlete_id):
	role = account_role(request.user)
	if role == AccountProfile.Role.SELECTOR:
		athlete = get_object_or_404(Athlete, pk=athlete_id)
	elif role == AccountProfile.Role.PLAYER:
		athlete = get_object_or_404(Athlete, pk=athlete_id, user=request.user)
	else:
		return redirect('feed')
	return render(request, 'athlete_detail.html', {
		'athlete': athlete,
		'results': athlete.results.select_related('entered_by').all(),
		'is_selector': role == AccountProfile.Role.SELECTOR,
		'role': role,
		'ai_athletes': [athlete],
	})


@login_required(login_url='login')
def gemini_insight(request):
	if request.method != 'POST':
		return redirect('feed')
	role = account_role(request.user)
	athlete_id = request.POST.get('athlete_id')
	if role == AccountProfile.Role.SELECTOR:
		if not athlete_id:
			messages.error(request, 'Select an athlete before requesting a Gemini insight.')
			return redirect('feed')
		try:
			athlete = Athlete.objects.get(pk=athlete_id)
		except (Athlete.DoesNotExist, TypeError, ValueError):
			messages.error(request, 'Choose a valid athlete for the Gemini insight.')
			return redirect('feed')
	else:
		athlete = get_object_or_404(Athlete, user=request.user)

	results = athlete.results.all()[:20]
	posts = AthletePost.objects.none()
	if athlete.user_id:
		posts = AthletePost.objects.filter(author=athlete.user).order_by('-created_at')[:10]
	context = (
		f'Athlete: {athlete.full_name}; sport: {athlete.sport_category}; '
		f'specialization: {athlete.specialization}; district: {athlete.district}; '
		f'profile milestones: {athlete.highest_achievement}; '
		f'recorded results: {"; ".join(f"{item.event_date}: {item.event_name} - {item.result}; high score {item.high_score}" for item in results)}; '
		f'recent post captions: {"; ".join(item.caption for item in posts if item.caption)}'
	)
	request.session['gemini_insight'] = create_scout_insight(context)
	request.session['gemini_insight_athlete'] = athlete.full_name
	return_to = request.POST.get('return_to', '')
	if url_has_allowed_host_and_scheme(return_to, allowed_hosts={request.get_host()}):
		return redirect(return_to)
	return redirect('athlete-record', athlete_id=athlete.pk)


def gemini_question(request):
	if request.method != 'POST':
		return redirect('home')
	question = request.POST.get('question', '').strip()
	if not question:
		messages.error(request, 'Enter a sports question for Gemini.')
		return_to = request.POST.get('return_to', '')
		if url_has_allowed_host_and_scheme(return_to, allowed_hosts={request.get_host()}):
			return redirect(return_to)
		return redirect('home')
	if len(question) > 500:
		messages.error(request, 'Keep your Gemini question within 500 characters.')
		return redirect('home')

	request.session.save()
	rate_identity = f'{request.META.get("REMOTE_ADDR", "")}:{request.session.session_key}'
	rate_key = 'gemini-question:' + hashlib.sha256(rate_identity.encode()).hexdigest()
	if not cache.add(rate_key, True, timeout=15):
		messages.error(request, 'Please wait 15 seconds before asking Gemini again.')
		return_to = request.POST.get('return_to', '')
		if url_has_allowed_host_and_scheme(return_to, allowed_hosts={request.get_host()}):
			return redirect(return_to)
		return redirect('home')

	request.session['gemini_question'] = question
	request.session['gemini_answer'] = answer_sports_question(question)
	return_to = request.POST.get('return_to', '')
	if url_has_allowed_host_and_scheme(return_to, allowed_hosts={request.get_host()}):
		return redirect(return_to)
	return redirect('home')


@login_required(login_url='login')
def protected_media(request, path):
	role = account_role(request.user)
	if path.startswith('feed/'):
		if not AthletePost.objects.filter(media=path).exists():
			raise Http404
	elif path.startswith('athlete_photos/'):
		if not Athlete.objects.filter(passport_photo=path).exists():
			raise Http404
	elif path.startswith(('athlete_docs/', 'trial_videos/')):
		athlete = Athlete.objects.filter(id_proof_pdf=path).first() or Athlete.objects.filter(video_file=path).first()
		if athlete is None or (role != AccountProfile.Role.SELECTOR and athlete.user_id != request.user.id):
			raise Http404
	else:
		raise Http404
	return serve(request, path, document_root=settings.MEDIA_ROOT)


@login_required(login_url='login')
def dashboard(request):
	if account_role(request.user) != AccountProfile.Role.SELECTOR:
		messages.error(request, 'The selector dashboard is available to selector accounts only.')
		return redirect('feed')
	athletes = Athlete.objects.all().order_by('-registered_at')
	search_query = request.GET.get('search_name')
	filter_sport = request.GET.get('filter_sport')

	if search_query:
		athletes = athletes.filter(full_name__icontains=search_query)
	if filter_sport:
		athletes = athletes.filter(sport_category=filter_sport)

	return render(request, 'dashboard.html', {
		'athletes': athletes,
		'role': AccountProfile.Role.SELECTOR,
		'ai_athletes': Athlete.objects.all().order_by('full_name'),
	})
