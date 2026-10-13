#!/usr/bin/env bash
set -Eeuo pipefail

# OpenSearch 공식 Docker 이미지의 기본 실행 UID
readonly OPENSEARCH_UID="${OPENSEARCH_UID:-1000}"

# 현재 터미널 위치가 아닌 스크립트 파일 위치를 기준으로 동작
SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
  pwd
)"

readonly BASE_DIR="${SCRIPT_DIR}"
readonly CERT_DIR="${BASE_DIR}/certs"

readonly CURRENT_UID="$(id -u)"
readonly CURRENT_GID="$(id -g)"

# 새로 생성되는 개인키의 초기 권한을 제한
umask 077

# --------------------------------------------------
# 0. 사전 검증
# --------------------------------------------------

if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl이 설치되어 있지 않습니다."
  exit 1
fi

if [[ "${CURRENT_UID}" == "0" ]]; then
  echo "ERROR: 이 스크립트를 sudo 또는 root 계정으로 실행하지 마세요."
  echo "일반 사용자(shshin) 계정으로 실행하세요."
  exit 1
fi

if [[ "${CURRENT_UID}" != "${OPENSEARCH_UID}" ]]; then
  echo "===> Host UID/GID   : ${CURRENT_UID}:${CURRENT_GID}"
  echo "===> OpenSearch UID : ${OPENSEARCH_UID}"
  echo "===> UID가 다르므로 ACL로 OpenSearch 읽기 권한을 설정합니다."

  if ! command -v setfacl >/dev/null 2>&1; then
    echo
    echo "ERROR: setfacl 명령어가 필요합니다."
    echo "다음 명령어로 설치하세요."
    echo
    echo "sudo apt-get update"
    echo "sudo apt-get install -y acl"
    exit 1
  fi
fi

# 예상하지 않은 경로를 rm -rf 하는 상황 방지
case "${CERT_DIR}" in
  "${BASE_DIR}/certs")
    ;;
  *)
    echo "ERROR: 안전하지 않은 인증서 경로입니다: ${CERT_DIR}"
    exit 1
    ;;
esac

echo "===> Script directory     : ${SCRIPT_DIR}"
echo "===> Certificate directory: ${CERT_DIR}"

# --------------------------------------------------
# 기존 인증서 제거
# --------------------------------------------------

if [[ -d "${CERT_DIR}" ]]; then
  echo "===> Remove old certificate directory"
  rm -rf -- "${CERT_DIR}"
fi

mkdir -p -- "${CERT_DIR}"
chmod 750 "${CERT_DIR}"

# --------------------------------------------------
# 인증서 설정 생성 함수
# --------------------------------------------------

