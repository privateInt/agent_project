ARG OPENSEARCH_VERSION=3.8.0

FROM opensearchproject/opensearch:${OPENSEARCH_VERSION}

# 한국어 Nori 분석기 설치
RUN /usr/share/opensearch/bin/opensearch-plugin install \
    --batch analysis-nori

# 인증서 개별 마운트 대상 디렉터리를 이미지 내부에 미리 생성
USER root

RUN mkdir -p /usr/share/opensearch/config/certs \
    && chown opensearch:opensearch /usr/share/opensearch/config/certs \
    && chmod 750 /usr/share/opensearch/config/certs

USER opensearch