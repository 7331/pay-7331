# Production Postgres: the root compose file's postgres:18-alpine plus WAL-G, which archives every WAL segment to R2
# within about a minute (PLAN.md section 2; deploy/docker-compose.production.yml sets archive_command). Same base image
# as local and e2e runs, so the existing data volume and its musl collation stay as they are.
#
# WAL-G is built here from its tagged source: its release binaries are Ubuntu glibc builds
# (https://github.com/wal-g/wal-g/releases, wal-g-pg-<ubuntu>-amd64) and Alpine packages none (checked 2026-10-12).
# `-tags libsodium` adds WALG_LIBSODIUM_KEY sealing, which needs libsodium at run time
# (https://github.com/wal-g/wal-g/blob/master/docs/README.md, "Building from Source"; Makefile `pg_build`, whose first
# line exports GOEXPERIMENT=jsonv2: the code imports encoding/json/v2).
FROM golang:1.25-alpine AS wal_g_build
# The tag and the commit it points at (git ls-remote, 2026-10-12): a moved tag fails the build instead of building other
# code. Go modules are checked against go.sum.
ARG WAL_G_VERSION=v3.0.9
ARG WAL_G_COMMIT=3e493188db28335fb93a45bd680c6ff8fc3028c1
RUN apk add --no-cache build-base git libsodium-dev
RUN git clone --quiet --depth 1 --branch "$WAL_G_VERSION" https://github.com/wal-g/wal-g /src/wal-g && \
    test "$(git -C /src/wal-g rev-parse HEAD)" = "$WAL_G_COMMIT"
WORKDIR /src/wal-g/main/pg
RUN --mount=type=cache,target=/root/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=1 GOEXPERIMENT=jsonv2 go build -mod=mod -tags libsodium -trimpath \
      -ldflags "-s -w -X github.com/wal-g/wal-g/cmd/pg.walgVersion=$WAL_G_VERSION" -o /usr/local/bin/wal-g .

FROM postgres:18-alpine
RUN apk add --no-cache libsodium
COPY --from=wal_g_build /usr/local/bin/wal-g /usr/local/bin/wal-g
