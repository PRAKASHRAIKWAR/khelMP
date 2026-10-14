from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model
from django.core.validators import RegexValidator

from .models import AccountProfile, Athlete
from .otp import OTPError, local_mobile_number, normalize_mobile


User = get_user_model()


class AdminAccountCreationForm(UserCreationForm):
    account_role = forms.ChoiceField(
        choices=AccountProfile.Role.choices,
        label='Account type',
    )
    full_name = forms.CharField(max_length=150, label='Full name')
    email = forms.EmailField(max_length=254)
    mobile_number = forms.CharField(max_length=20, label='Mobile number')
    mobile_otp = forms.CharField(
        max_length=6,
        label='Mobile OTP',
        validators=[RegexValidator(r'^\d{6}$', 'Enter the 6-digit OTP.')],
    )
    district = forms.CharField(max_length=100, required=False)
    sport_category = forms.CharField(max_length=80, required=False, label='Primary sport')

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username',)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().casefold()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account already uses this email address.')
        return email

    def clean_mobile_number(self):
        try:
            mobile = normalize_mobile(self.cleaned_data['mobile_number'])
        except OTPError as error:
            raise forms.ValidationError(str(error)) from error
        local_number = local_mobile_number(mobile)
        if AccountProfile.objects.filter(mobile=mobile).exists() or Athlete.objects.filter(
            mobile_number=local_number
        ).exists():
            raise forms.ValidationError('This mobile number is already linked to an account.')
        return mobile

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('account_role') == AccountProfile.Role.PLAYER:
            if not cleaned_data.get('district'):
                self.add_error('district', 'District is required for a player account.')
            if not cleaned_data.get('sport_category'):
                self.add_error('sport_category', 'Primary sport is required for a player account.')
        return cleaned_data
