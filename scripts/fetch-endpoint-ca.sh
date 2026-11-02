#!/usr/bin/env bash
# fetch-endpoint-ca.sh — fetch the TLS certificate chain presented by an
# endpoint and save it as PEM files for local trust-store assembly.
#
# Reads PUBLIC certificate data only; no credentials are used or printed.
# Inspect the printed subjects/issuers/fingerprints and confirm the chain
# is the one you expect BEFORE trusting anything it saves.
#
# Usage:
#   scripts/fetch-endpoint-ca.sh [HOST[:PORT] | URL]
#   scripts/fetch-endpoint-ca.sh "$IAP_MUSE_ENDPOINT"
#   scripts/fetch-endpoint-ca.sh                  # uses $ENDPOINT_URL or $IAP_MUSE_ENDPOINT
#   OUT_DIR=/path/to/certs scripts/fetch-endpoint-ca.sh example.com:443
#
# Output (under OUT_DIR, default ./certs-out):
#   cert-00-leaf.pem, cert-01.pem, ...   individual chain certificates
#   chain.pem                             all chain certificates concatenated
#   trust-bundle.pem                      system certifi bundle + self-signed root(s),
#                                         if python3+certifi is available; otherwise
#                                         chain.pem doubles as the untrusted-side input
#   chain-summary.txt                     subject/issuer/fingerprint per certificate
set -euo pipefail

OUT_DIR="${OUT_DIR:-./certs-out}"
TARGET="${1:-${ENDPOINT_URL:-${IAP_MUSE_ENDPOINT:-}}}"

command -v openssl >/dev/null || { echo "error: openssl is required" >&2; exit 2; }
command -v awk >/dev/null || { echo "error: awk is required" >&2; exit 2; }
if [[ -z "$TARGET" ]]; then
  echo "usage: $0 [HOST[:PORT] | URL]  (or set ENDPOINT_URL / IAP_MUSE_ENDPOINT)" >&2
  exit 2
fi

# Normalize URL-or-host into host + port (default 443).
HOSTPORT="$TARGET"
HOSTPORT="${HOSTPORT#https://}"
HOSTPORT="${HOSTPORT#http://}"
HOSTPORT="${HOSTPORT%%/*}"
HOST="${HOSTPORT%%:*}"
PORT="${HOSTPORT##*:}"
if [[ "$HOST" == "$PORT" ]]; then PORT="443"; fi
if [[ -z "$HOST" ]]; then echo "error: could not parse a host from '$TARGET'" >&2; exit 2; fi

mkdir -p "$OUT_DIR"
echo "Fetching certificate chain (public data only)..."
if ! openssl s_client -connect "${HOST}:${PORT}" -servername "$HOST" -showcerts </dev/null 2>/dev/null \
  | awk -v dir="$OUT_DIR" 'BEGIN{n=-1} /BEGIN CERTIFICATE/{n++} n>=0{print > (dir "/cert-" sprintf("%02d", n) ".pem")}' \
  && true; then
  echo "error: openssl s_client failed (unreachable host/port?)" >&2
  exit 1
fi
# shellcheck disable=SC2012
COUNT=$(ls -1 "$OUT_DIR"/cert-*.pem 2>/dev/null | wc -l | tr -d ' ')
if [[ "$COUNT" -eq 0 ]]; then
  echo "error: no certificates captured (TLS handshake failed?)" >&2
  exit 1
fi
echo "Captured $COUNT certificate(s)."

SUMMARY="$OUT_DIR/chain-summary.txt"
: > "$SUMMARY"
ROOTS=""
i=0
for cert in "$OUT_DIR"/cert-*.pem; do
  SUBJECT=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 2>/dev/null | sed 's/^subject=//')
  ISSUER=$(openssl x509 -in "$cert" -noout -issuer -nameopt RFC2253 2>/dev/null | sed 's/^issuer=//')
  FPRINT=$(openssl x509 -in "$cert" -noout -fingerprint -sha256 2>/dev/null | sed 's/^.*=//')
  ROLE="intermediate"
  if [[ "$i" -eq 0 ]]; then ROLE="leaf"; fi
  if [[ -n "$SUBJECT" && "$SUBJECT" == "$ISSUER" ]]; then
    ROLE="self-signed root (trust candidate)"
    ROOTS="$ROOTS $cert"
  fi
  {
    echo "--- $(basename "$cert") [$ROLE]"
    echo "subject: $SUBJECT"
    echo "issuer:  $ISSUER"
    echo "sha256:  $FPRINT"
  } | tee -a "$SUMMARY"
  i=$((i + 1))
