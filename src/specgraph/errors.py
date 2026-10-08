"""specGraph 예외 계층. 모든 도메인 예외는 SpecGraphError 를 상속한다."""


class SpecGraphError(Exception):
    """specGraph 의 모든 도메인 예외의 기반."""


class ConfigError(SpecGraphError):
    """환경변수 설정이 없거나 잘못되었다."""


class GitSourceError(SpecGraphError):
    """product-docs git 원격 조회·읽기에 실패했다."""


class NonUtf8FileError(GitSourceError):
    """파일이 UTF-8 이 아니라 읽을 수 없다(그 파일만 건너뛴다)."""


class IndexingError(SpecGraphError):
    """LightRAG 또는 manifest 반영에 실패했다."""


class LlmError(IndexingError):
    """LLM 호출이 실패했다(``claude -p`` 비정상 종료 · 시간 초과 · 오류 응답 · 비활성 경로 호출)."""


class LlmRateLimitedError(LlmError):
    """구독 사용량 한도(rate limit)에 걸렸다 — 재시도하지 않고 이번 poll 의 나머지를 멈춘다."""


class NotFoundError(SpecGraphError):
    """요청한 문서·챕터·화면·정책을 찾지 못했다."""


class InvalidArgumentError(SpecGraphError):
    """도구 · 서비스 인자가 올바르지 않다(예: 지원하지 않는 검색 모드, 빈 질문)."""
