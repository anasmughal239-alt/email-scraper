# Always-on deployment target (Railway or any Docker host). This is a
# separate deployment path from Streamlit Community Cloud (which uses
# packages.txt instead) — a Dockerfile gives full, explicit control over
# the base OS and package names, avoiding the mixed-Debian-repo package-
# naming surprises we hit getting Playwright working on Streamlit Cloud.
FROM python:3.11-slim

# System libraries needed for headless Chromium (Playwright's browser
# binary is still downloaded at runtime on first use — see
# PlaywrightFetcher's self-install logic in email_scraper.py — this only
# provides the shared libraries it links against).
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation libfontconfig1 libxcb1 libxdamage1 libxext6 libxfixes3 \
    libxkbcommon0 libxrandr2 libcairo2 libdbus-1-3 libdrm2 libfreetype6 \
    libnspr4 libpango-1.0-0 libwayland-client0 libx11-6 xvfb \
    fonts-freefont-ttf libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libxcomposite1 libgbm1 libgl1-mesa-dri libglx-mesa0 libnss3 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY email_scraper.py app.py ./

# Railway assigns the listen port via $PORT at runtime; must bind 0.0.0.0
# (not localhost) to be reachable. Shell form so $PORT actually expands.
CMD streamlit run app.py --server.port $PORT --server.address 0.0.0.0
