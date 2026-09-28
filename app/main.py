from app.api.routes import create_app
from app.config import Settings

app = create_app(Settings.from_env())
