import logging

from django.db import IntegrityError
from django.dispatch import receiver
from registration.signals import user_activated

logger = logging.getLogger(__name__)


@receiver(user_activated)
def handle_activate(sender, user, **kwargs):
    from .lib import save_init_license

    try:
        save_init_license(user)
    except IntegrityError:
        logger.warning("user %s already has an init license", user.id, exc_info=True)
