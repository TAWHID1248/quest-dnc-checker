from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agents'

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import post_save
        User = get_user_model()
        post_save.connect(_on_user_save, sender=User)


def _on_user_save(sender, instance, created, **kwargs):
    """Auto-generate first promo code when a new agent is created."""
    if created and instance.role == 'agent':
        from .utils import generate_next_promo_code
        generate_next_promo_code(instance)
