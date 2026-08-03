# OpenBao PRODUCTION configuration (khctl apply phase 5).
# Replaces dev mode: raft persistence (survives restarts — the pilot's
# in-memory vault does not), manual init/unseal under the plan's custody
# ceremony (DEPLOY_NOTES.md).
ui = false

storage "raft" {
  path    = "/openbao/data"
  node_id = "kh-bao-1"
}

listener "tcp" {
  address = "0.0.0.0:8200"
  # Loopback/compose-network only today. TLS on this listener is part of
  # §8.9 item 2 (remote-seam hardening) — required before this port is
  # ever reachable beyond the box.
  tls_disable = true
}

api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"
