# aura-redis staging image — C data plane only (aura_redis_server).
# Not a Redis drop-in. Optional policy_agent: compose profile "agent"
# (ghcr.io/cybrid-systems/dev — same as CI/demos).
#
#   docker build -f deploy/Dockerfile -t aura-redis:staging .
#   docker compose up -d --build

ARG BUILD_IMAGE=ghcr.io/cybrid-systems/dev:v1.0.7
FROM ${BUILD_IMAGE} AS build
WORKDIR /src
COPY native/ native/
COPY scripts/build-native.sh scripts/build-native.sh
# Fresh build dir — never reuse a host-native/build CMakeCache.
RUN rm -rf native/build \
 && chmod +x scripts/build-native.sh \
 && ./scripts/build-native.sh

# Slim runtime (bookworm). If apt mirrors flake, rebuild with
#   --target build  and run from the build stage, or pin a local mirror.
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libssl3 python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 6379 --home /data --shell /usr/sbin/nologin aura
WORKDIR /data
COPY --from=build /src/native/build/aura_redis_server /usr/local/bin/aura_redis_server
COPY --from=build /src/native/build/libaura_redis_core.so /usr/local/lib/libaura_redis_core.so
COPY scripts/healthcheck.sh /usr/local/bin/aura-redis-healthcheck
COPY deploy/staging/aura-redis.conf.example /etc/aura-redis/aura-redis.conf.example
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN ldconfig \
    && chmod +x /usr/local/bin/aura-redis-healthcheck \
                   /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown -R aura:aura /data
ENV AURA_REDIS_DENY_PLUGIN=1 \
    AURA_REDIS_CONFIG=/data/aura-redis.conf \
    AURA_REDIS_DIR=/data \
    AURA_REDIS_DBFILENAME=dump.rdb \
    LD_LIBRARY_PATH=/usr/local/lib
USER aura
EXPOSE 6379
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["aura_redis_server", "--port", "6379", "--bind", "0.0.0.0", \
     "--evict", "lru", "--config", "/data/aura-redis.conf", \
     "--dir", "/data", "--dbfilename", "dump.rdb"]
