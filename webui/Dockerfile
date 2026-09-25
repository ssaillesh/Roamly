FROM caddy:2-alpine

COPY index.html /srv/index.html
COPY auth.html /srv/auth.html
COPY roamly.html /srv/roamly.html
COPY config.js /srv/config.js
COPY Caddyfile /etc/caddy/Caddyfile

# Caddy reads $PORT from the env (Railway injects it); see Caddyfile.
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
