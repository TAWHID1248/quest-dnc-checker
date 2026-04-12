import os
import sys

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'quest_dnc.settings')

from quest_dnc.wsgi import application

# Vercel expects a callable named `app`
app = application