done

cat "$OUT_DIR"/cert-*.pem > "$OUT_DIR/chain.pem"

LEAF="$OUT_DIR/cert-00.pem"
TOP_CERT=$(ls -1 "$OUT_DIR"/cert-*.pem | sort | tail -1)

# Servers never send the self-signed root: if none was captured, the trust
# anchor must come from the local machine (e.g. macOS Keychain, where
# corporate/ISP filtering roots are installed so browsers keep working).
if [[ -z "$ROOTS" ]] && [[ "$(uname)" == "Darwin" ]] && command -v security >/dev/null; then
  TOP_ISSUER_CN=$(openssl x509 -in "$TOP_CERT" -noout -issuer -nameopt RFC2253 2>/dev/null | sed -n 's/.*CN=\([^,]*\).*/\1/p')
  if [[ -n "${TOP_ISSUER_CN:-}" ]] && security find-certificate -c "$TOP_ISSUER_CN" -p >/dev/null 2>&1; then
    security find-certificate -c "$TOP_ISSUER_CN" -p > "$OUT_DIR/root-from-keychain.pem"
    echo "Found issuing root '$TOP_ISSUER_CN' in macOS Keychain; saved as root-from-keychain.pem."
    ROOTS="$OUT_DIR/root-from-keychain.pem"
  else
    echo "NOTE: no self-signed root in the presented chain and none matching"
    echo "  '${TOP_ISSUER_CN:-unknown}' in the macOS Keychain. Ask whoever operates"
    echo "  the filtering endpoint (or IT) for the root CA PEM, save it as"
    echo "  $OUT_DIR/root-from-operator.pem, and re-run assembly below."
  fi
fi

# Assemble a trust bundle: system certifi bundle + root candidate(s).
BUNDLE="$OUT_DIR/trust-bundle.pem"
ROOT_COUNT=0
if command -v python3 >/dev/null && python3 -c "import certifi" 2>/dev/null; then
  CABUNDLE=$(python3 -c "import certifi; print(certifi.where())")
  cat "$CABUNDLE" > "$BUNDLE"
  # shellcheck disable=SC2086
  if [[ -n "$ROOTS" ]]; then
    # shellcheck disable=SC2086
    for rc in $ROOTS; do
      cat "$rc" >> "$BUNDLE"
      ROOT_COUNT=$((ROOT_COUNT + 1))
    done
  fi
  echo "Wrote $BUNDLE (system bundle + $ROOT_COUNT captured root(s))."
else
  cat "$OUT_DIR"/cert-*.pem > "$BUNDLE"
  echo "NOTE: python3/certifi unavailable; $BUNDLE holds only the fetched chain."
  echo "Append your system CA bundle ahead of it before use as SSL_CERT_FILE."
fi

# Self-check: verify the leaf against the assembled bundle.
INTERMEDIATES="$OUT_DIR/intermediates.pem"
: > "$INTERMEDIATES"
for cert in "$OUT_DIR"/cert-*.pem; do
  if [[ "$cert" == "$LEAF" ]]; then continue; fi
  SUBJ=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253 2>/dev/null | sed 's/^subject=//')
  ISS=$(openssl x509 -in "$cert" -noout -issuer -nameopt RFC2253 2>/dev/null | sed 's/^issuer=//')
  if [[ "$SUBJ" == "$ISS" ]]; then continue; fi
  cat "$cert" >> "$INTERMEDIATES"
done
echo "--- self-check (leaf against assembled bundle):"
if [[ -s "$INTERMEDIATES" ]]; then
  openssl verify -CAfile "$BUNDLE" -untrusted "$INTERMEDIATES" "$LEAF" || true
else
  openssl verify -CAfile "$BUNDLE" "$LEAF" || true
fi
echo "---"
echo "Review chain-summary.txt, then point the platform at the bundle, e.g.:"
echo "  export SSL_CERT_FILE=\"$BUNDLE\""
