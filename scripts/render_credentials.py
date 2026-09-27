#!/usr/bin/env python3
"""Reads .env and prints the n8n credentials JSON for `n8n import:credentials`.

The IDs match the ones referenced in n8n/workflows/*.json. n8n encrypts the
`data` objects with N8N_ENCRYPTION_KEY on import.
"""
import json
import sys


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def main():
    e = load_env(sys.argv[1] if len(sys.argv) > 1 else ".env")
    g = lambda k: e.get(k, "")
    creds = [
        {
            "id": "shCredPostgres01", "name": "Social Hub DB", "type": "postgres",
            "data": {"host": "postgres", "port": 5432, "database": g("POSTGRES_DB"),
                     "user": g("POSTGRES_USER"), "password": g("POSTGRES_PASSWORD"),
                     "ssl": "disable", "allowUnauthorizedCerts": False, "maxConnections": 10},
        },
        {
            "id": "shCredTelegram01", "name": "Telegram Bot", "type": "telegramApi",
            "data": {"accessToken": g("TELEGRAM_BOT_TOKEN"),
                     "baseUrl": g("TELEGRAM_API_BASE_URL") or "https://api.telegram.org"},
        },
        {
            "id": "shCredAnthropic1", "name": "Anthropic API Key", "type": "httpHeaderAuth",
            "data": {"name": "x-api-key", "value": g("ANTHROPIC_API_KEY")},
        },
        {
            "id": "shCredFbPage0001", "name": "Facebook Page Token", "type": "httpQueryAuth",
            "data": {"name": "access_token", "value": g("FACEBOOK_PAGE_ACCESS_TOKEN")},
        },
        {
            "id": "shCredXBearer001", "name": "X Bearer Token", "type": "httpHeaderAuth",
            "data": {"name": "Authorization", "value": "Bearer " + g("TWITTER_BEARER_TOKEN")},
        },
        {
            # OAuth 1.0a user context (needed to post, upload media and retweet).
            # The access token/secret from the X developer portal are injected as
            # already-authorized token data, so no browser OAuth dance is needed.
            "id": "shCredXOAuth1001", "name": "X OAuth1 User Context", "type": "oAuth1Api",
            "data": {
                "authUrl": "https://api.x.com/oauth/authorize",
                "accessTokenUrl": "https://api.x.com/oauth/access_token",
                "requestTokenUrl": "https://api.x.com/oauth/request_token",
                "consumerKey": g("TWITTER_API_KEY"),
                "consumerSecret": g("TWITTER_API_SECRET"),
                "signatureMethod": "HMAC-SHA1",
                "oauthTokenData": {
                    "oauth_token": g("TWITTER_ACCESS_TOKEN"),
                    "oauth_token_secret": g("TWITTER_ACCESS_SECRET"),
                },
            },
        },
    ]
    json.dump(creds, sys.stdout)


if __name__ == "__main__":
    main()
