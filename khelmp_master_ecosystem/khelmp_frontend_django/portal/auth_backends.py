from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class StaffEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        identifier = username or kwargs.get(user_model.USERNAME_FIELD)
        if isinstance(identifier, str) and '@' in identifier:
            staff_users = user_model._default_manager.filter(
                email__iexact=identifier.strip(),
                is_staff=True,
            )
            if staff_users.count() != 1:
                return None
            username = staff_users.get().get_username()
            kwargs.pop(user_model.USERNAME_FIELD, None)
        return super().authenticate(request, username=username, password=password, **kwargs)
