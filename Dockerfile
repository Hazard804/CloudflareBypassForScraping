FROM python:3.12-slim AS brave
ENV DEBIAN_FRONTEND=noninteractive
# builder only: the final image gets Brave plus just the shared libs it loads, not the full GTK/X apt closure
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
        https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
        > /etc/apt/sources.list.d/brave-browser-release.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends brave-browser fonts-liberation
# collect only the shared libs Brave links against, plus NSS modules it dlopens at runtime
RUN mkdir -p /out/lib && find /opt/brave.com/brave/locales -type f ! -name 'en-US.pak' -delete \
    && for bin in /opt/brave.com/brave/brave /opt/brave.com/brave/chrome_crashpad_handler /opt/brave.com/brave/*.so; do \
         ldd "$bin" 2>/dev/null | awk '/=> \//{print $3}'; done | sort -u > /tmp/libs \
    && (ls /usr/lib/*-linux-gnu/libsoftokn3.so /usr/lib/*-linux-gnu/libfreebl*.so /usr/lib/*-linux-gnu/libnssckbi.so \
          /usr/lib/*-linux-gnu/nss/*.so 2>/dev/null >> /tmp/libs || true) \
    && grep -v -e '/libc\.so' -e '/libm\.so' -e 'ld-linux' -e '/libpthread' -e '/libdl\.' -e '/librt\.' /tmp/libs \
       | xargs -n1 realpath | sort -u | xargs -I{} cp -L --parents {} /out/lib/ \
    && cp -a --parents /etc/fonts /usr/share/fonts/truetype/liberation /out/lib/

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=brave /out/lib/ /
COPY --from=brave /opt/brave.com/brave /opt/brave.com/brave
RUN ldconfig && useradd --create-home --uid 1000 app
WORKDIR /app
ARG INSTALL_TEST_DEPS=0
COPY server_requirements.txt .
COPY tests/test-requirements.txt tests/
RUN pip install -r server_requirements.txt \
    && if [ "$INSTALL_TEST_DEPS" = "1" ]; then pip install -r tests/test-requirements.txt; \
       else pip uninstall -y pip setuptools wheel; fi \
    && find /usr/local/lib/python3.12 -name '__pycache__' -prune -exec rm -rf {} +
COPY cf_bypasser cf_bypasser
COPY server.py .
# the cookie cache file is written next to the app
RUN chown -R app:app /app
ENV CF_BROWSER_PATH=/opt/brave.com/brave/brave CF_HEADLESS=true CF_BROWSER_NO_SANDBOX=true CF_BROWSERS=3
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(b'total' not in urllib.request.urlopen('http://127.0.0.1:8000/browsers', timeout=4).read())"

# args pass through: docker run IMAGE --browsers 5 --port 9000
ENTRYPOINT ["python", "server.py"]
