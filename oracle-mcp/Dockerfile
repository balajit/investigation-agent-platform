# Oracle SQLcl MCP image — rebased on Oracle's official distribution
# (the vendored docker/dependencies/sqlcl-latest.zip is an incomplete bundle
# and is NOT used). Pinned by digest for reproducible builds; refresh the
# digest deliberately, never to :latest blindly.
FROM container-registry.oracle.com/database/sqlcl@sha256:63ac902122bb83209865889fa5d1fa9eec4d327d9b10c449d7cbb4f16e13da66

ENV HOME=/home/sqlcl

RUN useradd \
      --create-home \
      --home-dir /home/sqlcl \
      --shell /bin/bash \
      sqlcl \
    && mkdir -p /home/sqlcl/.dbtools \
    && chown -R sqlcl:sqlcl /home/sqlcl

COPY entrypoint.sh /entrypoint.sh

RUN chmod 0755 /entrypoint.sh \
    && chown sqlcl:sqlcl /entrypoint.sh

USER sqlcl

WORKDIR /home/sqlcl

ENTRYPOINT ["/entrypoint.sh"]
