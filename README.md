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
python agent.py import clients data/import/거래처목록.xls      # 대표자명 채움 (사업자번호 기준 갱신/추가)
```

같은 이름에 사업자번호가 여럿이면 시트에 `거래처(대표자명)`으로 구분할 수 있다 — 공백 무시, `( )`/`（ ）` 모두.
대표자명이 정확히 한 곳과 맞으면 그 번호로 확정(산출근거 `대표명 일치 김영주`), 둘 이상에 같으면(동명이인)
확정하지 않음, 어느 대표자명과도 안 맞는 괄호(`(택배)` 등)는 비고. 우선순위: 별칭 사업자번호·homonyms 명시
번호 > 대표자명 일치 = `주)`/`(개인)` 표시 > 최근 12개월 거래 하나뿐 규칙.

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

작성자 잠금은 **묶음 단위**: 묶음 안 어느 행이든(보통 날짜 있는 첫 행, 메모 행 포함) 사람 작성자가 있으면
다른 줄이 비어 있거나 `AI`여도 묶음 전체 사람처리 — 기존건-확인필요·발행대기·AI발행 판정보다 우선.
서로 다른 작성자가 섞이면(예: 1행 `고문`, 2행 `AI`) 사람처리 + 로그 WARNING.
이미 outbox 파일에 담긴 묶음에 나중에 사람 작성자가 붙으면 상태 `사람처리-발행확인` + 산출근거
`[주의: 파일 …xls에 담긴 뒤 작성자 변경 → 경리나라 중복 발행 확인]` + WARNING. 확인 후 상태 칸을 비우면 보통 사람처리.

```bash
python agent.py issue --dry-run   # 파일만 임시 폴더에 (시트·DB 안 건드림)
python agent.py issue             # 한 바퀴 돌고 outbox 파일 생성
python agent.py outbox            # 파일별 포함 묶음·상태
```

공통메모는 `config/sheets.yaml`의 `issue.memo_text`를 명세표마다 첫 줄에만 그대로 넣는다(이어지는 품목 줄은 비움).
끝의 `(AI)`로 월간 import(`feedback.py`)가 명세표 목록의 공통메모 열을 보고 AI 발행 건을 `statements.issued_by='AI'`로 표시한다
(열이 없으면 건너뛰고 로그만). 묶음 추적은 DB(`issue_file_blocks`)로 한다.

## 임가공 백테스트 정답지

```bash
python agent.py backtest build            # 2026년 월 탭 전부 (시트·DB 읽기만)
python agent.py backtest build --seed 3   # 콘솔 샘플만 다르게
```

임가공 시트의 그 해 월 탭 전부(1월~이번 달, 왼쪽 탭만이 아님)의 품목 행을 묶음 규칙대로 모아 거래처를 해석하고,
D·E·F·호닝 전 치수(11월~: 연마량/치대)·호닝 전/후 노트·호닝 후 치수에서 구경·공차·기장·수량·재질·특이사항·Ra를
뽑는다(못 뽑으면 빈칸, 원문은 `raw_*` 열). 탭마다 열 배치가 달라 2행 헤더 글자로 열을 찾는다.
DB lines(kind='job')와 같은 사업자번호(또는 거래처명)·날짜 ±10일·구경 ±0.5mm(양쪽 기장 있으면 ±10%)로 짝을 지어
`actual_unit_price`·`actual_qty`·`statement_id`를 붙인다: 후보 하나 `exact`, 여럿 → 가장 가까운 날짜 `nearest`,
없음 `none` + `match_reason`. 결과 `data/backtest/imgagong_<연도>.csv`(UTF-8-SIG)·`.xlsx` (git 제외).
