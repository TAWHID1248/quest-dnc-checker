web: gunicorn quest_dnc.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120
worker: celery -A quest_dnc worker --loglevel=info --concurrency=2
beat: celery -A quest_dnc beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
