"""알림 모듈.

NCP SENS SMS 등으로 보류/오류/발행 결과를 알린다.
지금은 발송 수단이 없어 기록만 한다 — 호출하는 쪽(watcher.notify)이 같은 내용을 반복해 보내지 않는다.
"""
from loguru import logger

sent: list[str] = []  # 테스트·점검용: 이 프로세스에서 보낸(보낼) 알림


def notify(msg: str) -> None:
    # TODO: NCP SENS SMS 발송 (settings.NCP_* / SMS_TO)
    sent.append(msg)
    logger.debug("알림(발송 수단 없음): {}", msg)
