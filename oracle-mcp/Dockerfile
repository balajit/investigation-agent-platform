# Oracle SQLcl MCP image — rebased on Oracle's official distribution
# Pinned by digest for reproducible builds.
FROM container-registry.oracle.com/database/sqlcl@sha256:63ac902122bb83209865889fa5d1fa9eec4d327d9b10c449d7cbb4f16e13da66

ENV HOME=/home/sqlcl \
    JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"

RUN useradd \
      --create-home \
      --home-dir /home/sqlcl \
      --shell /bin/bash \
      sqlcl \
    && mkdir -p /home/sqlcl/.dbtools \
    && chown -R sqlcl:sqlcl /home/sqlcl

# Pre-declare persistent dbtools volume to ensure non-root write access
VOLUME ["/home/sqlcl/.dbtools"]

COPY entrypoint.sh /entrypoint.sh

RUN chmod 0755 /entrypoint.sh \
    && chown sqlcl:sqlcl /entrypoint.sh

USER sqlcl

WORKDIR /home/sqlcl

ENTRYPOINT ["/entrypoint.sh"]
CMD ["-mcp"]
