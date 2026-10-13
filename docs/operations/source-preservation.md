# 소스 보존과 환경 복구

최신 검수 기준은 2026-10-07 첨부 `agent_project(5).tar`다. 현재 코드·설정·키·fixtures·candidate/source pins를 보존하고 문서와 완료 이력만 정리한다. [정리/원복 정책](project-cleanup.md)을 따른다. 업로드 소스는 서버의 DB·MinIO 객체·Docker image/volume·모델·현재 프로세스의 백업이 아니다.

1. 기존 공유 인프라의 연결 설정·자격 참조·실제 저장 경로를 재사용한다. 소스 전용 전달본에는 credential 포함 범위를 명시한다.
2. [로컬 운영](local.md), [빌드](build.md), [Data 설치](../data-platform/testing-collection.md)에 따라 필요한 의존성과 이미지를 확보한다. 소스만으로 오프라인 설치를 보장하지 않는다.
3. 기존 DB의 현재 리소스를 조회한다. 과거 UUID를 새 환경의 실행 대상으로 자동 채택하지 않는다.
4. 문서 원복은 소스 경로만 복원한다. 외부 QA SQLite/키/receipt·n8n 자산과 운영 DB/볼륨은 원복 대상이 아니다.

과거 패치 백업 삭제와 테스트 데이터 초기화는 당시 완료한 작업이며 현재 재실행 지시가 아니다. 실제 데이터 초기화는 [별도 절차](reset-test-data.md), 복구의 검증 범위는 [백업 안내](backup-restore.md)에서 확인한다.
