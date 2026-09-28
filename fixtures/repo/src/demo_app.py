"""Demo application module seeded with FAKE tokens for scanner tests."""

# Classic GitHub personal access token shape (synthetic random body).
GITHUB_TOKEN = "ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka"

# Slack bot token shape — fabricated digits/letters, not a real workspace.
SLACK_BOT_TOKEN = "xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"

# Ordinary configuration without secrets: placeholders must NOT be flagged.
PASSWORD = "changeme"
DATABASE_URL = "postgres://localhost:5432/demo"

# Short low-entropy password: below the length/entropy gates, not a candidate.
LOGIN_PASSWORD = "hunter2"