make_node_config() {
  local common_name="$1"
  local san_entries="$2"

  cat <<EOF
[ req ]
default_bits = 4096
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no
default_md = sha256

[ req_distinguished_name ]
C = KR
ST = Seoul
L = Seoul
O = MyOrg
CN = ${common_name}

[ v3_req ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[ alt_names ]
${san_entries}
EOF
}

make_admin_config() {
  cat <<'EOF'
[ req ]
default_bits = 4096
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no
default_md = sha256

[ req_distinguished_name ]
C = KR
ST = Seoul
L = Seoul
O = MyOrg
CN = admin

[ v3_req ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = admin
EOF
}

# --------------------------------------------------
# 1. Root CA 생성
# --------------------------------------------------

echo "===> Create Root CA configuration"

cat > "${CERT_DIR}/openssl-ca.cnf" <<'EOF'
[ req ]
default_bits = 4096
distinguished_name = req_distinguished_name
x509_extensions = v3_ca
prompt = no
default_md = sha256

[ req_distinguished_name ]
C = KR
ST = Seoul
L = Seoul
O = MyOrg
CN = MyRootCA

[ v3_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:true, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
EOF

echo "===> Create Root CA private key"

# genpkey는 OpenSearch가 요구하는 PKCS#8 형식으로 개인키 생성
openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:4096 \
  -out "${CERT_DIR}/rootCA.key"

echo "===> Create Root CA certificate"

openssl req \
  -x509 \
  -new \
  -sha256 \
  -key "${CERT_DIR}/rootCA.key" \
  -days 3650 \
  -out "${CERT_DIR}/rootCA.pem" \
  -config "${CERT_DIR}/openssl-ca.cnf"

# --------------------------------------------------
# 2. Node1 인증서 생성
# --------------------------------------------------

echo "===> Create Node1 configuration"

make_node_config \
  "opensearch-node1" \
  "DNS.1 = opensearch-node1
DNS.2 = localhost
IP.1 = 127.0.0.1" \
  > "${CERT_DIR}/openssl-node1.cnf"

echo "===> Create Node1 private key"

openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:4096 \
  -out "${CERT_DIR}/node1.key"

echo "===> Create Node1 CSR"

openssl req \
  -new \
  -sha256 \
  -key "${CERT_DIR}/node1.key" \
  -out "${CERT_DIR}/node1.csr" \
  -config "${CERT_DIR}/openssl-node1.cnf"

echo "===> Sign Node1 certificate"

openssl x509 \
  -req \
  -sha256 \
  -in "${CERT_DIR}/node1.csr" \
  -CA "${CERT_DIR}/rootCA.pem" \
  -CAkey "${CERT_DIR}/rootCA.key" \
  -CAcreateserial \
  -out "${CERT_DIR}/node1.crt" \
  -days 1825 \
  -extensions v3_req \
  -extfile "${CERT_DIR}/openssl-node1.cnf"

# --------------------------------------------------
# 3. Node2 인증서 생성
# --------------------------------------------------

echo "===> Create Node2 configuration"

make_node_config \
  "opensearch-node2" \
  "DNS.1 = opensearch-node2" \
  > "${CERT_DIR}/openssl-node2.cnf"

echo "===> Create Node2 private key"

openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:4096 \
  -out "${CERT_DIR}/node2.key"

echo "===> Create Node2 CSR"

openssl req \
  -new \
  -sha256 \
  -key "${CERT_DIR}/node2.key" \
  -out "${CERT_DIR}/node2.csr" \
  -config "${CERT_DIR}/openssl-node2.cnf"

echo "===> Sign Node2 certificate"

openssl x509 \
  -req \
  -sha256 \
  -in "${CERT_DIR}/node2.csr" \
  -CA "${CERT_DIR}/rootCA.pem" \
  -CAkey "${CERT_DIR}/rootCA.key" \
  -CAserial "${CERT_DIR}/rootCA.srl" \
  -out "${CERT_DIR}/node2.crt" \
  -days 1825 \
  -extensions v3_req \
  -extfile "${CERT_DIR}/openssl-node2.cnf"

# --------------------------------------------------
# 4. Admin 인증서 생성
# --------------------------------------------------

echo "===> Create Admin configuration"

make_admin_config > "${CERT_DIR}/openssl-admin.cnf"

echo "===> Create Admin private key"

openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:4096 \
  -out "${CERT_DIR}/admin.key"

echo "===> Create Admin CSR"

openssl req \
  -new \
  -sha256 \
  -key "${CERT_DIR}/admin.key" \
  -out "${CERT_DIR}/admin.csr" \
  -config "${CERT_DIR}/openssl-admin.cnf"

echo "===> Sign Admin certificate"

openssl x509 \
  -req \
  -sha256 \
  -in "${CERT_DIR}/admin.csr" \
  -CA "${CERT_DIR}/rootCA.pem" \
  -CAkey "${CERT_DIR}/rootCA.key" \
  -CAserial "${CERT_DIR}/rootCA.srl" \
  -out "${CERT_DIR}/admin.crt" \
  -days 1825 \
  -extensions v3_req \
  -extfile "${CERT_DIR}/openssl-admin.cnf"

# --------------------------------------------------
# 5. 생성된 인증서 검증
# --------------------------------------------------

echo "===> Verify certificate chain"

openssl verify \
  -CAfile "${CERT_DIR}/rootCA.pem" \
  "${CERT_DIR}/node1.crt" \
  "${CERT_DIR}/node2.crt" \
  "${CERT_DIR}/admin.crt"

echo "===> Verify Node1 hostname"

openssl verify \
  -CAfile "${CERT_DIR}/rootCA.pem" \
  -verify_hostname opensearch-node1 \
  "${CERT_DIR}/node1.crt"

echo "===> Verify Node2 hostname"

openssl verify \
  -CAfile "${CERT_DIR}/rootCA.pem" \
  -verify_hostname opensearch-node2 \
  "${CERT_DIR}/node2.crt"

# --------------------------------------------------
# 6. 기본 파일 권한 설정
# --------------------------------------------------

echo "===> Configure base permissions"

# 디렉터리는 소유자와 그룹만 조회 및 진입 가능
find "${CERT_DIR}" \
  -type d \
  -exec chmod 750 {} +

# 설정, CSR, serial 파일
find "${CERT_DIR}" \
  -type f \
  -exec chmod 640 {} +

# 공개 인증서는 읽기 허용
find "${CERT_DIR}" \
  -type f \
  \( -name "*.crt" -o -name "*.pem" \) \
  -exec chmod 644 {} +

# 개인키는 소유자만 읽고 쓸 수 있음
# 공개 인증서 처리 이후 실행해야 *-key.pem도 600으로 유지됨
find "${CERT_DIR}" \
  -type f \
  \( -name "*.key" -o -name "*-key.pem" \) \
  -exec chmod 600 {} +

# --------------------------------------------------
# 7. OpenSearch UID에 ACL 부여
# --------------------------------------------------

if [[ "${CURRENT_UID}" != "${OPENSEARCH_UID}" ]]; then
  echo "===> Configure ACL for OpenSearch UID ${OPENSEARCH_UID}"

  # 디렉터리 조회 및 진입 권한
  find "${CERT_DIR}" \
    -type d \
    -exec setfacl -m "u:${OPENSEARCH_UID}:r-x" {} +

  # OpenSearch가 실제로 사용하는 인증서만 읽기 허용
  for cert_file in \
    "${CERT_DIR}/rootCA.pem" \
    "${CERT_DIR}/node1.crt" \
    "${CERT_DIR}/node1.key" \
    "${CERT_DIR}/node2.crt" \
    "${CERT_DIR}/node2.key"
  do
    setfacl -m "u:${OPENSEARCH_UID}:r--" "${cert_file}"
  done

  echo "===> ACL configuration completed"
fi

# --------------------------------------------------
# 8. 최종 검증 및 결과 출력
# --------------------------------------------------

echo
echo "===> Certificate generation completed"
echo "===> Output directory: ${CERT_DIR}"
echo

find "${CERT_DIR}" \
  -maxdepth 1 \
  -printf '%M %u:%g %f\n' \
  | sort

if [[ "${CURRENT_UID}" != "${OPENSEARCH_UID}" ]]; then
  echo
  echo "===> Node1 key ACL"
  getfacl --absolute-names "${CERT_DIR}/node1.key"

  echo
  echo "===> Node2 key ACL"
  getfacl --absolute-names "${CERT_DIR}/node2.key"
fi

echo
echo "IMPORTANT:"
echo "  - rootCA.key는 외부에 노출하거나 컨테이너에 마운트하지 마세요."
echo "  - OpenSearch에는 rootCA.pem과 해당 노드의 crt/key만 마운트하세요."
echo "  - admin.crt/admin.key는 Security 관리 작업에만 사용하세요."
echo "  - OPENSEARCH_UID=1006으로 실행하지 마세요."