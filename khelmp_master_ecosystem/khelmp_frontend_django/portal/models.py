from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import timedelta


def selector_invite_expiry():
    return timezone.now() + timedelta(days=7)


class Athlete(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='athlete_profile',
        null=True,
        blank=True,
    )
    full_name = models.CharField(max_length=160)
    mobile_number = models.CharField(max_length=10, unique=True)
    email = models.EmailField(blank=True)
    district = models.CharField(max_length=100)
    age_group = models.CharField(max_length=50, blank=True)
    height_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    years_of_experience = models.PositiveSmallIntegerField(default=0)
    sport_category = models.CharField(max_length=80)
    specialization = models.CharField(max_length=120, blank=True)
    highest_achievement = models.TextField(blank=True)
    passport_photo = models.ImageField(upload_to='athlete_photos/', blank=True, null=True)
    id_proof_pdf = models.FileField(upload_to='athlete_docs/', blank=True, null=True)
    video_file = models.FileField(upload_to='trial_videos/', blank=True, null=True)
    registered_at = models.DateTimeField(default=timezone.now, editable=False)

    @property
    def registration_number(self):
        if not self.pk:
            return ''
        return f'KHELMP/{self.registered_at.year}/{self.pk:05d}'

    def __str__(self):
        return f'{self.registration_number} - {self.full_name}'


class AccountProfile(models.Model):
    class Role(models.TextChoices):
        PLAYER = 'player', 'Player'
        SELECTOR = 'selector', 'Selector'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='account_profile')
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.PLAYER)
    mobile = models.CharField(max_length=16, unique=True, null=True, blank=True)
    mobile_verified = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.user.username} ({self.role})'


class PhoneOTP(models.Model):
    class Purpose(models.TextChoices):
        SIGNUP = 'signup', 'Signup'
        LOGIN = 'login', 'Login'

    mobile = models.CharField(max_length=16, db_index=True)
    purpose = models.CharField(max_length=6, choices=Purpose.choices)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=('mobile', 'purpose', 'created_at'), name='otp_mobile_purpose_created')]

    def __str__(self):
        return f'{self.mobile} · {self.purpose} · {self.created_at:%Y-%m-%d %H:%M}'


class SelectorApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    full_name = models.CharField(max_length=160)
    email = models.EmailField(db_index=True)
    organization = models.CharField(max_length=180)
    position = models.CharField(max_length=120)
    official_website = models.URLField(blank=True)
    reason = models.TextField(max_length=1200)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_selector_applications',
    )

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.full_name} · {self.organization} · {self.status}'


class SelectorInvite(models.Model):
    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=64, unique=True)
    application = models.ForeignKey(
        SelectorApplication,
        on_delete=models.PROTECT,
        related_name='invites',
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(default=selector_invite_expiry)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='redeemed_selector_invites',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='issued_selector_invites',
    )

    class Meta:
        ordering = ['-created_at']

    @property
    def is_usable(self):
        return (
            self.redeemed_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )

    def __str__(self):
        return f'{self.email} · expires {self.expires_at:%Y-%m-%d}'


class AthletePost(models.Model):
    class MediaType(models.TextChoices):
        PHOTO = 'photo', 'Photo'
        VIDEO = 'video', 'Video'

    class MediaAIStatus(models.TextChoices):
        NOT_REVIEWED = 'not_reviewed', 'Not screened'
        LIKELY_SYNTHETIC = 'likely_synthetic', 'Possible synthetic media'
        LIKELY_CAMERA = 'likely_camera', 'No strong synthetic cues detected'
        UNCERTAIN = 'uncertain', 'Uncertain'
        UNAVAILABLE = 'unavailable', 'Not screened'

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='athlete_posts')
    media = models.FileField(upload_to='feed/')
    media_type = models.CharField(max_length=8, choices=MediaType.choices)
    caption = models.CharField(max_length=500, blank=True)
    media_ai_status = models.CharField(max_length=24, choices=MediaAIStatus.choices, default=MediaAIStatus.NOT_REVIEWED)
    media_ai_confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    media_ai_note = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.author.username} · {self.media_type} · {self.created_at:%Y-%m-%d}'


class PostLike(models.Model):
    post = models.ForeignKey(AthletePost, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_likes')
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('post', 'user'), name='unique_user_post_like')]
        ordering = ['created_at']

    def __str__(self):
        return f'{self.user.username} likes post {self.post.pk}'


class PostComment(models.Model):
    post = models.ForeignKey(AthletePost, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_comments')
    text = models.CharField(max_length=500)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.user.username} commented on post {self.post.pk}'


class PlayerConnection(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Friends'
        DECLINED = 'declined', 'Declined'

    player_one = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='connections_as_player_one',
    )
    player_two = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='connections_as_player_two',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_player_connections',
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('player_one', 'player_two'), name='unique_player_connection_pair'),
            models.CheckConstraint(condition=models.Q(player_one__lt=models.F('player_two')), name='ordered_player_connection_pair'),
        ]
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.player_one.username} ↔ {self.player_two.username} · {self.status}'


class FriendMessage(models.Model):
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_friend_messages',
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_friend_messages',
    )
    text = models.CharField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'pk']
        indexes = [
            models.Index(fields=('sender', 'recipient', 'created_at'), name='friend_msg_sender_recipient'),
            models.Index(fields=('recipient', 'read_at'), name='friend_msg_unread_lookup'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(sender=models.F('recipient')),
                name='friend_message_not_self',
            ),
        ]

    def __str__(self):
        return f'{self.sender.username} → {self.recipient.username} · {self.created_at:%Y-%m-%d %H:%M}'


class AthleteResult(models.Model):
    athlete = models.ForeignKey(Athlete, on_delete=models.CASCADE, related_name='results')
    event_name = models.CharField(max_length=160)
    result = models.CharField(max_length=160)
    high_score = models.CharField(max_length=80, blank=True)
    event_date = models.DateField()
    location = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='entered_athlete_results')
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-event_date', '-created_at']

    def __str__(self):
        return f'{self.athlete.full_name} · {self.event_name} · {self.event_date}'


class AIPerformanceClaim(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending selector review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    athlete = models.ForeignKey(Athlete, on_delete=models.CASCADE, related_name='ai_performance_claims')
    source_post = models.OneToOneField(AthletePost, on_delete=models.CASCADE, related_name='ai_performance_claim')
    runs = models.PositiveSmallIntegerField()
    balls = models.PositiveSmallIntegerField()
    is_high_score_candidate = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_ai_performance_claims',
    )

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(runs__lte=500), name='ai_claim_runs_max_500'),
            models.CheckConstraint(condition=models.Q(balls__gt=0, balls__lte=600), name='ai_claim_balls_range'),
        ]

    def __str__(self):
        return f'{self.athlete.full_name} · {self.runs} off {self.balls} · {self.status}'