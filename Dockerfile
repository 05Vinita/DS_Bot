# FROM python:3.11-slim

# ENV DEBIAN_FRONTEND=noninteractive

# RUN apt-get update && apt-get install -y \
#     chromium \
#     chromium-driver \
#     && rm -rf /var/lib/apt/lists/*

# WORKDIR /app

# COPY . .

# RUN pip install --no-cache-dir -r requirements.txt

# CMD ["python", "main.py"]
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# chromium        -> the browser Selenium drives
# chromium-driver -> matching chromedriver, kept in sync by apt (avoids the
#                     version-mismatch crashes that webdriver-manager can
#                     cause when it fetches a driver built for Google Chrome
#                     instead of Chromium)
# fonts-liberation, libnss3, etc. -> Chromium's actual runtime dependencies;
#                     without these it fails to launch even though it's
#                     "installed", with cryptic errors from Selenium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Tell gmaps_scraper.py exactly where Chromium and its driver live, instead
# of guessing at /usr/bin -- apt installs to different paths on some base
# images, so this keeps the two in sync with wherever this image put them.
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

# gunicorn instead of Flask's own dev server: the dev server is
# single-threaded and not meant for production, and its warning about that
# was showing up in the logs alongside the crashes.
# --timeout 300: gunicorn kills a worker that's silent for too long: a
# scraping run stays well under this now that /run itself replies instantly
# and the actual work happens on a background thread (see main.py).
# --workers 1: Selenium/Chromium is memory-heavy: more workers means more
# Chromium instances fighting over the same RAM limit.
CMD gunicorn main:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1
