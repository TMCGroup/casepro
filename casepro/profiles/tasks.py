from __future__ import unicode_literals

from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@shared_task
def send_notifications():
    from .models import Notification
    Notification.send_all()
