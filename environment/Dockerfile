FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PAGER=cat GIT_PAGER=cat GIT_TERMINAL_PROMPT=0 LANG=C.UTF-8

COPY setup/ /opt/setup/
RUN chmod +x /opt/setup/*.sh
RUN sh /opt/setup/install_packages.sh   # baseline toolset + rustc/cargo 1.75 from the Ubuntu archive
RUN sh /opt/setup/setup_user.sh         # git identity + the single non-root `agent` user (uid 1000)
RUN sh /opt/setup/fetch_docs.sh         # RFC 1950/1951/1952 into /app/docs, digest-pinned

# --- Agent workspace ---
WORKDIR /app
COPY workspace/ /app/zlib-rs/
COPY public/ /app/tests/

# --- Grader assets: corpus generator, pristine manifest, the corpus itself. Root only. ---
COPY grader/ /root/grader/
COPY workspace/Cargo.toml /root/grader/Cargo.toml
RUN python3 /root/grader/gen_corpus.py /root/grader/corpus && chmod -R go-rwx /root

RUN chown -R agent:agent /app && rm -rf /opt/setup

# --- Sandbox timer + entrypoint ---
RUN mkdir -p /logs/agent /sandbox-timer && chmod 1777 /logs /logs/agent
COPY sandbox_timer.sh /usr/local/bin/sandbox-timer
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/sandbox-timer && chmod 0700 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
