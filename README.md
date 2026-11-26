# shhone-invoice-agent

구글시트 작업현황표를 감시해 단가를 산출하고 경리나라에 거래명세표를 발행하는 파이썬 에이전트.

## 폴더 구조

```
shhone-invoice-agent/
├── agent.py              # 진입점 (run / import / blocks / aliases / clients / calibrate)
├── clients_cli.py        # 거래처 별칭표 검사·후보 제안·마스터 검색
├── watcher.py            # 구글시트 감시
├── pricer.py             # 단가 산출
├── notifier.py           # SMS 알림
├── feedback.py           # 경리나라 내려받기 → SQLite 적재
├── issuer/
│   ├── excel_upload.py   # 엑셀 업로드
│   ├── actions.py        # 화면 조작
│   └── vision.py         # 화면 캡처/비전 검증
├── config/
│   ├── settings.py       # .env 설정 로더
│   ├── sheets.yaml       # 시트 구조 설정
│   ├── client_aliases.yaml  # 거래처 별칭표 (시트 표기 → 정식 등록명/사업자번호)
│   └── pricing_rules.md  # 단가 규칙
├── data/
│   ├── agent.db          # SQLite (git 제외)
│   ├── import/           # 경리나라 내려받기 파일
│   └── screenshots/      # 화면 캡처
└── tests/
```

## 실행법

```bash
pip install -r requirements.txt
cp .env.example .env      # 값 채우기
python agent.py run
```

## 거래처 별칭

시트 표기가 경리나라 등록명과 달라 `보류-미등록`이 되면 `config/client_aliases.yaml`에 별칭을 적는다
(watcher가 매 폴링 다시 읽음).

```bash
python agent.py clients find 에스티엠   # 정식명 / 사업자번호 / 최근거래 / 올해 건수
python agent.py aliases suggest         # 두 시트의 미해석 거래처 → 후보 (yaml 주석 형식)
python agent.py aliases check           # 별칭표 검사 (없는 등록명·번호 형식·약칭 중복, homonyms 번호)
python agent.py clients add "QMESYS(큐머시스)" 108-12-95838   # import에 없는 거래처 수동 등록
```

## 발행 (1단계 반자동)

작업완료가 체크된 묶음을 경리나라 **엑셀일괄등록** 파일로 `data/outbox/`에 만든다. 업로드는 사람이 한다.

1. 묶음의 모든 품목 행이 견적완료(또는 견적확인필요 + 사람 단가), 작성자 빈칸, 모든 행 작업완료 TRUE
   (그중 하나 이상은 에이전트가 체크 순간을 봄) → 묶음 전체 `발행대기`.
2. `run` 루프가 `issue.bundle_interval_min`(기본 10분)마다, 또는 `python agent.py issue`가 아직 파일에 없는
   발행대기 묶음을 `data/outbox/YYYYMMDD_HHMM.xls` 한 파일로 묶는다 (묶음 하나 = 명세표 한 장).
   산출근거에 `[→ 20261126_1430.xls]`.
3. 경리나라에 올린 뒤 그 묶음 아무 행의 작성자를 `AI`로 → 묶음 전체 `AI발행`, 작성자 `AI`로 통일.
4. 파일에 담긴 뒤 행을 고치면 `발행대기-수정됨` (산출근거 `뺌(수정됨)`, 로그 WARNING) — 올린 파일과
   어긋나므로 사람이 확인하고 작성자를 `AI`(이미 맞게 처리함) 또는 사람 이름(직접 처리)으로 정리.
   같은 묶음은 두 번 파일에 담기지 않는다.

```bash
python agent.py issue --dry-run   # 파일만 임시 폴더에 (시트·DB 안 건드림)
python agent.py issue             # 한 바퀴 돌고 outbox 파일 생성
python agent.py outbox            # 파일별 포함 묶음·상태
```

공통메모의 `AI <block_id>`는 `config/sheets.yaml`의 `issue.memo_block_id: false`로 끈다.
