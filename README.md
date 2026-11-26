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
python agent.py clients trend "대진엔지니어링(김영주)"   # 재주문 쌍(품목·날짜·단가 전후·변화율) + 동결형/인상형 판정 근거
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
python agent.py backtest build            # 2026년 월 탭 전부 + 남은 2025년 탭(12월) (시트·DB 읽기만)
python agent.py backtest build --seed 3   # 콘솔 샘플만 다르게
python agent.py backtest inflation        # DB 같은 거래처·같은 품목 연도별 단가 변화율
```

임가공 시트의 그 해 월 탭 전부(1월~이번 달, 왼쪽 탭만이 아님)의 품목 행을 묶음 규칙대로 모아 거래처를 해석하고,
D·E·F·호닝 전 치수(11월~: 연마량/치대)·호닝 전/후 노트·호닝 후 치수에서 구경·공차·기장·수량·재질·특이사항·Ra를
뽑는다(못 뽑으면 빈칸, 원문은 `raw_*` 열). 탭마다 열 배치가 달라 2행 헤더 글자로 열을 찾는다.
DB lines(kind='job')와 같은 사업자번호(또는 거래처명)·날짜 ±15일·구경 ±max(1.0mm, 1%)(양쪽 기장 있으면 ±10%)로 짝을 지어
`actual_unit_price`·`actual_qty`·`statement_id`를 붙인다: 후보 하나 `exact`, 여럿 → 가장 가까운 날짜 `nearest`,
없음 `none` + `match_reason`. 결과 `data/backtest/imgagong_<연도>.csv`(UTF-8-SIG)·`.xlsx` (git 제외).
미해석 거래처는 `aliases suggest`와 같은 형식으로 `data/backtest/unresolved_clients.txt`에 (행 많은 순) — yaml에 옮겨 쓴다.

`backtest inflation`: DB lines(job) 전체에서 같은 거래처 + 같은 품목(구경 ±0.5, 공차 동일, 기장은 양쪽 다 있으면 ±10%,
한쪽만 있으면 다른 품목)의 연도별 단가 중앙값 → 품목이 나온 연속한 두 해마다 연환산 변화율. 전체·연도쌍·구경대·거래처 상위 20,
매뉴얼 '연 9%'와 비교 한 줄. `data/backtest/inflation.csv`.

견적 엔진용 유사건 검색은 `cases.find_similar_cases(index, query, cutoff_date)` — cutoff_date **이전**(당일 제외) 자료만 본다
(백테스트 정답 누출 방지, cutoff 없으면 오류).

## 임가공 견적 (pricer_job → pricer_ai)

```bash
python agent.py backtest run --no-ai                     # 사다리(사전단가·동일품)만 → results_noai.csv
python agent.py backtest run --ai --limit 200 --seed 0   # 미결 행 표본을 Claude로 → results_ai.csv (API 비용 발생)
```

- **A 사다리** (`pricer_job.py`): 사전단가(D열 숫자, 5천~500만 원만; 부호·mm가 붙은 값은 연마량이라 제외;
  `20/30`은 E 품목 수와 같을 때 품목별; 같은 거래처 과거 단가 중앙값과 3배 이상 차이 → low, 과거 없음 → mid)
  → 동일품(구경·공차·기장·치대·품목 성질 키워드 7개 — 명세표에만 있는 키워드는 허용하되 최대 mid; 최근 건 기준:
  6개월 이내 그대로 high / 6~12개월 그대로 mid '동결 유지 — 확인'(`config/sheets.yaml` `pricing.same_item_6_12m_confidence`로
  high/mid 전환, 고치면 다음 견적부터) / 12~24개월 ×1.05 100원 버림 mid '+5% 인상 제안'
  (거래처 인상형 = 최근 3쌍 평균 > 5%면 그 평균, 상한 15%) / 24개월 초과는 AI로(직전 단가·날짜 + '최소 +5% 이상 제안',
  결과 최대 mid); 수량 3배↑/⅓↓이면 AI로) → 미결. 부록 B.1 규칙 1 표와 같음.
  거래처 줄은 사업자번호 기준 — 이름이 같아도 번호가 다른 동명 거래처 줄은 쓰지 않는다(번호 없는 줄만 이름으로).
- 파서: 'OD Ø130'·'외경Ø80'처럼 OD/외경 표시가 붙은 Ø는 구경으로 안 씀. 'Ø67 / 1개 + Ø45 + Ø25 / 3개'처럼 '+'로
  이어진 Ø는 품목마다 나누고 뒤의 수량을 나눠 줌(수량 하나면 각 품목 같은 수량 + 경고). '+' 없이 이어진 Ø
  ('양쪽 Ø250 Ø300 / 1개', 'Ø72.5 (Ø73)')와 HOLE 행은 한 품목.
- **B Claude** (`pricer_ai.py`): `claude-sonnet-5-5`, 시스템 = `config/pricing_rules.md` 전문(prompt caching).
  입력 = D·E·F·H·S·T·U 원문(빈 칸 생략) + 파싱값(구경·공차·기장·수량·재질·특이사항·치대),
  참고 사례(같은 거래처 8 · 타 거래처 5 · 인상 성향). 부록 B.5 JSON(structured outputs), max_tokens 300.
  호출마다 `logs/ai_calls.jsonl`에 토큰·비용. 운영 호출은 하루 300건, 운영 첫날은 60건.
- **운영** (`pricer.py` kind=job): confidence high만 견적완료. mid/low → 단가 + `[가견적-mid]`/`[가견적-low]`
  + 견적확인필요(발행 차단). 사람이 상태 칸을 비우면 적힌 단가로, 단가를 고치면 고친 값으로 견적완료
  (에이전트가 적은 단가·지문은 DB `job_quotes` — `agent_price/agent_method/agent_conf`는 에이전트 견적 그대로,
  `human_price/human_at`은 사람 확정 단가: 교정 세션 재료). 미결·AI 실패 → 단가 비우고 견적확인필요.
- **시트 확정 즉시 학습** (`sheet_lines.py`): 사람이 단가를 확정(상태 칸 비움·단가 수정 → 견적완료)하거나 묶음이
  AI발행으로 마감되면, 그 행을 발행 줄과 같은 방식(`job_lines`)으로 줄로 만들어 DB `lines`에 `source='sheet'`,
  `row_id`=시트 행으로 바로 넣는다 → 다음 견적부터 동일품·유사품 검색에 쓰임(같은 달 재주문이 새 단가로 동일품 high).
  같은 행은 교체(중복 없음), 동명확인·dry-run은 학습 안 함. 월간 import 때 같은 거래처(사업자번호, 없으면 이름)·
  날짜·구경(±0.3mm)의 공식 줄이 들어오면 sheet 줄은 지운다. 백테스트·마스터 엑셀은 공식 줄만 본다.
  건별 INFO 로그 `임가공 견적 … 거래처 | 입력 | 단가 | method | confidence | 상태`. `--dry-run`은 AI를 부르지 않는다.
- **다품목**: 표준은 품목 하나당 한 줄(날짜·거래처는 블록 첫 줄에만 — 2번째 줄 이후도 각자 독립 견적, 거래처는 블록 것).
  폴백으로 E에 '+'로 품목이 여럿인 한 줄(`job_parse.item_segments`: 수량 표기 뒤 '+'·구경마다 나눔, 공차 '+'는 안 나눔)은
  구간마다 사다리를 따로 적용. 뒤 구간은 앞 구간의 공통 머리(재질·구경·공차·조도)를 물려받음
  (`SUS316 Ø63.5 +0~+0.02 / 432L / 2개 + 534L / 1개` → 두 구간 모두 SUS316 Ø63.5 공차 같음, 기장만 다름).
  D가 구간 수와 같은 개수의 숫자(`6/5.5`)면 구간별 사전단가. 미결 구간은 Claude 한 번 호출로 구간별 JSON 배열.
  단가 칸 `60000/55000/50000`, 산출근거는 구간별 `; `, confidence는 구간 중 최저이되 최대 mid(→ 항상 견적확인필요).
  사람이 단가를 고치면 단가 개수 = 품목 수일 때만 견적완료, 아니면 견적확인필요 + 산출근거 `단가 개수 불일치`.
- **발행** (`issuer`, kind=job): 트리거·조건은 호닝파이프와 같음(블록 전체 J열 작업완료 + 전환 관찰, 블록 전원 견적완료,
  작성자 없음). 단, 임가공은 견적확인필요(가견적 단가가 적혀 있음)는 발행하지 않는다.
  줄 = E(없으면 D)의 품목 구간마다 한 줄(견적과 같은 나눔 — 구경마다, '+' 뒤 구경 없는 구간은 머리 상속). 품목명·규격은 같은 거래처 동일품 최근 명세표 표기를 따르고(규격은 구경·기장이 똑같을 때만),
  없으면 품목명 '수리품 SUS 막힘 시린더호닝'(성질·재질 키워드 + 시린더호닝, 면작업이면 '면작업'),
  규격 = 그 품목 구간에서 수량 부분을 뺀 글('Ø50+0.0~+0.05 / 300'). 단가 '40000/25000'은 품목별, 하나면 모든 줄.
  수량은 품목 구간별 'N개'(없으면 1, 경고 로그). 구경이 없는 행은 한 줄: 같은 거래처 명세표에 품목명이 같은 글(공백 무시,
  서로 포함)이 있으면 그 표기, 없으면 품목명 = E(없으면 D)에서 수량만 뺀 글, 규격 비움. 보류는 단가 개수 ≠ 품목 개수일 때만.
  호닝파이프와 같은 outbox 파일에 섞임.
