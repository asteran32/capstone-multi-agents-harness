"""Create local credentials once, without overwriting an existing .env."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
path = root / '.env'
if path.exists():
    raise SystemExit('.env already exists; keeping it unchanged.')
content = (root / '.env.example').read_text()
for name in ['APP_PASSWORD', 'POSTGRES_PASSWORD', 'SANDBOX_TOKEN']:
    content = content.replace(name + '=\n', name + '=' + secrets.token_hex(18) + '\n')
with path.open('x') as f:
    f.write(content)
path.chmod(0o600)
print('Created .env. Use APP_USERNAME and APP_PASSWORD from that file to sign in.')
