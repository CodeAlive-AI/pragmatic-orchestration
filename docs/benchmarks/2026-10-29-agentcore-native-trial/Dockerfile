# Trial-only bootstrap over the separately qualified ARM64 image. No credentials.
FROM porch-agentcore:qualification-20261029
USER 0:0
RUN mkdir -p /mnt/work && chown 1000:1000 /mnt/work \
    && rm -rf /work && ln -s /mnt/work /work
COPY entrypoint.sh /usr/local/bin/porch-trial-entrypoint
COPY fleet.json /opt/porch/trial-fleet.json
# Build with --build-context porch=<this revision's pragmatic-orchestration skill>.
COPY --from=porch /scripts/lib/agentcore/server.py /opt/porch/scripts/lib/agentcore/server.py
RUN chmod 755 /usr/local/bin/porch-trial-entrypoint
ENV PORCH_FLEET_CONFIG=/opt/porch/trial-fleet.json
# Do not resolve a symlink into a runtime-mounted volume as the OCI startup cwd.
WORKDIR /
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/porch-trial-entrypoint"]
