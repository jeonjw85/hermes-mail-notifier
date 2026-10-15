FROM python:3.14-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# Install the exact host source and its Telegram extra at build time, never in
# the network-disabled smoke containers. Keep its source tree like the CI gate.
ADD https://github.com/NousResearch/hermes-agent/archive/d5aa9d7adf308ca466a118206c201254cf3ca977.tar.gz /opt/hermes.tar.gz
RUN mkdir /opt/hermes && tar -xzf /opt/hermes.tar.gz -C /opt/hermes --strip-components=1 \
    && rm /opt/hermes.tar.gz \
    && python -m pip install --no-cache-dir -e '/opt/hermes[telegram]'

WORKDIR /opt/plugin
COPY pyproject.toml README.md LICENSE __init__.py plugin.yaml ./
COPY mail_notifier/ ./mail_notifier/
COPY tests/ ./tests/
COPY web/ ./web/
RUN python -m pip install --no-cache-dir -e '.[dev]' \
    && python -m pip check \
    && python -c 'import cryptography, telegram; from gateway.run import GatewayRunner; from plugins.platforms.telegram.adapter import TelegramAdapter'

ENTRYPOINT ["python", "-m", "tests.container.smoke"]
