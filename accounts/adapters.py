from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    def get_login_redirect_url(self, request):
        return '/dashboard/'

    def get_logout_redirect_url(self, request):
        return '/accounts/login/'


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        extra = sociallogin.account.extra_data
        if not user.name:
            user.name = extra.get('name', extra.get('given_name', ''))
            user.save(update_fields=['name'])
        return user

    def get_connect_redirect_url(self, request, socialaccount):
        return '/dashboard/'
