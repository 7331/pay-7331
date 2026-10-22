# pay-7331's Monero scanner sidecar (PLAN.md section 14). Build context: xmr-scanner/.
# Build on Debian trixie and run on distroless cc-debian13 (glibc and libgcc only, no shell, no package manager),
# the same Debian release on both sides so the binary's glibc matches.
FROM rust:1-slim-trixie AS build
WORKDIR /build
# Dependencies first, so a source change does not refetch and rebuild every crate.
COPY Cargo.toml Cargo.lock rustfmt.toml ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && cargo build --release --locked && rm -rf src
COPY src ./src
RUN touch src/main.rs && cargo build --release --locked

FROM gcr.io/distroless/cc-debian13:nonroot
COPY --from=build /build/target/release/xmr-scanner /usr/local/bin/xmr-scanner
USER nonroot
EXPOSE 8750
ENTRYPOINT ["/usr/local/bin/xmr-scanner"]
