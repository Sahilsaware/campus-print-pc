"""Prints random values for AGENT_KEY and SECRET_KEY. Paste them into .env files."""
import secrets

print('AGENT_KEY=' + secrets.token_urlsafe(32))
print('SECRET_KEY=' + secrets.token_hex(32))
