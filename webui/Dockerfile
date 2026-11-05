FROM caddy:2-alpine

COPY index.html /srv/index.html
COPY auth.html /srv/auth.html
COPY rova.html /srv/rova.html
COPY n.html /srv/n.html
COPY itinerary.js /srv/itinerary.js
COPY itinerary.css /srv/itinerary.css
COPY profile.html /srv/profile.html
COPY admin.html /srv/admin.html
COPY submit-event.html /srv/submit-event.html
COPY terms.html /srv/terms.html
COPY privacy.html /srv/privacy.html
COPY seatgeek-logo.svg /srv/seatgeek-logo.svg
COPY brand /srv/brand
COPY config.js /srv/config.js
COPY rova.css /srv/rova.css
COPY hero-toronto.jpg /srv/hero-toronto.jpg
COPY auth-santorini.jpg /srv/auth-santorini.jpg
COPY Caddyfile /etc/caddy/Caddyfile

# Caddy reads $PORT from the env (Railway injects it); see Caddyfile.
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
